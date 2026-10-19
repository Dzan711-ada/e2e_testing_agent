"""Actual native execution graph owns diagnosis, proposal and repair policy."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2e_agent.config import Settings
from e2e_agent.domain import Action, Suite
from e2e_agent.execution_runtime import RunRuntime
from e2e_agent.repairs import RepairController
from e2e_agent.resources import ResourceUnavailable
from e2e_agent.coordinator import execute_suite
from test_graph_privacy import BrowserFreeServices


def repair_suite(*, action_budget=6, repair_budget=2):
    return Suite.model_validate({
        "id": "split-repair", "query": "Verify login", "fixture": "local_shop",
        "budget": {"max_actions": action_budget, "max_repairs_per_failure": repair_budget},
        "resources": [{"id": "password", "kind": "password", "run_id": "per-execution", "reusable": True}],
        "rules": [{"id": "login", "statement": "Successful login is visible",
                   "source": {"kind": "contract", "reference": "contract:shop-v1"}}],
        "scenarios": [{"id": "main", "description": "Independent login", "namespace": "main",
            "start_url": "{base}/shop/{namespace}", "checkpoints": [{"id": "login", "description": "Login check",
                "acceptance": [{"rule_id": "login", "target": "testid:identity", "operator": "visible", "expected": True}]}],
            "actions": [{"id": "submit-login", "checkpoint_id": "login", "kind": "click", "intent": "submit-login",
                         "target": "testid:login-old"}]}],
    })


class RoleFixture:
    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, []
        self.after_diagnose = None

    async def call(self, role, schema, system, payload):
        self.calls.append({"role": role, "system": system, "payload": payload})
        if role == "diagnose":
            category = "suspected_product" if self.mode == "product" else "agent_execution"
            decisive = next(item["id"] for item in payload["evidence"]
                            if item["kind"] == ("observation" if category == "suspected_product" else "action"))
            if self.after_diagnose:
                self.after_diagnose()
            return schema.model_validate({"diagnosis": {"attribution": category, "evidence_ids": [decisive],
                "explanation": "Recorded evidence supports this bounded classification"}})
        original = Action.model_validate(payload["original_action"])
        changed = original.model_copy(update={"target": payload["allowed_equivalent_targets"][0]})
        if self.mode == "malicious":
            changed = changed.model_copy(update={"kind": "observe"})
        return schema.model_validate({"scenario_id": payload["scenario_id"],
            "action_id": "PrivateRepair9Z6" if self.mode == "private_metadata" else original.id,
            "restart_checkpoint": payload["checkpoint_id"], "attempt": payload["attempt"],
            "requirements_fingerprint": payload["requirements_fingerprint"],
            "protected_data_fingerprint": payload["protected_data_fingerprint"],
            "validation_conditions": payload["required_validation_conditions"],
            "evidence_ids": [payload["evidence"]["evidence"][0]["id"]], "proposed_actions": [changed.model_dump()]})


class NativeRepairGraphTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, mode="normal", dispatch=False, always_fail=False,
                       resource=False, action_budget=6, repair_budget=2, active_boundary=None, resources=None):
        suite = repair_suite(action_budget=action_budget, repair_budget=repair_budget)
        model, services = RoleFixture(mode), BrowserFreeServices()
        active_runtime = []
        if active_boundary == "plan":
            def exhaust_clock():
                active_runtime[0].clock.started -= 1000
            model.after_diagnose = exhaust_clock

        async def observe(runtime):
            return {"url": runtime.session.page.url, "candidates": {"testid:login-current": [
                {"tag": "button", "text": "Log in", "form_testid": "login-form"}]}}

        async def execute(runtime, action, conditions):
            active_runtime[:] = [runtime]
            services.actions.append(action.model_dump())
            failed = always_fail or len(services.actions) == 1
            event = {"action_id": action.id, "checkpoint_id": action.checkpoint_id, "kind": action.kind,
                     "target": action.target, "status": "error" if failed else "passed",
                     "side_effect_attempted": dispatch, "url": runtime.session.page.url}
            runtime.session.executor.events.append(event)
            if resource:
                raise ResourceUnavailable("Resource is expired")
            if failed:
                if active_boundary == "diagnose":
                    runtime.clock.started -= 1000
                raise ValueError("The observed target is unavailable")
            return event

        with tempfile.TemporaryDirectory() as temporary, services.patched(), ExitStack() as stack:
            stack.enter_context(patch.object(RunRuntime, "observe", observe))
            stack.enter_context(patch.object(RunRuntime, "execute_one", execute))
            controller = RepairController(Settings(), model, Path(temporary) / "repairs")
            stack.enter_context(patch.object(controller, "propose", side_effect=AssertionError("Bundled scheduler called")))
            result = await execute_suite(suite, Path(temporary) / "run", Path("X:/unused-browser"),
                                         repair_controller=controller, resources=resources)
            state = json.loads((Path(temporary) / "run/execution-state.json").read_text(encoding="utf-8"))
            events = json.loads((Path(temporary) / "run/execution-events.json").read_text(encoding="utf-8"))
            self.assertEqual(controller.attempts, {}, "Production progress must not live in service counters")
            self.assertEqual(controller.scene_attempts, {})
            self.assertEqual(result["repair_history"], controller.history)
            self.assertEqual(result["requirements_fingerprint"], suite.requirements_fingerprint())
            self.assertEqual(state["requirements_fingerprint"], suite.requirements_fingerprint())
            if mode == "private_metadata":
                for saver in services.savers:
                    self.assertFalse("PrivateRepair9Z6" in repr((saver.storage, saver.writes)))
                for path in Path(temporary).rglob("*"):
                    if path.is_file():
                        self.assertFalse("PrivateRepair9Z6" in path.read_text(encoding="utf-8"))
            return result, state, events, services.actions, model.calls

    async def test_real_graph_separates_diagnosis_execution_proposal_and_policy(self):
        result, state, events, actions, calls = await self.exercise()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["initial_status"], "error")
        self.assertEqual(result["stability"], "inconsistent")
        self.assertEqual(result["repair_count"], 1)
        self.assertEqual(state["repair_attempts"], {"main:submit-login": 1})
        self.assertEqual(state["scene_repair_attempts"], {"main": 1})
        self.assertEqual([item["role"] for item in calls], ["diagnose", "repair"])
        self.assertIn("You are the execution Agent", calls[-1]["system"])
        path = [item["node"] for item in events]
        start = path.index("diagnose_failure")
        self.assertEqual(path[start:start + 4], ["diagnose_failure", "plan_repair", "validate_repair", "execute_action"])
        self.assertEqual([action["target"] for action in actions], ["testid:login-old", "testid:login-current"])
        self.assertEqual(result["initial_failures"][0]["action"]["target"], "testid:login-old")
        self.assertEqual(state["effective_actions"]["main"][0]["target"], "testid:login-current")
        self.assertEqual(state["repair_history"][0]["execution_status"], "passed")

    async def test_malicious_proposal_reaches_policy_and_cannot_reach_dispatch(self):
        result, state, events, actions, calls = await self.exercise(mode="malicious")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["repaired"])
        self.assertEqual(len(actions), 1)
        self.assertIn("changed operation", state["repair_history"][0]["reason"])
        path = [item["node"] for item in events]
        self.assertIn("validate_repair", path)
        self.assertEqual(path[path.index("validate_repair") + 1], "complete_checkpoint")

    async def test_uncertain_side_effect_stops_before_either_model_role(self):
        result, state, events, actions, calls = await self.exercise(dispatch=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(state["repair_attempts"], {})
        self.assertIn("uncertain", state["repair_history"][0]["reason"])
        self.assertNotIn("plan_repair", [item["node"] for item in events])

    async def test_resource_failure_never_enters_repair_or_changes_lifetime(self):
        result, state, events, actions, calls = await self.exercise(resource=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(calls, [])
        self.assertEqual(len(actions), 1)
        self.assertEqual(state["repair_history"], [])
        self.assertNotIn("diagnose_failure", [item["node"] for item in events])

    async def test_product_diagnosis_stops_before_execution_agent_proposal(self):
        result, state, events, actions, calls = await self.exercise(mode="product")
        self.assertEqual(result["status"], "error")
        self.assertEqual([item["role"] for item in calls], ["diagnose"])
        self.assertEqual(len(actions), 1)
        self.assertNotIn("plan_repair", [item["node"] for item in events])
        self.assertIn("does not support", state["repair_history"][0]["reason"])

    async def test_graph_owned_repair_budget_stops_after_exactly_two_attempts(self):
        result, state, events, actions, calls = await self.exercise(always_fail=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["repair_count"], 2)
        self.assertEqual(state["repair_attempts"], {"main:submit-login": 2})
        self.assertEqual(len(actions), 3)
        self.assertEqual([item["role"] for item in calls], ["diagnose", "repair", "diagnose", "repair"])
        self.assertIn("budget exhausted", state["repair_history"][-1]["reason"])
        self.assertEqual(sum(item["decision"] == "approved" for item in state["repair_history"]), 2)

    async def test_action_budget_policy_refuses_proposal_before_replacement(self):
        result, state, events, actions, calls = await self.exercise(action_budget=1)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(actions), 1)
        self.assertIn("action budget", state["repair_history"][0]["reason"])
        path = [item["node"] for item in events]
        self.assertEqual(path[path.index("validate_repair") + 1], "complete_checkpoint")

    async def test_active_budget_stops_at_both_model_boundaries(self):
        for boundary in ("diagnose", "plan"):
            with self.subTest(boundary=boundary):
                result, state, events, actions, calls = await self.exercise(active_boundary=boundary)
                self.assertEqual(result["status"], "error")
                self.assertEqual(len(actions), 1)
                self.assertEqual([item["role"] for item in calls], [] if boundary == "diagnose" else ["diagnose"])
                self.assertIn("active execution budget", state["repair_history"][0]["reason"])
                self.assertEqual(state["repair_attempts"], {})

    async def test_private_repair_plan_metadata_is_rejected_before_checkpoint(self):
        result, state, events, actions, calls = await self.exercise(mode="private_metadata",
            resources={"password": "PrivateRepair9Z6"})
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(actions), 1)
        self.assertIsNone(state["repair_proposal"])
        self.assertIn("private resource", state["repair_history"][0]["reason"])
        self.assertNotIn("validate_repair", [item["node"] for item in events])

    async def test_public_collision_survives_diagnosis_proposal_and_policy(self):
        result, state, events, actions, calls = await self.exercise(resources={"password": "login"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(state["repair_assessment"]["diagnosis"]["attribution"], "agent_execution")
        self.assertEqual(state["repair_proposal"]["restart_checkpoint"], "login")
        self.assertEqual(actions[-1]["target"], "testid:login-current")
        self.assertEqual(calls[-1]["payload"]["original_action"]["intent"], "submit-login")


if __name__ == "__main__":
    unittest.main()
