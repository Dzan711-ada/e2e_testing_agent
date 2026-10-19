"""Exercise the production graph with deterministic runtime services and no browser."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Suite
from e2e_agent.evidence import condition_fingerprint
from e2e_agent.execution_runtime import RunRuntime
from e2e_agent.suite_demo import shop_suite
from scripts.verify_execution_graph import assert_requirements_preserved, read_actual_graph


def tiny_suite():
    return Suite.model_validate({
        "id": "offline-graph", "query": "Verify dependent checkpoints in two isolated scenes", "fixture": "local_shop",
        "budget": {"max_actions": 4},
        "rules": [{"id": identity, "statement": "The checkpoint is ready",
                   "source": {"kind": "contract", "reference": "sample_shop.CONTRACT.v1"}} for identity in ("first", "second")],
        "scenarios": [{"id": scene, "description": scene, "namespace": scene, "start_url": "{base}/shop/{namespace}",
                       "checkpoints": [{"id": identity, "description": identity, "depends_on": [] if identity == "first" else ["first"],
                                        "acceptance": [{"rule_id": identity, "target": "testid:ready", "operator": "equals", "expected": "ready"}]}
                                       for identity in ("first", "second")],
                       "actions": [{"id": "observe-" + identity, "checkpoint_id": identity, "kind": "observe", "intent": "Observe readiness"}
                                   for identity in ("first", "second")]} for scene in ("alpha", "beta")],
    })


class DeterministicServices:
    def __init__(self, *, failure=None):
        self.calls, self.failure = [], failure

    def patched(self):
        services = self

        async def open_run(runtime):
            runtime.base = "http://127.0.0.1:12345"
            services.calls.append(("open", runtime.run_id))
            return runtime

        async def open_scene(runtime, scenario):
            if runtime.session is not None:
                raise AssertionError("Graph opened another scene before closing the current one")
            session = SimpleNamespace(scene_id=scenario.id, namespace=runtime.run_id + "_" + scenario.namespace,
                                      context_id=runtime.run_id + "_context_" + scenario.id,
                                      executor=SimpleNamespace(events=[]), network=[], page=SimpleNamespace(url="http://127.0.0.1:12345/shop/test"))
            runtime.session = session
            services.calls.append(("open_scene", scenario.id))
            return session

        async def close_scene(runtime):
            if runtime.session is not None:
                services.calls.append(("close_scene", runtime.session.scene_id))
                runtime.session = None

        async def observe(runtime):
            return {"url": runtime.session.page.url, "candidates": {"testid:ready": [{"tag": "div", "text": "ready"}]}}

        async def execute_one(runtime, action, check_conditions):
            session = runtime.session
            services.calls.append(("action", session.scene_id, action.checkpoint_id, action.id))
            event = {"action_id": action.id, "checkpoint_id": action.checkpoint_id, "kind": action.kind,
                     "status": "passed", "side_effect_attempted": False, "url": session.page.url}
            session.executor.events.append(event)
            return event

        async def check_conditions(runtime, checkpoint, outcome):
            services.calls.append(("verify", runtime.session.scene_id, checkpoint.id))
            fail = services.failure == (runtime.session.scene_id, checkpoint.id)
            for condition in checkpoint.acceptance:
                outcome["evidence"].append({"id": runtime.session.scene_id + ":" + checkpoint.id,
                    "rule_id": condition.rule_id, "target": condition.target, "operator": condition.operator,
                    "expected": condition.expected, "actual": "failed" if fail else condition.expected,
                    "condition_fingerprint": condition_fingerprint(condition), "status": "failed" if fail else "passed"})
                if fail:
                    raise AssertionError("Deterministic acceptance failed")

        async def capture_checkpoint(runtime, checkpoint, outcome):
            outcome.update(context_id=runtime.session.context_id, observation=await observe(runtime),
                           url=runtime.session.page.url, screenshot=None)
            return outcome

        async def screenshot(runtime, path):
            return str(path)

        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {}))
        for name, function in {"open": open_run, "open_scene": open_scene, "close_scene": close_scene,
                               "observe": observe, "execute_one": execute_one, "check_conditions": check_conditions,
                               "capture_checkpoint": capture_checkpoint, "screenshot": screenshot}.items():
            stack.enter_context(patch.object(RunRuntime, name, function))
        return stack


class ProductionExecutionGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_directory_rerun_archives_old_audit_and_keeps_current_run_isolated(self):
        suite = tiny_suite()
        services = DeterministicServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            directory = Path(temporary)
            first = await execute_suite(suite, directory, Path("X:/unused-browser"))
            first_state, first_events, first_evidence = read_actual_graph(directory, first)
            original_bytes = {Path(item["path"]).name: Path(item["path"]).read_bytes() for item in first_evidence.values()}
            second = await execute_suite(suite, directory, Path("X:/unused-browser"))
            second_state, second_events, _ = read_actual_graph(directory, second)
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(second["status"], "passed")
            assert_requirements_preserved(suite, first)
            assert_requirements_preserved(suite, second)
            self.assertEqual({event["run_id"] for event in second_events}, {second["run_id"]})
            self.assertEqual(second_state["run_id"], second["run_id"])
            archived = [path.parent for path in (directory / "execution-history").glob("*/execution-state.json")
                        if json.loads(path.read_text(encoding="utf-8"))["run_id"] == first["run_id"]]
            self.assertEqual(len(archived), 1)
            restored_state, restored_events, _ = read_actual_graph(archived[0], first)
            self.assertEqual(restored_state, first_state)
            self.assertEqual(restored_events, first_events)
            self.assertEqual(restored_state["execution"]["scenarios"], first["scenarios"])
            for filename, content in original_bytes.items():
                self.assertEqual((archived[0] / filename).read_bytes(), content)

    def test_resource_words_cannot_corrupt_public_control_ids_or_materialized_requirements(self):
        from e2e_agent.execution_graph import materialize_suite
        from e2e_agent.graph_state import initial_execution_state
        suite = shop_suite()
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}):
            runtime = RunRuntime(suite, Path(temporary), Path("X:/unused-browser"),
                                 resources={"username": "login", "password": "password", "wrong_password": "registration"})
            try:
                state = initial_execution_state(suite, runtime.run_id, redact=runtime.manager.redactor.data)
                self.assertEqual(state["scene_order"], [scene.id for scene in suite.scenarios])
                self.assertEqual(state["checkpoint_order"]["main-chain"], [checkpoint.id for checkpoint in suite.scenarios[0].checkpoints])
                password = next(action for action in state["effective_actions"]["main-chain"] if action["id"] == "password")
                self.assertEqual(password["kind"], "fill")
                self.assertEqual(password["resource_id"], "password")
                self.assertEqual(password["checkpoint_id"], "login")
                restored = materialize_suite(runtime, state)
                self.assertEqual(restored.model_dump(), suite.model_dump())
                self.assertEqual(restored.requirements_fingerprint(), suite.requirements_fingerprint())
                # Common words are valid public IDs too: a global substring scan
                # would falsely classify these intentionally public references.
                self.assertEqual(state["budget"], suite.budget.model_dump())
            finally:
                runtime.manager.close()

    async def test_each_action_advances_actual_graph_state_and_preserves_scene_context(self):
        suite = tiny_suite()
        services = DeterministicServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            directory = Path(temporary)
            result = await execute_suite(suite, directory, Path("X:/unused-browser"))
            self.assertEqual(result["status"], "passed")
            state, events, _ = read_actual_graph(directory, result)
            assert_requirements_preserved(suite, result)
            steps = [event for event in events if event["node"] == "execute_action"]
            self.assertEqual([event["action_count"] for event in steps], [1, 2, 3, 4])
            self.assertEqual([(event["scene_id"], event["checkpoint_id"]) for event in steps],
                             [("alpha", "first"), ("alpha", "second"), ("beta", "first"), ("beta", "second")])
            self.assertEqual(state["action_count"], 4)
            for identity in ("alpha", "beta"):
                contexts = {event["context_id"] for event in steps if event["scene_id"] == identity}
                self.assertEqual(contexts, {result["scenarios"][identity]["context_id"]})
            self.assertEqual(sum(event["node"] == "complete_checkpoint" for event in events), 4)
        self.assertEqual([call for call in services.calls if call[0] == "close_scene"], [("close_scene", "alpha"), ("close_scene", "beta")])

    async def test_failed_checkpoint_blocks_dependent_actions_and_keeps_other_scene_running(self):
        suite = tiny_suite()
        services = DeterministicServices(failure=("alpha", "first"))
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            directory = Path(temporary)
            result = await execute_suite(suite, directory, Path("X:/unused-browser"))
            self.assertEqual(result["status"], "failed")
            _, events, _ = read_actual_graph(directory, result)
            assert_requirements_preserved(suite, result)
            outcomes = {item["id"]: item for item in result["scenarios"]["alpha"]["checkpoints"]}
            self.assertEqual(outcomes["first"]["status"], "failed")
            self.assertEqual(outcomes["second"]["status"], "blocked")
            self.assertEqual(outcomes["second"]["blocked_by"], "first")
            self.assertEqual(result["scenarios"]["beta"]["status"], "passed")
            steps = [event for event in events if event["node"] == "execute_action"]
            self.assertEqual([(event["scene_id"], event["checkpoint_id"]) for event in steps],
                             [("alpha", "first"), ("beta", "first"), ("beta", "second")])
        self.assertEqual([call for call in services.calls if call[0] == "close_scene"], [("close_scene", "alpha"), ("close_scene", "beta")])
