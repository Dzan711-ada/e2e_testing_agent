from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from e2e_agent.agent_workflow import combined_outcome
from e2e_agent.domain import Action, RepairPlan, Suite
from e2e_agent.repairs import RepairController, RepairRejected, equivalent_targets, protected_fingerprint, required_validation_conditions, validate_repair
from e2e_agent.suite_demo import shop_suite


class RepairPolicyTests(unittest.TestCase):
    def setUp(self):
        self.suite = shop_suite()
        self.scene = self.suite.scenarios[0]
        self.checkpoint = self.scene.checkpoints[1]
        self.action = next(a for a in self.scene.actions if a.id == "submit-login")
        self.observation = {"candidates": {"testid:login-current": [{"tag": "button", "text": "Log in", "form_testid": "login-form"}],
                                           "button:Log in": [{"tag": "button", "text": "Log in", "form_testid": "login-form"},
                                                             {"tag": "button", "text": "Log in", "form_testid": None}]}}
        self.event = {"status": "error", "side_effect_attempted": False}
        self.plan = RepairPlan(scenario_id=self.scene.id, action_id=self.action.id,
            requirements_fingerprint=self.suite.requirements_fingerprint(), protected_data_fingerprint=protected_fingerprint(self.suite),
            evidence_ids=["ev-1"], proposed_actions=[self.action.model_copy(update={"target": "testid:login-current"})],
            restart_checkpoint="login", attempt=1, validation_conditions=required_validation_conditions(self.suite))

    def validate(self, plan=None, **kwargs):
        return validate_repair(plan or self.plan, self.suite, self.scene, self.checkpoint, self.action,
            self.observation, kwargs.pop("event", self.event), attempt=kwargs.pop("attempt", 1),
            scene_attempts=kwargs.pop("scene_attempts", 1), evidence_ids=["ev-1"], **kwargs)

    def test_unique_equivalent_control_is_allowed_and_multi_match_is_not(self):
        self.assertEqual(equivalent_targets(self.suite, self.scene, self.checkpoint, self.action, self.observation), ["testid:login-current"])
        self.assertEqual(self.validate()[0].target, "testid:login-current")
        for target in ("button:Log in", "css:button:first-child", "https://outside.test", "testid:unobserved"):
            with self.subTest(target=target), self.assertRaises(RepairRejected):
                self.validate(self.plan.model_copy(update={"proposed_actions": (self.action.model_copy(update={"target": target}),)}))

    def test_cannot_change_operations_inputs_resources_or_skip_checks(self):
        for field, value in (("kind", "observe"), ("resource_id", "other-account"), ("value", "replacement-password"),
                             ("checkpoint_id", "cart"), ("intent", "Skip login"), ("preconditions", ()), ("id", "new-action")):
            if getattr(self.action, field) == value:
                continue
            changed = self.plan.proposed_actions[0].model_copy(update={field: value})
            with self.subTest(field=field), self.assertRaises(RepairRejected):
                self.validate(self.plan.model_copy(update={"proposed_actions": (changed,)}))
        for forbidden in ("remove_assertion", "expected", "skip_failure", "budget", "rules"):
            data = self.plan.model_dump()
            data[forbidden] = True
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                RepairPlan.model_validate(data)

    def test_protected_digest_covers_data_scope_and_budget_beyond_requirements(self):
        for change in ("resource", "budget", "scope", "input", "check"):
            data = self.suite.model_dump()
            if change == "resource": data["resources"][0]["reusable"] = True
            if change == "budget": data["budget"]["max_actions"] = 41
            if change == "scope": data["scenarios"][0]["start_url"] = "https://outside.test"
            if change == "input": data["scenarios"][0]["actions"][1]["resource_id"] = "password"
            if change == "check": data["scenarios"][0]["checkpoints"][2]["acceptance"][0]["expected"] = "2"
            candidate = Suite.model_validate(data)
            with self.subTest(change=change):
                self.assertNotEqual(protected_fingerprint(candidate), protected_fingerprint(self.suite))
                if change != "check": self.assertEqual(candidate.requirements_fingerprint(), self.suite.requirements_fingerprint())

    def test_unknown_or_completed_side_effect_never_repeats(self):
        for event in ({"status": "error"}, {"status": "error", "side_effect_attempted": True}, {"status": "passed", "side_effect_attempted": False}):
            with self.subTest(event=event), self.assertRaises(RepairRejected):
                self.validate(event=event)

    def test_restart_evidence_and_attempt_identity_cannot_change(self):
        for change in ({"restart_checkpoint": "registration"}, {"scenario_id": "wrong-password"}, {"action_id": "register"},
                       {"requirements_fingerprint": "changed"}, {"protected_data_fingerprint": "0" * 64}, {"evidence_ids": ("invented",)},
                       {"attempt": 2}, {"validation_conditions": self.plan.validation_conditions[:-1]},
                       {"validation_conditions": ("Skip the failing check",)}):
            with self.subTest(change=change), self.assertRaises(RepairRejected):
                self.validate(self.plan.model_copy(update=change))
        with self.assertRaises(RepairRejected): self.validate(scene_attempts=5)
        with self.assertRaises(RepairRejected): self.validate(self.plan.model_copy(update={"attempt": 3}), attempt=3)

    def test_wait_is_one_bounded_step_not_a_loop_or_budget_change(self):
        wait = Action(id="bounded-wait", checkpoint_id="login", kind="wait", target="testid:login-current", intent="Wait for the same target")
        self.validate(self.plan.model_copy(update={"proposed_actions": (wait, *self.plan.proposed_actions)}))
        for bad in (wait.model_copy(update={"value": 999999}), wait.model_copy(update={"kind": "navigate", "value": "https://outside.test"}),
                    wait.model_copy(update={"target": "testid:different"}), wait.model_copy(update={"id": self.action.id})):
            with self.subTest(bad=bad.kind), self.assertRaises(RepairRejected):
                self.validate(self.plan.model_copy(update={"proposed_actions": (bad, *self.plan.proposed_actions)}))
        with self.assertRaises(RepairRejected):
            self.validate(self.plan.model_copy(update={"proposed_actions": (wait, wait, *self.plan.proposed_actions)}))

    def test_repair_and_stability_do_not_overwrite_original_failure(self):
        first = {"status": "error", "initial_status": "error"}
        frozen = deepcopy(first)
        result = combined_outcome(first, "passed")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stability"], "inconsistent")
        self.assertEqual(first, frozen)
        repaired = combined_outcome({"status": "passed", "initial_status": "error", "repaired": True, "repair_count": 1}, "passed")
        self.assertEqual(repaired, {"status": "passed", "stability": "inconsistent", "repaired": True, "repair_count": 1})


class RepairControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_repair_or_model_budget_stops_before_a_new_call(self):
        from e2e_agent.config import Settings
        class Never:
            async def call(self, *args):
                raise AssertionError("Exhausted budget reached a model")
        for exhausted in ("repair", "model"):
            data = shop_suite().model_dump()
            model = Never()
            model.calls = []
            if exhausted == "repair": data["budget"]["max_repairs_per_failure"] = 0
            else: model.calls = [{}] * data["budget"]["max_model_calls"]
            suite = Suite.model_validate(data)
            scene = suite.scenarios[0]
            with tempfile.TemporaryDirectory() as folder:
                controller = RepairController(Settings(), model, Path(folder))
                result = await controller.propose(suite=suite, scene=scene, checkpoint=scene.checkpoints[1], original=scene.actions[7],
                    observation={}, failed_event={"status": "error", "side_effect_attempted": False}, execution={}, redact=lambda x: x)
                self.assertIsNone(result)
                self.assertIn("budget exhausted", controller.history[0]["reason"])
                self.assertFalse(controller.attempts)

    async def test_uncertain_dispatch_stops_before_any_model_call(self):
        from e2e_agent.config import Settings
        class Never:
            calls = []
            async def call(self, *args):
                raise AssertionError("Unsafe repeat reached a model")
        suite = shop_suite()
        scene = suite.scenarios[0]
        with tempfile.TemporaryDirectory() as folder:
            controller = RepairController(Settings(), Never(), Path(folder))
            result = await controller.propose(suite=suite, scene=scene, checkpoint=scene.checkpoints[1], original=scene.actions[7],
                observation={}, failed_event={"status": "error", "side_effect_attempted": True}, execution={}, redact=lambda x: x)
            self.assertIsNone(result)
            self.assertFalse(controller.transport.calls)
            self.assertIn("uncertain", controller.history[0]["reason"])
            self.assertEqual(json.loads((Path(folder) / "attempt-001.json").read_text())["decision"], "stopped")
