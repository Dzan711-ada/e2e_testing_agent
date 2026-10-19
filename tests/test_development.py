"""The stage-0 workflow API contract, independent of browser installation."""
import unittest
from pathlib import Path
import tempfile

from scripts.verify_workflow_compat import pause_graph
from scripts.verify_stage import assert_configuration_unchanged, configuration_fingerprints


class ConfigurationPreservationTests(unittest.TestCase):
    def test_preserve_new_input_configuration_but_detect_changes_during_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".idea").mkdir()
            path = root / ".idea/workspace.xml"
            path.write_text("initial IDE state", encoding="utf-8")
            historical = configuration_fingerprints(root)
            path.write_text("user-updated IDE state before verification", encoding="utf-8")
            stage_input = configuration_fingerprints(root)
            self.assertNotEqual(historical, stage_input)
            assert_configuration_unchanged(stage_input, root)
            path.write_text("changed during verification", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workspace.xml"):
                assert_configuration_unchanged(stage_input, root)


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_update_and_resume_do_not_repeat_side_effect(self):
        effects = []

        def send(state):
            effects.append("issued")
            return {"issued": True}

        def consume(state):
            self.assertEqual(state["reply"], "provided")
            effects.append("continued")
            return {"completed": True}

        graph = pause_graph(send, consume)
        config = {"configurable": {"thread_id": "first"}}
        import asyncio
        await asyncio.to_thread(graph.invoke, {"issued": False}, config)
        snapshot = await asyncio.to_thread(graph.get_state, config)
        self.assertEqual(snapshot.next, ("consume",))
        self.assertEqual(effects, ["issued"])
        self.assertNotIn("completed", snapshot.values)
        await asyncio.to_thread(graph.update_state, config, {"reply": "provided"})
        final = await asyncio.to_thread(graph.invoke, None, config)
        self.assertTrue(final["completed"])
        self.assertEqual(effects, ["issued", "continued"])
        self.assertEqual((await asyncio.to_thread(graph.get_state, config)).next, ())

    async def test_checkpoint_threads_are_isolated(self):
        graph = pause_graph(lambda state: {"issued": True}, lambda state: {"completed": True})
        first = {"configurable": {"thread_id": "first"}}
        other = {"configurable": {"thread_id": "other"}}
        import asyncio
        await asyncio.to_thread(graph.invoke, {"issued": False}, first)
        self.assertEqual((await asyncio.to_thread(graph.get_state, other)).values, {})
        await asyncio.to_thread(graph.update_state, first, {"reply": "first-only"})
        self.assertNotIn("reply", (await asyncio.to_thread(graph.get_state, other)).values)


if __name__ == "__main__":
    unittest.main()
