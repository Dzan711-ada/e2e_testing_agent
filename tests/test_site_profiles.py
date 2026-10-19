"""Offline adapter boundaries; no provider or browser is contacted."""
import copy
import json
from pathlib import Path
import unittest

from e2e_agent.agents import (DesignAgent, DesignDecision, ExecutionAgent,
    action_cost_ledger, design_decision_schema, validate_design, validation_feedback)
from e2e_agent.domain import Suite
from e2e_agent.practice_resources import public_demo_resources
from e2e_agent.site_profiles import (LOCAL_SHOP, PRACTICE_PROFILE, PRACTICE_START_URL,
    PRACTICE_SOURCE_ID, get_site_profile, profile_contract, profile_for_suite,
    validate_profile_url)


QUERY = "验证公开练习站正确凭据登录成功，并检查错误密码被拒绝"
INVENTORY = {"url": PRACTICE_START_URL + "login", "candidates": {
    "label:Username": [{"tag": "input", "label": "Username", "type": "text"}],
    "label:Password": [{"tag": "input", "label": "Password", "type": "password"}],
    "button:Login": [{"tag": "button", "text": "Login", "type": "submit"}],
}}


def practice_data(*, with_actions=False):
    contract = profile_contract(PRACTICE_PROFILE)
    data = {"schema_version": 2, "id": "practice-bundle", "query": QUERY,
        "fixture": None, "budget": {"max_actions": 8, "max_model_calls": 20},
        "resources": copy.deepcopy(contract["resources"]),
        "rules": [{"id": role, "statement": "Confirm " + role,
                   "source": {"kind": "contract", "reference": PRACTICE_SOURCE_ID}}
                  for role in contract["cases"]],
        "scenarios": [{"id": role, "namespace": role, "description": "Independent " + role,
            "start_url": PRACTICE_START_URL, "initial_state": "empty",
            "checkpoints": [{"id": role, "description": "Check " + role,
                "acceptance": [{**item, "rule_id": role} for item in case["acceptance"]]}],
            "actions": actions_for(role) if with_actions else []}
            for role, case in contract["cases"].items()]}
    return data


def actions_for(role):
    password = "password" if role == "login" else "wrong_password"
    return [
        {"id": "open", "kind": "navigate", "checkpoint_id": role, "intent": "Open login", "value": "login"},
        {"id": "account", "kind": "fill", "checkpoint_id": role, "intent": "Fill account resource",
         "target": "label:Username", "resource_id": "username", "field_role": "username"},
        {"id": "secret", "kind": "fill", "checkpoint_id": role, "intent": "Fill password resource",
         "target": "label:Password", "resource_id": password, "field_role": "password"},
        {"id": "submit", "kind": "click", "checkpoint_id": role, "intent": "Submit login", "target": "button:Login"},
    ]


def validate(data, **kwargs):
    return validate_design(DesignDecision(status="ready", suite=Suite.model_validate(data)),
        kwargs.pop("query", QUERY), {}, {"login": INVENTORY}, 8, 20,
        site_profile=PRACTICE_PROFILE, **kwargs)


class SiteProfilePolicyTests(unittest.TestCase):
    def test_profile_is_exact_confirmed_target_and_url_route_allowlist(self):
        profile = get_site_profile(PRACTICE_PROFILE)
        self.assertEqual(profile.origin, "https://the-internet.herokuapp.com")
        self.assertEqual(profile.login_url, PRACTICE_START_URL + "login")
        self.assertIsNone(get_site_profile(LOCAL_SHOP))
        for path in profile.allowed_paths:
            self.assertEqual(validate_profile_url(PRACTICE_PROFILE, profile.origin + path), profile.origin + path)
        invalid = ["https://the-internet.herokuapp.com.evil.invalid/login", "http://the-internet.herokuapp.com/login",
            "https://user@the-internet.herokuapp.com/login", "https://the-internet.herokuapp.com:443/login",
            "https://the-internet.herokuapp.com/login?next=/secure", "https://the-internet.herokuapp.com/login#fragment",
            "https://the-internet.herokuapp.com/login/../secure", "https://the-internet.herokuapp.com/%73ecure",
            "https://the-internet.herokuapp.com/download", "https://the-internet.herokuapp.com/login/",
            "https://THE-INTERNET.HEROKUAPP.COM/login", "//the-internet.herokuapp.com/login", "login", None]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_profile_url(PRACTICE_PROFILE, url)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            get_site_profile("arbitrary-site")

    def test_resource_values_exist_only_in_separate_project_provider(self):
        resources = public_demo_resources(PRACTICE_PROFILE)
        self.assertEqual(set(resources), {"username", "password", "wrong_password"})
        self.assertNotEqual(resources["password"], resources["wrong_password"])
        public_metadata = json.dumps(profile_contract(PRACTICE_PROFILE), ensure_ascii=False)
        import e2e_agent.site_profiles as profiles
        public_metadata += Path(profiles.__file__).read_text(encoding="utf-8")
        for value in resources.values():
            self.assertNotIn(value, public_metadata)
            data = practice_data()
            data["rules"][0]["statement"] = "Accidental resource literal: " + value
            with self.assertRaisesRegex(ValueError, "literal values are prohibited") as rejected:
                validate(data, require_actions=False)
            for secret in resources.values():
                self.assertNotIn(secret, str(rejected.exception))
        resources["password"] = "changed"
        self.assertNotEqual(public_demo_resources(PRACTICE_PROFILE)["password"], "changed")

    def test_profile_recognition_requires_exact_start_and_contract_source(self):
        self.assertEqual(profile_for_suite(Suite.model_validate(practice_data())), PRACTICE_PROFILE)
        for change in ("url", "source", "fixture"):
            data = practice_data()
            if change == "url":
                data["scenarios"][0]["start_url"] += "login"
            elif change == "source":
                for rule in data["rules"]:
                    rule["source"]["reference"] = "contract:invented"
            else:
                data["fixture"] = "local_shop"
            suite = Suite.model_validate(data)
            self.assertNotEqual(profile_for_suite(suite), PRACTICE_PROFILE)
            with self.assertRaises(ValueError):
                validate(data, require_actions=False)

    def test_fixed_two_case_contract_accepts_design_and_complete_typed_actions(self):
        validate(practice_data(), require_actions=False)
        validate(practice_data(with_actions=True), require_actions=True)
        ledger = action_cost_ledger(QUERY, {}, site_profile=PRACTICE_PROFILE)
        self.assertEqual(ledger["total_operations"], 8)
        self.assertEqual([group["checkpoint"] for group in ledger["groups"]], ["login", "rejection"])

    def test_frozen_checks_cannot_be_dropped_weakened_or_relabelled(self):
        for mutation in ("drop-check", "wrong-url", "weak-auth", "rule-optional", "source", "one-case", "shared-chain"):
            data = practice_data()
            if mutation == "drop-check":
                data["scenarios"][1]["checkpoints"][0]["acceptance"].pop()
            elif mutation == "wrong-url":
                data["scenarios"][0]["checkpoints"][0]["acceptance"][0]["expected"] = PRACTICE_START_URL + "login"
            elif mutation == "weak-auth":
                data["scenarios"][0]["checkpoints"][0]["acceptance"][1]["target"] = "button:Login"
            elif mutation == "rule-optional":
                data["rules"][0]["required"] = False
            elif mutation == "source":
                data["rules"][1]["source"] = {"kind": "user", "reference": "user:request"}
            elif mutation == "one-case":
                data["rules"].pop()
                data["scenarios"].pop()
            else:
                data["scenarios"][1]["depends_on"] = ["login"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(data, require_actions=False)

    def test_resource_permissions_lifecycle_and_initial_context_are_frozen(self):
        for field, value in (("kind", "phone"), ("acquisition", "fixture"), ("sensitive", False),
                ("reusable", False), ("run_id", "other-run"), ("challenge_id", "stale")):
            data = practice_data()
            data["resources"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "resources"):
                validate(data, require_actions=False)
        data = practice_data()
        data["scenarios"][0]["initial_state"] = "registered_account"
        with self.assertRaisesRegex(ValueError, "empty browser"):
            validate(data, require_actions=False)

    def test_budget_caps_and_unsupported_business_scope_are_not_silently_reduced(self):
        for cap in (7, 9):
            data = practice_data()
            data["budget"]["max_actions"] = cap
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, "budget"):
                validate(data, require_actions=False)
        for query in ("登录后加购", "注册并登录", "login using SMS", "测试邮箱登录"):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "outside"):
                validate(practice_data(), require_actions=False, query=query)
            validate_design(DesignDecision(status="conflict", conflicts=["Unsupported by profile"]),
                query, {}, {}, 8, 20, site_profile=PRACTICE_PROFILE)
        validate(practice_data(), require_actions=False, query="只测试正确密码登录")
        # Even an abbreviated custom query retains both required contract cases.
        self.assertEqual(len(practice_data()["scenarios"]), 2)

    def test_practice_schema_keeps_actions_empty_and_target_fixed_local_default_unchanged(self):
        payload = {"query": QUERY, "answers": {}, "observations": {"login": INVENTORY},
            "site_profile": PRACTICE_PROFILE, "max_actions": 8, "model_call_limit": 20}
        schema = design_decision_schema(8, 20, payload)
        schema.model_validate({"status": "ready", "suite": practice_data()})
        for data in (practice_data(with_actions=True), practice_data()):
            if not data["scenarios"][0]["actions"]:
                data["scenarios"][0]["start_url"] = "{base}/shop/{namespace}"
            with self.assertRaises(ValueError):
                schema.model_validate({"status": "ready", "suite": data})
        local_schema = design_decision_schema(16, 20)
        definitions = json.dumps(local_schema.model_json_schema())
        self.assertIn("{base}/shop/{namespace}", definitions)
        self.assertNotIn(PRACTICE_START_URL, definitions)
        self.assertNotIn("total_operations", action_cost_ledger("login", {}))


class RecordingTransport:
    def __init__(self, output):
        self.output, self.calls = output, []

    async def call(self, role, schema, system, payload):
        self.calls.append({"role": role, "system": system, "payload": payload})
        return schema.model_validate(self.output)


class PracticeRoleProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_design_role_uses_selected_profile_without_public_credential_values(self):
        transport = RecordingTransport({"status": "ready", "suite": practice_data()})
        payload = {"query": QUERY, "answers": {}, "observations": {"login": INVENTORY},
            "site_profile": PRACTICE_PROFILE, "max_actions": 8, "model_call_limit": 20,
            "profile_contract": profile_contract(PRACTICE_PROFILE)}
        decision = await DesignAgent(transport).design(payload)
        self.assertEqual(profile_for_suite(decision.suite), PRACTICE_PROFILE)
        self.assertEqual(transport.calls[0]["role"], "design")
        self.assertIn(PRACTICE_SOURCE_ID, transport.calls[0]["system"])
        self.assertNotIn("fixture=local_shop", transport.calls[0]["system"])
        for value in public_demo_resources().values():
            self.assertNotIn(value, json.dumps(transport.calls, ensure_ascii=False))

    async def test_execution_role_plans_both_typed_protocols_from_observed_controls(self):
        suite = Suite.model_validate(practice_data())
        for scene in suite.scenarios:
            role = scene.checkpoints[0].id
            transport = RecordingTransport({"actions": actions_for(role)})
            planned = await ExecutionAgent(transport).plan_checkpoint(suite=suite, scene=scene,
                checkpoint=scene.checkpoints[0], observations={"login": INVENTORY}, current_observation={"candidates": {}}, remaining=8)
            self.assertEqual([action.id for action in planned], [role + "-" + action["id"] for action in actions_for(role)])
            self.assertEqual(planned[2].resource_id, "password" if role == "login" else "wrong_password")
            self.assertEqual(transport.calls[0]["role"], "execute")
            examples = transport.calls[0]["payload"]["action_field_examples"]
            self.assertEqual(len(examples), 4)
            self.assertTrue(all(example["preconditions"] == [] and example["postcondition"] is None for example in examples))
            self.assertEqual(examples[2]["resource_id"], "password" if role == "login" else "wrong_password")
            self.assertIsNone(examples[0]["resource_id"])
            self.assertIsNone(examples[0]["field_role"])
            self.assertTrue(all(examples[3][field] is None for field in ("value", "resource_id", "field_role")))
            for value in public_demo_resources().values():
                self.assertNotIn(value, json.dumps(transport.calls, ensure_ascii=False))

    async def test_execution_rejects_wrong_resource_literal_scope_and_unobserved_control(self):
        suite = Suite.model_validate(practice_data())
        scene = suite.scenarios[1]
        sentinel = 'Rejected_private_"\\_Canary'
        for mutation in ("password", "literal", "wrong-field", "external-url", "missing-action", "extra-wait", "unobserved", "wrong-button", "precondition", "postcondition", "click-value", "navigation-role"):
            actions = actions_for("rejection")
            inventory = copy.deepcopy(INVENTORY)
            if mutation == "password":
                actions[2]["resource_id"] = "password"
            elif mutation == "literal":
                actions[2].pop("resource_id")
                actions[2]["value"] = "do-not-accept-literal"
            elif mutation == "wrong-field":
                actions[1]["target"] = "label:Password"
            elif mutation == "external-url":
                actions[0]["value"] = "https://example.invalid/login"
            elif mutation == "missing-action":
                actions.pop()
            elif mutation == "extra-wait":
                actions.append({"id": "wait", "kind": "wait", "checkpoint_id": "rejection", "intent": "wait", "target": "button:Login"})
            elif mutation == "unobserved":
                inventory["candidates"].pop("button:Login")
            elif mutation == "wrong-button":
                inventory["candidates"]["button:Login"][0]["text"] = "Delete account"
            elif mutation == "precondition":
                actions[0]["preconditions"] = [sentinel]
            elif mutation == "postcondition":
                actions[0]["postcondition"] = sentinel
            elif mutation == "click-value":
                actions[3]["value"] = sentinel
            else:
                actions[0]["field_role"] = "password"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError) as rejected:
                await ExecutionAgent(RecordingTransport({"actions": actions})).plan_checkpoint(suite=suite, scene=scene,
                    checkpoint=scene.checkpoints[0], observations={"login": inventory}, current_observation={"candidates": {}}, remaining=8)
            feedback = json.dumps(validation_feedback(rejected.exception), ensure_ascii=False)
            self.assertNotIn(sentinel, feedback)
            self.assertNotIn(repr(sentinel)[1:-1], feedback)
            if mutation == "click-value":
                self.assertIn("Practice action 4 field value must be null", feedback)
            elif mutation == "navigation-role":
                self.assertIn("Practice action 1 field field_role must be null", feedback)
        transport = RecordingTransport({"actions": actions_for("rejection")})
        with self.assertRaisesRegex(ValueError, "budget"):
            await ExecutionAgent(transport).plan_checkpoint(suite=suite, scene=scene,
                checkpoint=scene.checkpoints[0], observations={"login": INVENTORY}, current_observation={"candidates": {}}, remaining=3)
        self.assertFalse(transport.calls)


if __name__ == "__main__":
    unittest.main()
