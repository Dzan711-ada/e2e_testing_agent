"""Exercise the real role transport with deterministic provider responses."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from typing import Literal
import unittest

from pydantic import Field

from e2e_agent.agents import ModelServiceError, RoleModel
from e2e_agent.config import Settings
from e2e_agent.domain import Record
from e2e_agent.resources import Redactor


class BoundedReply(Record):
    max_actions: int = Field(ge=1, le=3)


class AcceptedReply(Record):
    accepted: Literal[True]


class Provider:
    def __init__(self, response, payloads):
        self.response, self.payloads = response, payloads

    def with_structured_output(self, schema, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.payloads.append(json.loads(messages[-1].content))
        return self.response


def role_model(providers, audit_path, redactor, *, public_ids=(), public_locators=()):
    # No model client or credentials are constructed. The actual RoleModel.call
    # implementation still owns fallback, parsing, budget and audit behavior.
    model = object.__new__(RoleModel)
    model.settings, model.limit, model.calls = Settings(), len(providers), []
    model.audit_path = audit_path
    model.providers = [("fixture-" + str(index), "offline", provider)
                       for index, provider in enumerate(providers)]
    model.bind_runtime_redactor(redactor.data, public_ids=public_ids, public_locators=public_locators)
    return model


class RoleModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_response_and_metadata_stay_private_while_fallback_keeps_budget_feedback(self):
        prefix = "RoleFallbackCanary7Q"
        private = prefix + "'\\\"tail"
        redactor, payloads = Redactor(), []
        redactor.register(private)
        try:
            BoundedReply.model_validate({"max_actions": 4, "rejected_input": private})
        except ValueError as error:
            rejected = error
        raw = SimpleNamespace(response_metadata={"finish_reason": private,
            "model_name": private, "system_fingerprint": private}, usage_metadata={"input_tokens": 2})
        providers = [Provider({"parsed": None, "parsing_error": rejected, "raw": raw}, payloads),
                     Provider({"parsed": {"max_actions": 3}, "raw": raw}, payloads)]
        with tempfile.TemporaryDirectory() as folder:
            audit = Path(folder) / "model-calls.json"
            model = role_model(providers, audit, redactor)
            reply = await model.call("execute", BoundedReply, "Return a bounded response", {"goal": "Public goal"})
            self.assertEqual(reply.max_actions, 3)
            self.assertEqual([item["status"] for item in model.calls], ["error", "passed"])
            feedback = payloads[1]["previous_validator_feedback"]
            self.assertTrue(any(item["type"] == "less_than_equal" and "3" in item["message"] for item in feedback))
            self.assertTrue(all(set(item) == {"path", "message", "type"} for item in feedback))
            for body in (json.dumps(model.calls), json.dumps(payloads), audit.read_text(encoding="utf-8")):
                self.assertNotIn(prefix, body)
            self.assertEqual(len(model.calls), model.limit)

    async def test_unparsed_provider_completion_is_not_echoed_in_error_or_audit(self):
        prefix = "RoleRawCompletionCanary8Q"
        private = prefix + "'\\\"tail"
        redactor, payloads = Redactor(), []
        redactor.register(private)
        response = {"parsed": None, "parsing_error": ValueError("Raw completion: " + repr({"password": private})),
                    "raw": SimpleNamespace(response_metadata={}, usage_metadata=None)}
        with tempfile.TemporaryDirectory() as folder:
            audit = Path(folder) / "model-calls.json"
            model = role_model([Provider(response, payloads)], audit, redactor)
            with self.assertRaises(ModelServiceError) as raised:
                await model.call("execute", AcceptedReply, "Return a structured response", {"goal": "Public goal"})
            for body in (str(raised.exception), json.dumps(model.calls), audit.read_text(encoding="utf-8")):
                self.assertNotIn(prefix, body)
                self.assertNotIn("Raw completion:", body)
            self.assertEqual(len(model.calls), 1)

    async def test_public_resource_protocol_survives_secret_collisions_and_repair_keeps_execution_identity(self):
        prefix = "RolePrivateLoginCanary6Q"
        redactor, payloads = Redactor(), []
        for value in ("login", "password", "kind", "query", "status", prefix):
            redactor.register(value)
        payload = {"query": "Public testing goal", "status": "running", "scenario_id": "login", "checkpoint_id": "login",
            "resources": [{"id": "password", "kind": "password", "acquisition": "runtime"}],
            "current_observation": {"candidates": {"testid:login-submit": [
                {"tag": "button", "text": "Log in", "form_testid": "login-form"}]}},
            "original_action": {"id": "submit-login", "checkpoint_id": "login", "kind": "click",
                "intent": "submit-login", "target": "testid:login-submit", "value": None},
            "private_note": prefix}
        with tempfile.TemporaryDirectory() as folder:
            model = role_model([Provider({"parsed": {"accepted": True}, "raw": None}, payloads)],
                Path(folder) / "model-calls.json", redactor,
                public_ids=("login", "password", "submit-login"), public_locators=("testid:login-submit",))
            await model.call("repair", AcceptedReply, "Propose an execution repair", payload)
            sent = payloads[0]
            self.assertEqual(sent["query"], "Public testing goal")
            self.assertEqual(sent["status"], "running")
            self.assertEqual(sent["scenario_id"], "login")
            self.assertEqual(sent["checkpoint_id"], "login")
            self.assertEqual(sent["resources"], payload["resources"])
            self.assertIn("testid:login-submit", sent["current_observation"]["candidates"])
            self.assertEqual(sent["original_action"], payload["original_action"])
            self.assertNotIn(prefix, json.dumps(sent))
            self.assertEqual(model.calls[0]["role"], "repair")
            self.assertEqual(model.calls[0]["agent_id"], "execution")


if __name__ == "__main__":
    unittest.main()
