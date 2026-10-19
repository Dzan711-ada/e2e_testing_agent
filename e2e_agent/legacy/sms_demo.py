"""Frozen SMS chain used to verify resource orchestration independently of models."""
from .domain import Suite


def sms_suite():
    def checkpoint(identity, target, operator, expected, depends=()):
        return {"id": identity, "description": identity, "depends_on": depends,
                "acceptance": [{"rule_id": identity, "target": target, "operator": operator, "expected": expected}]}

    def action(identity, checkpoint_id, kind, target=None, value=None, resource_id=None, field_role=None):
        return {"id": identity, "checkpoint_id": checkpoint_id, "kind": kind, "target": target, "value": value,
                "resource_id": resource_id, "field_role": field_role, "intent": identity}

    return Suite.model_validate({"id": "sms-suite", "query": "Simulated SMS login, add one book and verify retention", "fixture": "local_shop",
        "rules": [{"id": key, "statement": statement, "source": {"kind": "contract", "reference": "sample_shop.CONTRACT.v1"}} for key, statement in (
            ("login", "A current code bound to this phone/challenge/run logs in once"),
            ("cart", "Adding one Agent Handbook yields quantity 1"), ("refresh", "Quantity 1 survives refresh"))],
        "resources": [{"id": "phone", "kind": "phone", "reusable": True, "run_id": "per-execution"},
                      {"id": "sms_code", "kind": "sms_code", "reusable": False, "run_id": "per-execution"}],
        "scenarios": [{"id": "sms-chain", "description": "SMS login and cart using the same browser context", "namespace": "sms", "start_url": "{base}/shop/{namespace}",
            "checkpoints": [checkpoint("login", "testid:identity", "visible", True),
                            checkpoint("cart", "testid:quantity", "equals", "1", ["login"]),
                            checkpoint("refresh", "testid:quantity", "equals", "1", ["cart"])],
            "actions": [action("open-sms", "login", "navigate", value="sms"),
                        action("request-phone", "login", "request_resource", resource_id="phone"),
                        action("fill-phone", "login", "fill", "label:Phone", resource_id="phone", field_role="phone"),
                        action("send-code", "login", "click", "button:Send code"),
                        action("request-code", "login", "request_resource", resource_id="sms_code"),
                        action("fill-code", "login", "fill", "label:Code", resource_id="sms_code", field_role="sms_code"),
                        action("verify-code", "login", "click", "button:Verify code"),
                        action("add-book", "cart", "click", "button:Add to cart"),
                        action("refresh-cart", "refresh", "navigate", value="cart")]}]})
