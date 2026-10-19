"""Native production resource transitions with deterministic browser I/O only."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2e_agent.domain import Suite
from e2e_agent.execution_graph import build_execution_graph, recursion_limit
from e2e_agent.execution_runtime import RunRuntime
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from e2e_agent.graph_state import initial_execution_state
from e2e_agent.resources import ScriptedProvider
from scripts.verify_graph_resources import private_scan_saver
from test_execution_graph import DeterministicServices, tiny_suite


def phone_suite():
    data = tiny_suite().model_dump()
    scene = data["scenarios"][0]
    scene["checkpoints"] = [scene["checkpoints"][0]]
    scene["actions"] = [{"id": "request-phone", "checkpoint_id": "first", "kind": "request_resource",
                         "resource_id": "phone", "intent": "Acquire test phone"}, scene["actions"][0]]
    data["scenarios"], data["rules"] = [scene], [data["rules"][0]]
    data["resources"] = [{"id": "phone", "kind": "phone", "run_id": "per-execution", "reusable": True}]
    data["budget"]["max_actions"] = 2
    return Suite.model_validate(data)


class NativeGraphResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_pause_clock_is_idempotent_and_closed_by_runtime_cleanup(self):
        services = DeterministicServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            runtime = RunRuntime(tiny_suite(), Path(temporary), Path("X:/unused-browser"))
            runtime.begin_native_pause()
            runtime.begin_native_pause()
            before = runtime.clock.active_seconds
            await asyncio.sleep(.04)
            self.assertEqual(runtime.clock.active_seconds, before)
            runtime.end_native_pause()
            runtime.end_native_pause()
            self.assertGreaterEqual(runtime.clock.wait_seconds, .03)
            runtime.begin_native_pause()
            await runtime.close()
            after = runtime.clock.active_seconds
            await asyncio.sleep(.02)
            self.assertGreater(runtime.clock.active_seconds, after)

    async def test_actual_production_graph_resumes_single_resource_request_without_replay(self):
        services, suite = DeterministicServices(), phone_suite()
        phone = "15500003333"
        provider = ScriptedProvider({"phone": phone})
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            original = RunRuntime.execute_one
            async def execute_resource(runtime, action, check_conditions, *, pending_resource=None):
                if action.kind == "request_resource":
                    self.assertEqual(pending_resource["run_id"], runtime.run_id)
                    self.assertEqual(pending_resource["scene_id"], runtime.session.scene_id)
                    self.assertEqual(pending_resource["step_id"], action.id)
                    self.assertEqual(pending_resource["ref"], action.resource_id)
                    await runtime.manager.request(action.resource_id, runtime.session.scene_id, action.id, action.intent)
                return await original(runtime, action, check_conditions)
            async def metadata(runtime, action):
                return {}
            with patch.object(RunRuntime, "execute_one", execute_resource), \
                 patch.object(RunRuntime, "resource_request_metadata", metadata):
                runtime = RunRuntime(suite, Path(temporary), Path("X:/unused-browser"), resource_provider=provider)
                await runtime.open()
                saver, bridge = MemorySaver(), AsyncNodeBridge()
                graph = build_execution_graph(runtime, bridge=bridge, checkpointer=saver, interrupt_resources=True)
                session = AsyncGraphSession(graph, bridge)
                config = {"configurable": {"thread_id": runtime.run_id}, "recursion_limit": recursion_limit(suite)}
                try:
                    await session.ainvoke(initial_execution_state(suite, runtime.run_id, redact=runtime.manager.redactor.data), config)
                    waiting = await session.aget_state(config)
                    self.assertEqual(waiting.next, ("await_resource",))
                    self.assertEqual(waiting.values["pending_resource"]["ref"], "phone")
                    self.assertTrue(waiting.values["pending_resource"]["logical_request_id"])
                    self.assertFalse(provider.requests)
                    context = waiting.values["context_id"]
                    before = runtime.clock.active_seconds
                    await asyncio.sleep(.03)
                    self.assertEqual(runtime.clock.active_seconds, before)
                    await session.ainvoke(None, config)
                    final = await session.aget_state(config)
                    self.assertFalse(final.next)
                    self.assertEqual(final.values["execution"]["status"], "passed")
                    self.assertEqual(final.values["action_count"], 2)
                    self.assertEqual(final.values["execution"]["scenarios"]["alpha"]["context_id"], context)
                    self.assertEqual(len(provider.requests), 1)
                    self.assertEqual(private_scan_saver(saver, [phone])["plaintext_resource_leaks"], 0)
                finally:
                    await runtime.close()
                self.assertEqual(session.active_workers, 0)
