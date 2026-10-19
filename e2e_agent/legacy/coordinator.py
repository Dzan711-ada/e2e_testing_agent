"""Execution entry point: invoke the graph, resume native pauses, clean services."""
import asyncio
import json
from pathlib import Path

from .domain import Suite
from .action_policy import safe_validation_message
from .execution_graph import build_execution_graph, recursion_limit
from .execution_runtime import RunRuntime, aggregate
from .graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from .graph_state import initial_execution_state, redact_execution_record


async def execute_suite(suite: Suite, output_dir: Path, browsers_path: Path, *, resources=None, fixture=None,
                        faults=None, headless=True, strategy=None, resource_provider=None,
                        resource_wait_timeout=120, public_fictional=False, external_resources=(), source_catalog=None, action_planner=None,
                        repair_controller=None):
    runtime = RunRuntime(suite, output_dir, browsers_path, resources=resources, fixture=fixture,
                         faults=faults, headless=headless, resource_provider=resource_provider,
                         resource_wait_timeout=resource_wait_timeout, public_fictional=public_fictional,
                         external_resources=external_resources, source_catalog=source_catalog)
    manager = runtime.manager
    state = {}
    try:
        state = initial_execution_state(suite, runtime.run_id, redact=manager.redactor.data)
        await runtime.open()
        try:
            bridge = AsyncNodeBridge()
            graph = build_execution_graph(runtime, bridge=bridge, action_planner=action_planner,
                strategy=strategy, repair_controller=repair_controller, checkpointer=MemorySaver())
            session = AsyncGraphSession(graph, bridge)
            config = {"configurable": {"thread_id": runtime.run_id}, "recursion_limit": recursion_limit(suite)}
            value = state
            # The driver only resumes native interrupt boundaries. Graph nodes
            # own scene/action selection, failures, retry and resource routing.
            while True:
                async for update in session.astream(value, config, stream_mode="values"):
                    state = update
                snapshot = await session.aget_state(config)
                if not snapshot.next:
                    break
                value = None
        finally:
            await runtime.close(clear_resources=False)
        record = dict(state["execution"])
        record["timing"] = {"active_seconds": runtime.clock.active_seconds,
                            "human_wait_seconds": runtime.clock.wait_seconds}
    except BaseException as error:
        partial = redact_execution_record({"run_id": runtime.run_id, "scenarios": state.get("scene_results", {}),
            "status": "cancelled" if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)) else "error",
            "error": safe_validation_message(error), "complete": False}, manager.redactor.data,
            public_locators=state.get("public_locators", ()), public_ids=state.get("public_ids", ()))
        (output_dir / "partial-suite.json").write_text(json.dumps(partial, indent=2), encoding="utf-8")
        safe_message = manager.redactor.text(f"{type(error).__name__}: {safe_validation_message(error)}")
        manager.close()
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
            raise
        raise RuntimeError(safe_message) from None
    record = redact_execution_record(record, manager.redactor.data,
        public_locators=state.get("public_locators", ()), public_ids=state.get("public_ids", ()))
    (output_dir / "suite-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    manager.close()
    return record
