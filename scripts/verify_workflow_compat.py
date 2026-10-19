"""Exercise the installed LangGraph API; no model or human resource is used."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from e2e_agent.browser import BrowserSession
from e2e_agent.config import Settings
from e2e_agent.sample import sample_server


class PauseState(TypedDict, total=False):
    issued: bool
    reply: str
    completed: bool


def pause_graph(send, consume):
    graph = StateGraph(PauseState)
    graph.add_node("send", send)
    graph.add_node("consume", consume)
    graph.add_edge(START, "send")
    graph.add_edge("send", "consume")
    graph.add_edge("consume", END)
    return graph.compile(checkpointer=MemorySaver(), interrupt_before=["consume"])


def windows_process_tree(items):
    # Windows retains a creator PID after that parent exits. A newer process can
    # reuse it; older children then belong to the departed parent, not this run.
    by_id = {item["ProcessId"]: item for item in items}
    return {pid: (item["ParentProcessId"] if item["ParentProcessId"] not in by_id
                  or item["Created"] >= by_id[item["ParentProcessId"]]["Created"] else -1)
            for pid, item in by_id.items()}


def process_tree():
    if os.name != "nt":
        output = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True)
        return {int(line.split()[0]): int(line.split()[1]) for line in output.splitlines()}
    command = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,@{Name='Created';Expression={$_.CreationDate.ToUniversalTime().Ticks}} | ConvertTo-Json -Compress"
    output = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
    return windows_process_tree(json.loads(output))


def descendants(tree, root):
    found = {root}
    while True:
        more = {pid for pid, parent in tree.items() if parent in found}
        if more <= found:
            return found
        found |= more


async def experiment(output_dir: Path):
    settings = Settings.load()
    report = {"api": "MemorySaver + interrupt_before + get_state/update_state + invoke(None), via asyncio.to_thread",
              "cases": [], "model_calls": 0}
    with sample_server() as url:
        for mode in ("resume", "cancel_while_paused"):
            directory = output_dir / mode
            directory.mkdir(parents=True)
            browser = BrowserSession(settings, directory)
            effects = []
            owned = set()

            async def execute(code, assertion=False):
                result = await browser.execute(code, len(effects) + 1, assertion)
                if not result.ok:
                    raise AssertionError(result.error)

            event_loop = asyncio.get_running_loop()

            def send(state):
                asyncio.run_coroutine_threadsafe(execute('await page.evaluate("window.sentCount = (window.sentCount || 0) + 1")'), event_loop).result(timeout=45)
                effects.append("send")
                return {"issued": True}

            def consume(state):
                assert state.get("reply") == "compatibility-only", "No reply, must not continue"
                asyncio.run_coroutine_threadsafe(execute('await page.evaluate("window.continuedCount = (window.continuedCount || 0) + 1")'), event_loop).result(timeout=45)
                effects.append("consume")
                return {"completed": True}

            config = {"configurable": {"thread_id": mode}}
            graph = pause_graph(send, consume)
            cancelled = False
            try:
                opened = await browser.start(url)
                assert opened.ok, opened.error
                await asyncio.to_thread(graph.invoke, {"issued": False}, config)
                snapshot = await asyncio.to_thread(graph.get_state, config)
                assert snapshot.next == ("consume",)
                assert snapshot.values.get("issued") and not snapshot.values.get("completed")
                await asyncio.sleep(0.1)
                await execute('assert await page.evaluate("window.sentCount") == 1\n'
                              'assert await page.evaluate("window.continuedCount || 0") == 0', True)
                assert effects == ["send"]
                owned = descendants(await asyncio.to_thread(process_tree), browser.process.pid)
                assert len(owned) > 1, "Must inspect actual browser child processes"
                if mode == "cancel_while_paused":
                    raise asyncio.CancelledError
                await asyncio.to_thread(graph.update_state, config, {"reply": "compatibility-only"})
                result = await asyncio.to_thread(graph.invoke, None, config)
                assert result["completed"]
                assert effects == ["send", "consume"]
                assert not (await asyncio.to_thread(graph.get_state, config)).next
                await execute('assert await page.evaluate("window.sentCount") == 1\n'
                              'assert await page.evaluate("window.continuedCount") == 1', True)
            except asyncio.CancelledError:
                cancelled = True
            finally:
                await browser.close()
            alive = set(await asyncio.to_thread(process_tree)) & owned
            assert not alive, f"Owned processes leaked: {sorted(alive)}"
            assert browser.process.returncode is not None
            assert cancelled == (mode == "cancel_while_paused")
            report["cases"].append({"id": mode, "status": "passed", "effects": effects,
                                    "owned_process_count": len(owned), "remaining_owned_processes": [],
                                    "worker_exit_code": browser.process.returncode})
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(experiment(args.output))
    except Exception as error:
        report = {"status": "failed", "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "compatibility.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
