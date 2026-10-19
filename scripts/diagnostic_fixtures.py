"""Frozen evaluator inputs and labels. Labels are never part of an Agent payload."""
from copy import deepcopy

INPUT_VERSION = 1
EXPECTED = {"case-a": "agent_execution", "case-b": "suspected_product", "case-c": "environment",
            "case-d": "requirements", "case-e": "unknown", "case-f": "agent_execution", "case-g": "suspected_product",
            "browser-a": "agent_execution", "browser-b": "suspected_product"}
HOLDOUT = {"case-g"}


def fixed_bundle(identity):
    bundle = {"schema_version": 1, "query": "核对明确约定的登录和购物车行为", "execution_status": "failed",
              "requirements_fingerprint": "fixed-evaluation-v1", "source_status": "confirmed",
              "coverage": {"rules": [], "all_required_verified": False}, "planned_actions": [], "planned_checks": [],
              "evidence": [{"id": "ev-0001", "kind": "requirement", "rule_id": "rule", "statement": "有效账号可以登录；加购一本商品后数量为 1，刷新后保留。",
                            "source": {"kind": "contract", "reference": "test-contract-v1"}}]}
    evidence = bundle["evidence"]
    if identity == "case-a":
        evidence += [{"id": "ev-0002", "kind": "action", "operation": "click", "target": "testid:login-old", "status": "error", "error": "TimeoutError: target not found"},
                     {"id": "ev-0003", "kind": "observation", "url": "http://127.0.0.1/shop/example/login",
                      "candidates": {"testid:login-current": [{"tag": "button", "text": "Log in"}]}, "messages": [],
                      "fact": "The intended Log in control exists; planned login-old does not exist; no login submission occurred."}]
    elif identity in {"case-b", "case-g"}:
        evidence += [{"id": "ev-0002", "kind": "action", "operation": "click" if identity == "case-b" else "navigate", "status": "passed",
                      "intent": "Add exactly one book once" if identity == "case-b" else "Reload cart after quantity 1 was verified", "actual": "Authenticated session preserved"},
                     {"id": "ev-0003", "kind": "assertion", "rule_id": "rule", "target": "testid:quantity", "operator": "equals",
                      "expected": "1", "actual": "2" if identity == "case-b" else {"quantity": "0", "message": "Cart is empty"}, "status": "failed"},
                     {"id": "ev-0004", "kind": "observation", "fact": "Target resolves to the actual cart quantity; expected user rule is frozen; there are no duplicate add requests."}]
    elif identity == "case-c":
        evidence += [{"id": "ev-0002", "kind": "action", "operation": "navigate", "status": "error", "error": "TimeoutError: navigation failed"},
                     {"id": "ev-0003", "kind": "network", "url": "http://127.0.0.1:9/login", "method": "GET", "failure": "net::ERR_CONNECTION_REFUSED", "responses_received": 0}]
    elif identity == "case-d":
        bundle["source_status"] = "missing"
        evidence[0].update(statement="需要测试自定义折扣，但用户尚未提供折扣规则。", source=None)
        evidence += [{"id": "ev-0002", "kind": "observation", "fact": "Page shows a price; the expected discount rule is unavailable. Observed behavior is not a requirement."}]
    elif identity == "case-e":
        evidence += [{"id": "ev-0002", "kind": "error", "error": "TimeoutError", "fact": "No target, DOM observation, request result, submitted input validity or action log is available."}]
    elif identity == "case-f":
        evidence += [{"id": "ev-0002", "kind": "action", "status": "passed", "operation": "click", "intent": "Log in once with the prepared valid account"},
                     {"id": "ev-0003", "kind": "assertion", "target": "testid:old-identity", "expected": True, "actual": False, "status": "failed", "error": "TimeoutError: assertion target absent"},
                     {"id": "ev-0004", "kind": "observation", "fact": "Current identity element is visible, authenticated cart loads successfully; old-identity locator is absent. The business login succeeded independently of the failed locator assertion."}]
    else:
        raise ValueError("Unknown fixed evidence identity")
    return deepcopy(bundle)
