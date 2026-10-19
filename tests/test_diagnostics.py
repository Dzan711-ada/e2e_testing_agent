import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from e2e_agent.config import Settings
from e2e_agent.diagnostics import Assessment, diagnose_execution, validate_assessment
from e2e_agent.domain import Diagnosis
from e2e_agent.evidence import archive_replay, build_bundle, condition_fingerprint, model_bundle, public_url, review_coverage
from e2e_agent.suite_demo import shop_suite
from scripts.diagnostic_fixtures import fixed_bundle


def designed_data():
    data = shop_suite().model_dump()
    data["budget"]["max_actions"] = 16
    for scene in data["scenarios"]:
        scene["actions"] = []
    return data


class DiagnosisTests(unittest.TestCase):
    def test_design_role_leaves_actions_to_the_execution_role(self):
        from e2e_agent.agents import design_decision_schema
        schema = design_decision_schema(16, 20)
        schema.model_validate({"status": "ready", "suite": designed_data()})
        with self.assertRaises(ValueError):
            schema.model_validate({"status": "ready", "suite": shop_suite().model_dump()})
        data = designed_data()
        data["scenarios"][0]["start_url"] += "/sms"
        with self.assertRaises(ValueError):
            schema.model_validate({"status": "ready", "suite": data})

    def test_action_ids_cannot_escape_evidence_directory(self):
        from e2e_agent.domain import Action
        for identity in ("../other", "x/y", "x\\y", "x:alternate"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                Action(id=identity, checkpoint_id="login", kind="navigate", value="login", intent="Open login")

    def test_cost_ledger_keeps_required_operations_inside_the_existing_cap(self):
        from e2e_agent.agents import action_cost_ledger
        full = action_cost_ledger("注册后登录和购物车，数量为1，刷新后保留，错误密码和未登录", {})
        self.assertEqual(full["total_operations"], 15)
        self.assertEqual(len(full["groups"]), 6)
        sms = action_cost_ledger("短信登录和购物车，数量为1，刷新后保留", {})
        self.assertEqual(sms["total_operations"], 9)

    def test_validator_feedback_does_not_include_rejected_input_values(self):
        from e2e_agent.agents import design_decision_schema, validation_feedback
        schema = design_decision_schema(16, 20)
        data = designed_data()
        data["budget"]["max_actions"] = "PrivateSentinelValue"
        try:
            schema.model_validate({"status": "ready", "suite": data})
        except ValueError as error:
            feedback = validation_feedback(error)
        self.assertTrue(feedback)
        self.assertNotIn("PrivateSentinelValue", json.dumps(feedback))
        self.assertTrue(all(set(item) == {"path", "message", "type"} for item in feedback))

    def test_design_schema_rejects_missing_login_dependency_before_execution(self):
        from e2e_agent.agents import design_decision_schema
        data = designed_data()
        data["budget"]["max_actions"] = 16
        for rule in data["rules"]:
            rule["source"]["reference"] = "contract:shop-v1"
        for scene in data["scenarios"]:
            for action in scene["actions"]:
                if action["kind"] == "fill":
                    action["field_role"] = "username" if action["resource_id"] == "username" else "password"
        context = {"query": "测试登录和购物车，刷新后保留", "answers": {}, "observations": {}}
        schema = design_decision_schema(16, 20, context)
        schema.model_validate({"status": "ready", "suite": data})
        data["scenarios"][0]["checkpoints"][2]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "ordered chain"):
            schema.model_validate({"status": "ready", "suite": data})

    def test_design_source_schema_accepts_only_confirmed_exact_references(self):
        from e2e_agent.agents import design_decision_schema
        data = designed_data()
        context = {"query": "测试登录和购物车", "answers": {"quantity": "1"}, "observations": {}}
        for rule in data["rules"]:
            rule["source"]["reference"] = "contract:shop-v1"
        schema = design_decision_schema(16, 20, context)
        schema.model_validate({"status": "ready", "suite": data})
        data["rules"][0]["source"]["reference"] = "contract:shop-v1.registration"
        with self.assertRaises(ValueError):
            schema.model_validate({"status": "ready", "suite": data})

    def test_omitted_model_budget_uses_configured_cap_and_explicit_excess_is_refused(self):
        from e2e_agent.agents import design_decision_schema
        schema = design_decision_schema(16, 12)
        data = designed_data()
        data.pop("budget")
        result = schema.model_validate({"status": "ready", "suite": data})
        self.assertEqual(result.suite.budget.max_actions, 16)
        self.assertEqual(result.suite.budget.max_model_calls, 12)
        data["budget"] = {"max_actions": 40}
        with self.assertRaises(ValueError):
            schema.model_validate({"status": "ready", "suite": data})
        data["budget"] = {"max_actions": 16, "max_model_calls": 20}
        with self.assertRaises(ValueError):
            schema.model_validate({"status": "ready", "suite": data})

    def test_modified_expectations_cannot_relabel_old_execution(self):
        suite = shop_suite()
        with self.assertRaises(ValueError):
            build_bundle(suite, {"status": "failed", "requirements_fingerprint": "another-version", "scenarios": {}})

    def test_a_source_reference_without_recorded_source_is_not_confirmed(self):
        suite = shop_suite()
        bundle = build_bundle(suite, {"status": "failed", "requirements_fingerprint": suite.requirements_fingerprint(), "scenarios": {}})
        self.assertEqual(bundle["source_status"], "missing")
        self.assertIn("sample_shop.CONTRACT.v1", bundle["missing_source_refs"])

    def test_resource_receipts_prove_reuse_without_values_or_value_hashes(self):
        from e2e_agent.domain import ResourceRef
        from e2e_agent.resources import ActiveClock, ResourceManager, ResourceUnavailable, ScriptedProvider
        with tempfile.TemporaryDirectory() as folder:
            manager = ResourceManager([ResourceRef(id="password", kind="password", run_id="run")], "run", ScriptedProvider(), ActiveClock(), Path(folder))
            manager.bind_static({"password": "PrivateFixtureValue"})
            first = manager.fill_proof("password", "PrivateFixtureValue")
            second = manager.fill_proof("password", "PrivateFixtureValue")
            self.assertEqual(first["binding_id"], second["binding_id"])
            self.assertNotIn("PrivateFixtureValue", json.dumps(first))
            self.assertEqual(len(first["binding_id"]), 32)
            with self.assertRaises(ResourceUnavailable):
                manager.fill_proof("password", "ChangedValue")

    def test_account_evidence_is_read_only_and_independent_of_auth_rejection_switch(self):
        from e2e_agent.sample_shop import ShopStore
        with tempfile.TemporaryDirectory() as folder:
            store = ShopStore(Path(folder) / "proof.sqlite3")
            store.prepare("proof", ["valid_login_denied"])
            store.account("proof", "FixtureAccount", "FixturePassword")
            self.assertEqual(store.account_state("proof", "FixtureAccount", "FixturePassword"), {"account_exists": True, "password_matches": True})
            self.assertEqual(store.account_state("proof", "FixtureAccount", "WrongPassword"), {"account_exists": True, "password_matches": False})
            self.assertEqual(store.stats("proof"), {"login_count": 0, "send_count": 0})

    def assessment(self, attribution="unknown", references=()):
        return Assessment(diagnosis=Diagnosis(attribution=attribution, evidence_ids=references, explanation="Supported conclusion",
                          uncertainties=("Missing decisive evidence",)), investigations=("Inspect current target and request outcome",))

    def test_unknown_is_allowed_but_fabricated_evidence_is_rejected(self):
        bundle = fixed_bundle("case-e")
        validate_assessment(self.assessment(), bundle)
        with self.assertRaises(ValueError):
            validate_assessment(self.assessment(references=("nonexistent",)), bundle)

    def test_exception_alone_cannot_support_a_product_conclusion(self):
        with self.assertRaises(ValueError):
            validate_assessment(self.assessment("suspected_product", ("ev-0002",)), fixed_bundle("case-e"))

    def test_missing_expectations_require_real_clarification(self):
        bundle = fixed_bundle("case-d")
        with self.assertRaises(ValueError):
            validate_assessment(self.assessment(), bundle)
        value = self.assessment("requirements", ("ev-0001",)).model_copy(update={"clarification_questions": ("What discount rule applies?",)})
        validate_assessment(value, bundle)

    def test_unknown_requires_actionable_uncertainty(self):
        with self.assertRaises(ValueError):
            validate_assessment(Assessment(diagnosis=Diagnosis(evidence_ids=(), explanation="Unknown")), fixed_bundle("case-e"))

    def test_coverage_does_not_trust_a_claimed_pass(self):
        suite = shop_suite()
        result = {"status": "passed", "scenarios": {}}
        coverage = review_coverage(suite, result)
        self.assertFalse(coverage["all_required_verified"])
        self.assertTrue(coverage["reported_pass_without_required_evidence"])
        self.assertTrue(all(r["unverified_checks"] for r in coverage["rules"] if r["required"]))

    def test_redacted_expectation_keeps_coverage_bound_to_original_condition(self):
        suite = shop_suite()
        sanitized = json.loads(suite.model_dump_json().replace("stage_user", "[RESOURCE]"))
        result = {"status": "passed", "sanitized_plan": sanitized, "scenarios": {}}
        for scene in suite.scenarios:
            result["scenarios"][scene.id] = {"checkpoints": [{"id": cp.id, "status": "passed", "evidence": [
                {**condition.model_dump(), "expected": "[RESOURCE]" if "stage_user" in str(condition.expected) else condition.expected,
                 "condition_fingerprint": condition_fingerprint(condition), "status": "passed"}
                for condition in cp.acceptance]} for cp in scene.checkpoints]}
        bundle = build_bundle(suite, result)
        self.assertTrue(bundle["coverage"]["all_required_verified"])
        self.assertNotIn("stage_user", json.dumps(model_bundle(bundle)))

    def test_unknown_coverage_ids_are_refused(self):
        value = self.assessment().model_copy(update={"unverified_rule_ids": ("invented-rule",)})
        with self.assertRaises(ValueError):
            validate_assessment(value, fixed_bundle("case-e"))

    def test_model_payload_omits_paths_and_network_authentication_details(self):
        self.assertEqual(public_url("https://user:private@example.test/cart?token=secret#key"), "https://example.test/cart")
        bundle = fixed_bundle("case-a")
        bundle["evidence"].append({"id": "image", "kind": "screenshot", "path": "hidden-evaluator-label.png"})
        self.assertNotIn("hidden-evaluator-label", json.dumps(model_bundle(bundle)))

    def test_rerun_archives_original_failure_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pytest.log").write_text("original failure")
            (root / "replay-suite").mkdir()
            (root / "replay-suite/suite-result.json").write_text('{"status":"failed"}')
            archive = archive_replay(root)
            (root / "pytest.log").write_text("later outcome")
            self.assertEqual((archive / "pytest.log").read_text(), "original failure")
            self.assertEqual((archive / "replay-suite/suite-result.json").read_text(), '{"status":"failed"}')

    def test_action_kind_has_an_unambiguous_evidence_namespace(self):
        suite = shop_suite()
        result = {"status": "error", "scenarios": {suite.scenarios[0].id: {"events": [{"kind": "click", "status": "error", "action_id": "x"}]}}}
        bundle = build_bundle(suite, result)
        action = next(e for e in bundle["evidence"] if e["kind"] == "action")
        self.assertEqual(action["operation"], "click")


class DiagnosticPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_batch_preserves_checkpoint_fields_inputs_and_refresh(self):
        from e2e_agent.agents import ExecutionAgent
        data = shop_suite().model_dump()
        scene = shop_suite().scenarios[0]
        observation = {"candidates": {
            "label:Username": [{"label": "Username", "type": "text", "tag": "input"}],
            "label:Password": [{"label": "Password", "type": "password", "tag": "input"}],
            "testid:register-submit": [{"tag": "button", "text": "Register account"}]}}
        actions = [a for a in data["scenarios"][0]["actions"] if a["checkpoint_id"] == "registration"]
        for action in actions:
            if action["kind"] == "fill":
                action["field_role"] = "username" if action["resource_id"] == "username" else "password"
        class Fixed:
            async def call(self, role, schema, system, payload):
                return schema.model_validate({"actions": self.actions})
        transport = Fixed()
        executor = ExecutionAgent(transport)
        async def plan(checkpoint, remaining=16):
            return await executor.plan_checkpoint(suite=shop_suite(), scene=scene, checkpoint=checkpoint,
                observations={"register": observation}, current_observation=observation, remaining=remaining)
        transport.actions = actions
        good = await plan(scene.checkpoints[0])
        self.assertTrue(all(a.id.startswith("registration-") for a in good))
        for change in ("checkpoint", "resource", "field", "extra", "missing", "duplicate"):
            transport.actions = deepcopy(actions)
            if change == "checkpoint": transport.actions[0]["checkpoint_id"] = "login"
            if change == "resource": transport.actions[1]["resource_id"] = "absent"
            if change == "field": transport.actions[1]["target"] = "label:Password"
            if change == "extra": transport.actions.append(deepcopy(actions[0]))
            if change == "missing": transport.actions.pop()
            if change == "duplicate": transport.actions[1]["id"] = transport.actions[0]["id"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                await plan(scene.checkpoints[0])
        transport.actions = actions
        with self.assertRaises(ValueError):
            await plan(scene.checkpoints[0], remaining=3)
        transport.actions = [{"id": "skip", "checkpoint_id": "refresh", "kind": "observe", "intent": "Skip actual refresh"}]
        with self.assertRaises(ValueError):
            await plan(scene.checkpoints[3])

    async def test_partial_restart_uses_frozen_answers_without_restoring_browser_or_resources(self):
        from unittest.mock import AsyncMock, patch
        from e2e_agent.resume import restart_suite
        data = designed_data()
        data["version"] = 2
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "old"
            old.mkdir()
            (old / "partial-plan.json").write_text(json.dumps({"suite": data}), encoding="utf-8")
            metadata = {"agent_protocol_version": 2, "status": "cancelled", "answers": {"target_quantity": "1"},
                        "confirmed_sources": {"answer:target_quantity": {"kind": "clarification", "text": "1"}}}
            (old / "agent-result.json").write_text(json.dumps(metadata), encoding="utf-8")
            async def restarted(*args, **kwargs):
                self.assertEqual(kwargs["frozen_suite"].version, 2)
                self.assertEqual(kwargs["confirmed_answers"], metadata["answers"])
                self.assertEqual(kwargs["confirmed_sources"], metadata["confirmed_sources"])
                kwargs["run_dir"].mkdir()
                return {"status": "passed", "complete_script": True}
            with patch("e2e_agent.agent_workflow.run_agent_suite", new=AsyncMock(side_effect=restarted)) as run:
                result = await restart_suite(Settings(), old, run_dir=root / "new")
            run.assert_awaited_once()
            self.assertFalse(result["browser_state_restored"])
            self.assertFalse(result["old_resources_restored"])
            self.assertEqual(result["stability"], "inconsistent")
            self.assertTrue(result["missing_batches_at_restart"])

    async def test_provider_fallback_receives_only_validator_feedback_inside_call_budget(self):
        from e2e_agent.agents import ModelServiceError, RoleModel, design_decision_schema
        schema = design_decision_schema(16, 20)
        data = designed_data()
        data["budget"]["max_actions"] = 16
        invalid = deepcopy(data)
        invalid["budget"]["max_actions"] = "PrivateSentinelValue"
        try:
            schema.model_validate({"status": "ready", "suite": invalid})
        except ValueError as error:
            primary_error = error
        payloads = []
        class Client:
            def __init__(self, primary):
                self.primary = primary
            def with_structured_output(self, schema, **kwargs):
                return self
            async def ainvoke(self, messages):
                payloads.append(json.loads(messages[-1].content))
                if self.primary:
                    return {"parsed": None, "parsing_error": primary_error, "raw": None}
                return {"parsed": schema.model_validate({"status": "ready", "suite": data}), "raw": None}
        def model(limit):
            instance = object.__new__(RoleModel)
            instance.settings, instance.limit, instance.calls, instance.audit_path = Settings(), limit, [], None
            instance.providers = [("primary", "fixture", Client(True)), ("backup", "fixture", Client(False))]
            return instance
        transport = model(2)
        result = await transport.call("design", schema, "Return the schema", {"query": "A controlled goal"})
        self.assertEqual(result.suite.budget.max_actions, 16)
        self.assertEqual([c["status"] for c in transport.calls], ["error", "passed"])
        self.assertTrue(payloads[-1]["previous_validator_feedback"])
        self.assertNotIn("PrivateSentinelValue", json.dumps(payloads[-1]))
        bounded = model(1)
        with self.assertRaises(ModelServiceError):
            await bounded.call("design", schema, "Return the schema", {"query": "A controlled goal"})
        self.assertEqual(len(bounded.calls), 1)

    async def test_diagnosis_is_separate_and_never_changes_execution_status(self):
        class Fixed:
            calls = []
            async def call(self, role, schema, system, payload):
                self.calls.append({"role": role})
                return Assessment(diagnosis=Diagnosis(evidence_ids=(), explanation="Insufficient observation", uncertainties=("Missing actual page",)),
                                  investigations=("Collect current page and outcome",))
        suite = shop_suite()
        execution = {"status": "error", "scenarios": {}, "source_catalog": {"sample_shop.CONTRACT.v1": {"kind": "contract", "text": "Frozen test contract"}}}
        before = deepcopy(execution)
        with tempfile.TemporaryDirectory() as folder:
            report = await diagnose_execution(Settings(), suite, execution, Path(folder) / "assessment", transport=Fixed())
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["execution_status"], "error")
            self.assertEqual(report["automatic_repairs"], 0)
            self.assertEqual(execution, before)
