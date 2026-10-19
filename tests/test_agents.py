from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from e2e_agent.agent_workflow import ScriptedAnswers, run_agent_suite
from e2e_agent.agents import DesignDecision, TargetChoice, validate_choice, validate_design
from e2e_agent.config import Settings
from e2e_agent.domain import Action, Suite
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite


def ready_suite():
    data = shop_suite().model_dump()
    data["budget"]["max_actions"] = 16
    for rule in data["rules"]:
        rule["source"]["reference"] = "contract:shop-v1"
    for scene in data["scenarios"]:
        for action in scene["actions"]:
            if action["kind"] == "fill":
                action["field_role"] = "username" if action["resource_id"] == "username" else "password"
    return Suite.model_validate(data)


OBSERVATION = {"candidates": {"label:Password": [{"label": "Password", "type": "password", "tag": "input"}],
                              "label:Username": [{"label": "Username", "type": "text", "tag": "input"}]}, "url": "http://127.0.0.1"}


class RolePolicyTests(unittest.TestCase):
    def test_missing_rule_has_no_executable_plan_and_answer_is_not_reasked(self):
        decision = DesignDecision(status="needs_clarification", questions=[{"id": "quantity", "text": "目标数量？", "reason": "需要业务验收依据"}])
        validate_design(decision, "登录和购物车", {}, OBSERVATION, 16, 20)
        self.assertIsNone(decision.suite)
        with self.assertRaisesRegex(ValueError, "repeated"):
            validate_design(decision, "登录和购物车", {"quantity": "1"}, OBSERVATION, 16, 20)
        provider = ScriptedAnswers(["1"])
        self.assertEqual(provider.answer(decision.questions), {"quantity": "1"})
        self.assertIsNone(provider.answer(decision.questions))

    def test_email_absence_is_a_conflict_not_a_password_substitution(self):
        ready = DesignDecision(status="ready", suite=ready_suite())
        with self.assertRaisesRegex(ValueError, "email capability"):
            validate_design(ready, "填写邮箱后登录", {}, OBSERVATION, 16, 20)
        conflict = DesignDecision(status="conflict", conflicts=["页面没有邮箱字段"])
        validate_design(conflict, "填写邮箱后登录", {}, OBSERVATION, 16, 20)
        action = Action(id="email", checkpoint_id="registration", kind="fill", target="label:Email", intent="填写邮箱", field_role="email", resource_id="email")
        choice = TargetChoice(action_id="email", checkpoint_id="registration", target="label:Password", reason="wrong target")
        with self.assertRaisesRegex(ValueError, "meaning"):
            validate_choice(action, choice, OBSERVATION)

    def test_required_login_cart_dependency_and_confirmed_sources_enforced(self):
        suite = ready_suite()
        validate_design(DesignDecision(status="ready", suite=suite), "测试登录和购物车", {}, OBSERVATION, 16, 20)
        data = suite.model_dump()
        data["scenarios"][0]["checkpoints"][2]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "ordered chain"):
            validate_design(DesignDecision(status="ready", suite=Suite.model_validate(data)), "测试登录和购物车", {}, OBSERVATION, 16, 20)
        data = suite.model_dump()
        data["rules"][0]["source"]["reference"] = "invented:source"
        with self.assertRaisesRegex(ValueError, "unconfirmed source"):
            validate_design(DesignDecision(status="ready", suite=Suite.model_validate(data)), "测试登录和购物车", {}, OBSERVATION, 16, 20)

    def test_unknown_target_identity_and_role_changes_rejected(self):
        action = Action(id="username", checkpoint_id="login", kind="fill", target="label:Username", intent="账号", field_role="username", resource_id="username")
        for identity, checkpoint, target in (("other", "login", "label:Username"), ("username", "other", "label:Username"),
                                             ("username", "login", "label:Absent"), ("username", "login", "label:Password")):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_choice(action, TargetChoice(action_id=identity, checkpoint_id=checkpoint, target=target, reason="choose"), OBSERVATION)

    def test_cart_cannot_replace_quantity_check_with_visibility_or_guess_missing_target(self):
        suite = ready_suite()
        with self.assertRaisesRegex(ValueError, "must be clarified"):
            validate_design(DesignDecision(status="ready", suite=suite), "登录和购物车，目标数量尚未提供", {}, OBSERVATION, 16, 20)
        data = suite.model_dump()
        data["scenarios"][0]["checkpoints"][2]["acceptance"][0].update(operator="visible", expected=True)
        with self.assertRaisesRegex(ValueError, "confirmed quantity"):
            validate_design(DesignDecision(status="ready", suite=Suite.model_validate(data)), "测试登录和购物车", {}, OBSERVATION, 16, 20)

    def test_refresh_request_cannot_omit_the_separate_retention_checkpoint(self):
        data = ready_suite().model_dump()
        data["scenarios"][0]["checkpoints"] = tuple(c for c in data["scenarios"][0]["checkpoints"] if c["id"] != "refresh")
        data["scenarios"][0]["actions"] = tuple(a for a in data["scenarios"][0]["actions"] if a["checkpoint_id"] != "refresh")
        data["rules"] = tuple(r for r in data["rules"] if r["id"] != "refresh")
        with self.assertRaisesRegex(ValueError, "separate checks"):
            validate_design(DesignDecision(status="ready", suite=Suite.model_validate(data)), "测试登录和购物车，刷新后保留", {}, OBSERVATION, 16, 20)

    def test_design_cannot_raise_budget_or_claim_unsupported_export_complete(self):
        with self.assertRaisesRegex(ValueError, "budget"):
            validate_design(DesignDecision(status="ready", suite=ready_suite()), "登录和购物车", {}, OBSERVATION, 8, 20)
        data = ready_suite().model_dump()
        data["budget"]["max_actions"] = 40
        # Resources are implemented in stage 4; preserve the complete-artifact
        # guard by checking a scene whose required operations are still absent.
        data["scenarios"][0]["actions"] = ()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            export_suite(Suite.model_validate(data), Path(directory) / "export", Path("X:/browser"))


class MissingAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_answer_blocks_before_execution_and_preserves_question(self):
        class FixedModel:
            limit = 20
            calls = []
            async def call(self, role, schema, system, payload):
                self.calls.append({"role": role, "real": False})
                return DesignDecision(status="needs_clarification", questions=[{"id": "quantity", "text": "目标数量？", "reason": "未给出"}])
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(Settings(), root=Path(folder))
            with patch("e2e_agent.agent_workflow.observe_shop", new_callable=AsyncMock, return_value=OBSERVATION), \
                 patch("e2e_agent.agent_workflow.RunRuntime.open", new_callable=AsyncMock) as execute:
                result = await run_agent_suite(settings, query="登录和购物车", run_dir=Path(folder) / "run", transport=FixedModel(), answers_provider=ScriptedAnswers([]))
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["complete_script"])
            self.assertIn("quantity", str(result["decision"]))
            self.assertFalse((Path(folder) / "run/test_generated.py").exists())
            execute.assert_not_called()
