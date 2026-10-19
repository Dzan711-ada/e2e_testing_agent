"""Browser worker lifetime and bounded IPC, independent of the model service."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .config import Settings
from .models import BrowserResult


def execution_environment(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.endswith(("API_KEY", "API_TOKEN")):
            env.pop(key)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env.update(PYTHONIOENCODING="utf-8", PLAYWRIGHT_BROWSERS_PATH=str(settings.browsers_path),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    return env


async def stop_process(process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        # Only the process created by this run and its children are terminated.
        await asyncio.to_thread(subprocess.run,
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.kill()
    await process.wait()


class BrowserSession:
    def __init__(self, settings: Settings, artifact_dir: Path):
        self.settings, self.artifact_dir = settings, artifact_dir
        self.process = None
        self.log = None

    async def start(self, url: str) -> BrowserResult:
        self.log = (self.artifact_dir / "browser-worker.log").open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "e2e_agent.browser_worker",
            cwd=self.settings.root, env=execution_environment(self.settings),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.log, limit=16 * 1024 * 1024,
        )
        return await self.request({
            "op": "start", "url": url, "artifact_dir": str(self.artifact_dir),
            "headless": self.settings.headless, "timeout_ms": self.settings.action_timeout_ms,
        })

    async def request(self, payload: dict) -> BrowserResult:
        try:
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.settings.worker_timeout)
            if not line:
                raise RuntimeError("浏览器进程已退出，请查看 browser-worker.log；可运行 check --browser 检查安装。")
            return BrowserResult.model_validate_json(line)
        except asyncio.TimeoutError as error:
            await stop_process(self.process)
            raise TimeoutError("浏览器执行超时，已停止本次浏览器进程。") from error

    async def execute(self, code: str, step: int, assertion: bool) -> BrowserResult:
        return await self.request({"op": "execute", "code": code, "step": step, "assertion": assertion})

    async def close(self) -> None:
        try:
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.stdin.write(b'{"op":"close"}\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), timeout=10)
                except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                    await stop_process(self.process)
        finally:
            if self.log is not None:
                self.log.close()
