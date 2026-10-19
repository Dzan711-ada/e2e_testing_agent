"""Privacy boundaries on the actual production graph, without browser/model IO."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Action, RepairPlan, Suite
from e2e_agent.diagnostics import Assessment
from e2e_agent.repairs import protected_fingerprint, required_validation_conditions
from e2e_agent.action_policy import redact_role_payload
from e2e_agent.execution_runtime import RunRuntime
from e2e_agent.graph_checkpoint import MemorySaver


PRIVATE = "PrivateR3Canary9Z6"


def public_suite():
    return Suite.model_validate({
        "id": "privacy-boundary", "query": "Verify the public ready state",
        "budget": {"max_actions": 6},
        "rules": [{"id": "ready", "statement": "The page is ready",
                   "source": {"kind": "user", "reference": "user:request"}}],
        "resources": [{"id": "password", "kind": "password", "reusable": True,
                       "run_id": "per-execution"}],
        "scenarios": [{"id": "login", "description": "Public login scene", "namespace": "login",
                       "start_url": "http://127.0.0.1/public", "initial_state": "empty",
                       "checkpoints": [{"id": "login", "description": "Public readiness check",
                           "acceptance": [{"rule_id": "ready", "target": "testid:ready",
                                           "operator": "equals", "expected": "ready"}]}],
                       "actions": [{"id": "submit-login", "kind": "click", "checkpoint_id": "login",
                                    "intent": "Submit the public form", "target": "testid:login-submit"}]}],
    })


def planning_suite():
    data = public_suite().model_dump()
    data["scenarios"][0]["actions"] = []
    return Suite.model_validate(data)


class BrowserFreeServices:
    """Replace external IO, retaining runtime resource policy and graph execution."""

    def __init__(self, *, fail_original=False):
        self.fail_original, self.actions, self.savers = fail_original, [], []

    def patched(self):
        services = self

        async def open_run(runtime):
            runtime.base = "http://127.0.0.1"
            return runtime

        async def open_scene(runtime, scene):
            runtime.session = SimpleNamespace(scene_id=scene.id, namespace=scene.namespace,
                context_id="public-context", executor=SimpleNamespace(events=[]), network=[],
                page=SimpleNamespace(url="http://127.0.0.1/public"))
            return runtime.session

        async def close_scene(runtime):
            runtime.session = None

        async def observe(runtime):
            return {"url": runtime.session.page.url, "candidates": {
                "testid:ready": [{"tag": "div", "text": "ready"}],
                "testid:login-submit": [{"tag": "button", "text": "Log in"}],
            }}

        async def execute_one(runtime, action, check_conditions):
            services.actions.append(action.model_dump())
            event = {"action_id": action.id, "checkpoint_id": action.checkpoint_id,
                     "kind": action.kind, "target": action.target, "status": "passed",
                     "side_effect_attempted": False, "url": runtime.session.page.url}
            runtime.session.executor.events.append(event)
            if services.fail_original and len(services.actions) == 1:
                event.update(status="error", error="Original target is unavailable")
                raise ValueError("Original target is unavailable")
            return event

        async def check_conditions(runtime, checkpoint, outcome):
            for condition in checkpoint.acceptance:
                outcome["evidence"].append({"id": "public-evidence", "rule_id": condition.rule_id,
                    "target": condition.target, "operator": condition.operator,
                    "expected": condition.expected, "actual": condition.expected, "status": "passed"})

        async def capture_checkpoint(runtime, checkpoint, outcome):
            outcome.update(context_id=runtime.session.context_id, observation=await observe(runtime),
                           url=runtime.session.page.url, screenshot=None)
            return outcome

        async def screenshot(runtime, path):
            return str(path)

        def saver_factory():
            saver = MemorySaver()
            services.savers.append(saver)
            return saver

        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {}))
        stack.enter_context(patch("e2e_agent.coordinator.MemorySaver", side_effect=saver_factory))
        for name, function in {"open": open_run, "open_scene": open_scene, "close_scene": close_scene,
            "observe": observe, "execute_one": execute_one, "check_conditions": check_conditions,
            "capture_checkpoint": capture_checkpoint, "screenshot": screenshot}.items():
            stack.enter_context(patch.object(RunRuntime, name, function))
        return stack


class GraphPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def assert_private_absent(self, directory, services, *values, private=PRIVATE):
        bodies = [repr((saver.storage, saver.writes)) for saver in services.savers]
        bodies += [path.read_text(encoding="utf-8") for path in Path(directory).rglob("*") if path.is_file()]
        bodies += [str(value) for value in values]
        self.assertTrue(services.savers, "The test must exercise the native MemorySaver")
        for body in bodies:
            self.assertFalse(private in body, "A private resource entered a checkpoint or evidence artifact")

    async def run_candidate(self, *, field, via, private=PRIVATE):
        suite = public_suite()
        original = suite.scenarios[0].actions[0]
        if via == "planner":
            suite = planning_suite()
        updates = {field: ("Use " + private if field == "intent" else
                           "text:" + private if field == "target" else private)}
        if field == "value":
            updates.update(kind="select", target="testid:ready")
        candidate = Action.model_validate({**original.model_dump(), **updates})

        async def planner(*args, **kwargs):
            return [candidate]

        async def strategy(*args, **kwargs):
            return candidate

        services = BrowserFreeServices()
        options = {"action_planner": planner} if via == "planner" else {"strategy": strategy}
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(suite, Path(temporary), Path("X:/unused-browser"),
                resources={"password": private}, **options)
            self.assert_private_absent(temporary, services, result, private=private)
            self.assertEqual(result["status"], "error", "An unsafe candidate must stop before dispatch")
            self.assertEqual(services.actions, [], "The browser service received a private candidate")

    async def test_planner_private_intent_cannot_enter_native_checkpoint(self):
        await self.run_candidate(field="intent", via="planner")

    async def test_planner_private_target_cannot_enter_native_checkpoint(self):
        await self.run_candidate(field="target", via="planner")

    async def test_planner_private_literal_value_cannot_enter_native_checkpoint(self):
        await self.run_candidate(field="value", via="planner")

    async def test_strategy_private_intent_cannot_enter_native_checkpoint(self):
        await self.run_candidate(field="intent", via="strategy")

    async def test_strategy_private_target_cannot_enter_native_checkpoint(self):
        await self.run_candidate(field="target", via="strategy")

    async def test_private_candidate_cannot_hide_inside_public_id_substrings(self):
        for field in ("id", "target", "value"):
            with self.subTest(field=field):
                await self.run_candidate(field=field, via="planner", private="private-login-Canary9Z6")

    async def test_repair_private_target_and_rejected_candidate_history_are_safe(self):
        suite = public_suite()
        candidate = suite.scenarios[0].actions[0].model_copy(update={"target": "text:" + PRIVATE})

        class Repair:
            def __init__(self):
                self.transport = SimpleNamespace(calls=[])

            async def assess(self, **kwargs):
                return {"evidence": [{"id": "public-evidence", "kind": "action"}]}, Assessment(
                    diagnosis={"attribution": "agent_execution", "evidence_ids": ["public-evidence"],
                               "explanation": "The operation failed before dispatch"})

            async def plan(self, **kwargs):
                return RepairPlan(scenario_id="login", action_id="submit-login", restart_checkpoint="login",
                    requirements_fingerprint=suite.requirements_fingerprint(),
                    protected_data_fingerprint=protected_fingerprint(suite), evidence_ids=["public-evidence"],
                    validation_conditions=required_validation_conditions(suite), attempt=1, proposed_actions=[candidate])

            def validate(self, *args, **kwargs):
                raise AssertionError("A private proposal must be refused before graph checkpoint/policy validation")

            def publish(self, history, **kwargs):
                return redact_role_payload(history, kwargs["redact"], public_ids=["login", "submit-login"],
                                           public_locators=kwargs.get("public_locators", ()))

            async def propose(self, **kwargs):
                raise AssertionError("Production graph must not call the bundled compatibility scheduler")

        services = BrowserFreeServices(fail_original=True)
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(suite, Path(temporary), Path("X:/unused-browser"),
                resources={"password": PRIVATE}, repair_controller=Repair())
            self.assert_private_absent(temporary, services, result)
            self.assertEqual(result["status"], "error")
            self.assertEqual(len(services.actions), 1, "Only the safe original action may be attempted")

    async def test_planner_exception_cannot_echo_rejected_private_candidate(self):
        async def planner(*args, **kwargs):
            raise ValueError("Rejected candidate input_value={'intent': '" + PRIVATE + "'}")

        services = BrowserFreeServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(planning_suite(), Path(temporary), Path("X:/unused-browser"),
                resources={"password": PRIVATE}, action_planner=planner)
            self.assertEqual(result["status"], "error")
            self.assertEqual(services.actions, [])
            self.assert_private_absent(temporary, services, result)

    async def test_pydantic_rejection_cannot_persist_escaped_private_input(self):
        prefix = "EscapedR3Canary9Z6"
        private = prefix + "'\\\"tail"

        async def planner(*args, **kwargs):
            # The rejected value is repr-escaped by Pydantic before graph error
            # handling. Redacting only its original spelling is insufficient.
            return [Action.model_validate({"id": "invalid", "kind": "observe",
                "checkpoint_id": "login", "intent": "Public intent", "extra_value": private})]

        services = BrowserFreeServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(planning_suite(), Path(temporary), Path("X:/unused-browser"),
                resources={"password": private}, action_planner=planner)
            self.assertEqual(result["status"], "error")
            self.assertEqual(services.actions, [])
            self.assert_private_absent(temporary, services, result, private=prefix)

    async def test_ordinary_exception_cannot_persist_private_repr(self):
        prefix = "ReprR3Canary9Z6"
        private = prefix + "'\\\"tail"

        async def planner(*args, **kwargs):
            raise ValueError(f"Planner rejected action candidate {private!r}")

        services = BrowserFreeServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(planning_suite(), Path(temporary), Path("X:/unused-browser"),
                resources={"password": private}, action_planner=planner)
            self.assertEqual(result["status"], "error")
            self.assertEqual(services.actions, [])
            self.assertIn("Planner rejected action candidate", result["scenarios"]["login"]["checkpoints"][0]["error"])
            self.assert_private_absent(temporary, services, result, private=prefix)

    async def test_public_control_ids_still_execute_when_resource_has_same_spelling(self):
        services = BrowserFreeServices()
        with tempfile.TemporaryDirectory() as temporary, services.patched():
            result = await execute_suite(public_suite(), Path(temporary), Path("X:/unused-browser"),
                resources={"password": "login"})
            self.assertEqual(result["status"], "passed")
            self.assertEqual(services.actions[0]["id"], "submit-login")
            self.assertEqual(services.actions[0]["checkpoint_id"], "login")
            self.assertEqual(services.actions[0]["target"], "testid:login-submit")
            for saver in services.savers:
                self.assertIs(type(saver), MemorySaver)
            state = json.loads((Path(temporary) / "execution-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["scene_results"]["login"]["checkpoints"][0]["id"], "login")

if __name__ == "__main__":
    unittest.main()
