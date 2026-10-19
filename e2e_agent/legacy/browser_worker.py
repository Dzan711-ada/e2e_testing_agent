"""A separate browser process with a JSON-lines protocol and one live session.

Process separation and snippet checks are not an OS security sandbox.
"""

import ast
import asyncio
from contextlib import redirect_stdout
import inspect
import json
from pathlib import Path
import re
import sys

from playwright.async_api import async_playwright, expect

from .codegen import validate_snippet


async def serve() -> None:
    browser = context = page = None
    namespace = {}
    artifact_dir = None
    async with async_playwright() as playwright:
        try:
            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                if command["op"] == "close":
                    break
                try:
                    if command["op"] == "start":
                        artifact_dir = Path(command["artifact_dir"])
                        browser = await playwright.chromium.launch(channel="chromium", headless=command["headless"])
                        context = await browser.new_context()
                        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                        page = await context.new_page()
                        page.set_default_timeout(command["timeout_ms"])
                        page.set_default_navigation_timeout(command["timeout_ms"])
                        await page.goto(command["url"], wait_until="domcontentloaded")
                        namespace = {"page": page, "expect": expect, "re": re, "asyncio": asyncio}
                    elif command["op"] == "execute":
                        source = validate_snippet(command["code"], assertion=command.get("assertion", False))
                        compiled = compile(source, "<generated-step>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                        with redirect_stdout(sys.stderr):
                            execution = eval(compiled, namespace)
                            if inspect.isawaitable(execution):
                                await execution
                    else:
                        raise ValueError("Unknown browser operation")
                    result = {"ok": True}
                except Exception as error:
                    result = {"ok": False, "error": str(error), "error_type": type(error).__name__}
                if page is not None:
                    try:
                        result.update(html=await page.content(), url=page.url)
                        step = command.get("step", 0)
                        screenshot = artifact_dir / f"step-{step:02d}.png"
                        await page.screenshot(path=str(screenshot), full_page=True, timeout=5000)
                        result["screenshot"] = screenshot.name
                    except Exception as capture_error:
                        print(f"Artifact capture: {type(capture_error).__name__}", file=sys.stderr)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            if context is not None:
                try:
                    await context.tracing.stop(path=str(artifact_dir / "generation-trace.zip"))
                except Exception:
                    pass
            if browser is not None:
                await browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    asyncio.run(serve())
