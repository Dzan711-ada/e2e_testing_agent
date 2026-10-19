"""G7 repeated real-model ablation; retain every predeclared sample and failure."""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import sys
import time
from urllib.parse import quote, quote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent.agent_workflow import ScriptedAnswers, run_agent_suite
from e2e_agent.config import Settings
from e2e_agent.domain import Suite
from e2e_agent.sample_shop import CONTRACT
from scripts.verify_agents import INPUTS
from scripts.verify_diagnostics import GuardModel
from scripts.verify_repairs import case_suite
from scripts.verify_stage import configuration_fingerprints, sources

INPUT_VERSION = 1
REPEATS = 2
CALL_LIMIT = 20
CASES = ("clear-goal", "stale-target", "product-valid-login-denied")
ARMS = {
    "planning_execution": {"diagnosis": False, "repair": False},
    "with_diagnosis": {"diagnosis": True, "repair": False},
    "with_repair": {"diagnosis": True, "repair": True},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def model_configuration(settings):
    """Pin public configuration without serializing keys or private endpoint text."""
    providers = []
    for prefix, name in (("deepseek", "DeepSeek"), ("qwen", "Qwen")):
        if getattr(settings, prefix + "_api_key"):
            providers.append({"provider": name, "model": getattr(settings, prefix + "_model"),
                              "endpoint_sha256": digest(getattr(settings, prefix + "_base_url"))})
    return {"provider_order": providers, "temperature": 0, "request_timeout": settings.request_timeout,
            "provider_max_retries": 0, "physical_call_limit_per_run": CALL_LIMIT,
            "max_actions": settings.max_actions, "action_timeout_ms": settings.action_timeout_ms,
            "worker_timeout": settings.worker_timeout, "replay_timeout": settings.replay_timeout,
            "headless": settings.headless, "seed": "unsupported; temperature zero is not deterministic"}


def frozen_input(identity, settings):
    if identity == "clear-goal":
        return {"id": identity, "query": INPUTS[identity], "site_profile": "local_shop", "suite": None,
                "faults": None, "scope": "real Design and Execution; generated plans may differ and are retained"}
    # Freeze the representation that the written artifacts and capture_row read.
    # Python-mode tuples are not equal to JSON arrays and cannot accept the
    # bounded slice replacement used when checking an equivalent repair.
    data = case_suite(identity).model_dump(mode="json")
    data["budget"]["max_actions"] = min(data["budget"]["max_actions"], settings.max_actions)
    for rule in data["rules"]:
        rule["source"]["reference"] = "contract:shop-v1"
    for scene in data["scenarios"]:
        for action in scene["actions"]:
            if action["kind"] == "fill":
                action["field_role"] = "username" if action["resource_id"] == "username" else "password"
    suite = Suite.model_validate(data)
    faults = {suite.scenarios[0].id: ["valid_login_denied"]} if identity == "product-valid-login-denied" else None
    return {"id": identity, "query": suite.query, "site_profile": "local_shop", "suite": suite.model_dump(mode="json"),
            "faults": faults, "scope": "frozen complete plan; diagnosis/repair ablation, not Design skill"}


def protocol(settings):
    inputs = {identity: frozen_input(identity, settings) for identity in CASES}
    config = model_configuration(settings)
    return {"schema_version": 1, "input_version": INPUT_VERSION, "repeats": REPEATS,
            "cases": inputs, "fixed_input_hashes": {key: digest(value) for key, value in inputs.items()},
            "arms": deepcopy(ARMS), "model_configuration": config, "model_configuration_sha256": digest(config),
            "maximum_physical_model_calls": len(CASES) * len(ARMS) * REPEATS * CALL_LIMIT,
            "legacy": {"comparable": False, "actual_s0_model_ablation": False,
                       "status": "n/a", "reason": "Historical S0 single-registration free-code protocol has no shared Suite, resource or multi-scene/fault contract. Current fixed compatibility examples are separate reference evidence, not model improvement."},
            "ordering": "case order fixed; odd repeat arm order forward, even repeat reversed",
            "limitations": ["Three developed local-shop cases, not held-out or arbitrary-site generalization.",
                "Complete frozen fault plans isolate recovery; their no-repair arm needs no planning API call.",
                "Faults affect generation only; healthy independent replay may make the preserved product failure inconsistent.",
                "Provider fallback and nondeterministic responses are recorded; no best-run selection or causal significance claim."]}


def run_schedule():
    for identity in CASES:
        for repeat in range(1, REPEATS + 1):
            for arm in (tuple(ARMS) if repeat % 2 else tuple(reversed(ARMS))):
                yield identity, arm, repeat


def expectation_errors(row):
    errors = []
    identity, arm = row["case_id"], row["arm"]
    def need(condition, message):
        if not condition:
            errors.append(message)
    need(row.get("input_unchanged") is True, "fixed input or budget changed")
    need(row.get("model_call_count", 0) <= CALL_LIMIT, "physical model call cap exceeded")
    need(row.get("trace_verified") is True, "actual node trace is missing or mixed")
    need(row.get("diagnosis_flag_verified") is True, "ablation diagnosis flag/path differs")
    need(row.get("requirements_preserved") is True, "frozen requirements changed")
    if identity == "clear-goal":
        need(row.get("business_status") == row.get("execution_status") == row.get("replay_status") == "passed", "normal first/replay did not both pass")
        need(row.get("complete_script") is True and not row.get("repaired"), "normal script incomplete or repaired")
        need({"design", "execute"} <= set(row.get("model_roles", [])), "normal sample did not exercise real Design and Execution")
    elif identity == "stale-target":
        if arm == "with_repair":
            need(row.get("business_status") == row.get("execution_status") == row.get("replay_status") == "passed", "stale locator was not recovered and independently replayed")
            need(row.get("repaired") is True and row.get("initial_failure_count", 0) > 0, "repair lost original failure")
            need(row.get("equivalent_repair") is True, "repair changed more than the equivalent locator")
            need(row.get("repair_path_verified") is True, "repair did not follow the actual role/policy/dispatch path")
            need({"diagnose", "repair"} <= set(row.get("model_roles", [])), "repair did not exercise both real roles")
        else:
            need(row.get("business_status") != "passed" and row.get("execution_status") == "error" and not row.get("repaired"), "unrepaired stale locator falsely passed")
            if arm == "with_diagnosis":
                need(row.get("attribution") == "agent_execution", "stale locator diagnosis differs")
                need(row.get("diagnostic_source_verified") is True, "stale diagnosis did not use the actual failed run")
    else:
        need(row.get("business_status") != "passed" and row.get("execution_status") == "failed", "product defect was hidden")
        need(not row.get("repaired") and row.get("repair_count") == 0, "product defect was automatically repaired")
        need("repair" not in row.get("model_roles", []), "product assertion incorrectly entered repair proposal")
        if arm != "planning_execution":
            need(row.get("attribution") == "suspected_product", "product defect diagnosis differs")
            need(row.get("diagnostic_source_verified") is True, "product diagnosis did not use the actual failed run")
    if arm == "planning_execution":
        need(not ({"diagnose", "repair"} & set(row.get("model_roles", []))), "disabled roles were called")
    if arm == "with_diagnosis":
        need("repair" not in row.get("model_roles", []), "diagnosis-only arm called repair")
    return errors


def aggregate(rows, specification):
    """Pure aggregation: unsuccessful runs stay in the denominator and all rows."""
    expected = set(run_schedule())
    actual = [(r["case_id"], r["arm"], r["repeat"]) for r in rows]
    errors = []
    if len(actual) != len(expected) or set(actual) != expected:
        errors.append("Repeated sample grid is missing, duplicated or contains an unknown sample")
    evaluated = []
    for row in rows:
        row_errors = [*row.get("validation_errors", []), *expectation_errors(row)]
        if row.get("model_call_count") != len(row.get("model_calls", [])) or row.get("model_roles") != [call["role"] for call in row.get("model_calls", [])]:
            row_errors.append("Model call totals/roles differ from the retained service audit")
        if row["case_id"] not in specification["fixed_input_hashes"] or row.get("input_sha256") != specification["fixed_input_hashes"].get(row["case_id"]):
            errors.append("A repeated sample used a different fixed input")
        if row.get("model_configuration_sha256") != specification["model_configuration_sha256"]:
            errors.append("A repeated sample used a different model configuration")
        errors.extend(row_errors)
        evaluated.append((row, row_errors))
    groups = []
    for identity in CASES:
        for arm in ARMS:
            samples = [row for row in rows if row["case_id"] == identity and row["arm"] == arm]
            groups.append({"case_id": identity, "arm": arm, "sample_count": len(samples),
                "expected_outcome_count": sum(not problems for row, problems in evaluated if row["case_id"] == identity and row["arm"] == arm),
                "business_statuses": [row.get("business_status") for row in samples],
                "execution_statuses": [row.get("execution_status") for row in samples],
                "replay_statuses": [row.get("replay_status") for row in samples],
                "attributions": [row.get("attribution") for row in samples],
                "model_calls": sum(row.get("model_call_count", 0) for row in samples),
                "elapsed_seconds": [row.get("elapsed_seconds") for row in samples],
                "same_observed_outcome": len({(r.get("business_status"), r.get("execution_status"), r.get("replay_status"), r.get("attribution")) for r in samples}) <= 1})
    return {"status": "passed" if not errors else "failed", "sample_count": len(rows), "expected_sample_count": len(expected),
            "all_samples_retained": len(actual) == len(expected) and set(actual) == expected,
            "model_calls": sum(row.get("model_call_count", 0) for row in rows), "groups": groups,
            "validation_errors": errors, "scope": "per-case paired ablation only; legacy not comparable; no pooled improvement percentage"}


def capture_row(directory, identity, arm, repeat, spec, calls, elapsed):
    result = load(directory / "agent-result.json")
    execution = result.get("execution", {})
    parent = load(directory / "parent-events.json")
    parent_lines = [json.loads(line) for line in (directory / "parent-events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    events = load(directory / "generation/execution-events.json")
    event_lines = [json.loads(line) for line in (directory / "generation/execution-events.jsonl").read_text(encoding="utf-8").splitlines() if line]
    def follows(nodes, expected):
        current = iter(nodes)
        return all(any(item == wanted for item in current) for wanted in expected)
    repair_path = follows([e["node"] for e in events], ("diagnose_failure", "plan_repair", "validate_repair", "execute_action"))
    roles = [item["role"] for item in calls]
    audit_path = directory / "model-calls.json"
    audit_verified = load(audit_path) == calls if audit_path.is_file() else not calls
    diagnostics = result.get("diagnostics", {})
    attribution = diagnostics.get("assessment", {}).get("diagnosis", {}).get("attribution")
    diagnostic_source = True
    if attribution:
        actual = load(directory / "replay-suite/suite-result.json") if diagnostics.get("origin") == "replay" else execution
        diagnostic_source = actual.get("status") in {"failed", "error"} and actual.get("run_id") == result.get("diagnostic_run_id")
    original = spec["cases"][identity]["suite"]
    exported_path = directory / "suite.json"
    exported = Suite.model_validate(load(exported_path)) if exported_path.is_file() else None
    original_suite = Suite.model_validate(original) if original else None
    preserved = bool(exported and execution.get("requirements_fingerprint") == exported.requirements_fingerprint())
    if original_suite:
        preserved = preserved and execution.get("requirements_fingerprint") == original_suite.requirements_fingerprint()
    equivalent = False
    if identity == "stale-target" and arm == "with_repair" and exported:
        from e2e_agent.repairs import protected_fingerprint, required_validation_conditions
        before = load(directory / "original-plan.json")
        allowed = deepcopy(original)
        approved = [h for h in execution.get("repair_history", []) if h.get("decision") == "approved" and h.get("execution_status") == "passed"]
        equivalent = before == original and bool(approved)
        for history in approved:
            plan = history["proposed_plan"]
            scene = next(s for s in allowed["scenarios"] if s["id"] == history["scenario_id"])
            index = next(i for i, a in enumerate(scene["actions"]) if a["id"] == history["action_id"])
            current, proposed = scene["actions"][index], plan["proposed_actions"]
            equivalent = equivalent and len(proposed) in {1, 2} and history["failed_event"].get("side_effect_attempted") is False
            equivalent = equivalent and {k: v for k, v in proposed[-1].items() if k != "target"} == {k: v for k, v in current.items() if k != "target"}
            equivalent = equivalent and plan["requirements_fingerprint"] == original_suite.requirements_fingerprint() and plan["protected_data_fingerprint"] == protected_fingerprint(original_suite)
            equivalent = equivalent and set(plan["validation_conditions"]) == set(required_validation_conditions(original_suite))
            if len(proposed) == 2:
                equivalent = equivalent and proposed[0]["kind"] == "wait" and proposed[0]["target"] == proposed[-1]["target"]
            scene["actions"][index:index + 1] = deepcopy(proposed)
        equivalent = equivalent and exported.model_dump() == Suite.model_validate(allowed).model_dump()
        equivalent = equivalent and sum(e["node"] == "validate_repair" and e["next_step"] == "execute_action" for e in events) >= len(approved)
        attribution = approved[0].get("assessment", {}).get("diagnosis", {}).get("attribution") if approved else attribution
    if original_suite and not execution.get("repaired"):
        preserved = preserved and exported.model_dump() == original_suite.model_dump()
    configured = ARMS[arm]["diagnosis"]
    flag_verified = result.get("final_diagnose_enabled") is configured
    if not configured:
        flag_verified = flag_verified and diagnostics.get("status") == ("disabled" if execution.get("status") != "passed" else diagnostics.get("status")) and not any(e["node"] == "final_diagnose" for e in parent)
    return {"case_id": identity, "arm": arm, "repeat": repeat, "run_dir": str(directory),
            "input_sha256": spec["fixed_input_hashes"][identity], "model_configuration_sha256": spec["model_configuration_sha256"],
            "business_status": result.get("status"), "execution_status": execution.get("status"),
            "replay_status": result.get("replay", {}).get("status"), "stability": result.get("stability"),
            "complete_script": result.get("complete_script"), "repaired": execution.get("repaired", False),
            "repair_count": execution.get("repair_count", 0), "initial_failure_count": len(execution.get("initial_failures", [])),
            "attribution": attribution, "requirements_preserved": preserved, "equivalent_repair": equivalent,
            "diagnosis_flag_verified": flag_verified, "diagnostic_source_verified": bool(diagnostic_source), "repair_path_verified": repair_path,
            "trace_verified": bool(parent and parent == parent_lines and events and events == event_lines and audit_verified and len({e["run_id"] for e in parent}) == 1 and {e["run_id"] for e in events} == {execution.get("run_id")} and [e["graph_event_index"] for e in events] == list(range(1, len(events) + 1)) and follows([e["node"] for e in parent], ("export", "replay", "analyze"))),
            "model_roles": roles, "model_call_count": len(calls), "model_calls": calls,
            "usage": [item.get("usage") for item in calls], "elapsed_seconds": round(elapsed, 3),
            "action_count": execution.get("action_count"), "requirements_fingerprint": execution.get("requirements_fingerprint")}


async def verify(output):
    settings = Settings.load()
    settings.require_model()
    before, protected = sources(), configuration_fingerprints()
    specification = protocol(settings)
    write(output / "comparison-inputs.json", specification)
    report = {"schema_version": 1, "stage": 7, "input_version": INPUT_VERSION, "status": "running", "real_model": True,
              "repeats": REPEATS, "legacy_comparable": False, "specification_sha256": digest(specification), "runs": []}
    for identity, arm, repeat in run_schedule():
        case = deepcopy(specification["cases"][identity])
        directory = output / f"{identity}-{arm}-{repeat}"
        transport = GuardModel(settings, call_limit=CALL_LIMIT, audit_path=directory / "model-calls.json")
        began = time.monotonic()
        row = {"case_id": identity, "arm": arm, "repeat": repeat, "run_dir": str(directory),
               "input_sha256": specification["fixed_input_hashes"][identity],
               "model_configuration_sha256": specification["model_configuration_sha256"]}
        try:
            original = deepcopy(case)
            await run_agent_suite(settings, query=case["query"], run_dir=directory, transport=transport,
                answers_provider=ScriptedAnswers([]), frozen_suite=Suite.model_validate(case["suite"]) if case["suite"] else None,
                confirmed_sources={"user:request": {"kind": "user", "text": case["query"]},
                                   "contract:shop-v1": {"kind": "contract", "text": CONTRACT}},
                repair=ARMS[arm]["repair"], final_diagnose_enabled=ARMS[arm]["diagnosis"],
                faults=deepcopy(case["faults"]), site_profile="local_shop")
            row = capture_row(directory, identity, arm, repeat, specification, transport.calls, time.monotonic() - began)
            row["input_unchanged"] = case == original and digest(case) == specification["fixed_input_hashes"][identity]
            row["validation_errors"] = expectation_errors(row)
        except Exception as error:
            row.update(validation_errors=[settings.redact(f"{type(error).__name__}: comparison sample did not satisfy its frozen contract")],
                       business_status="error", model_call_count=len(transport.calls), model_calls=transport.calls,
                       elapsed_seconds=round(time.monotonic() - began, 3))
        report["runs"].append(row)
        write(output / "comparison-validation.json", report)
    if sources() != before or configuration_fingerprints() != protected or model_configuration(Settings.load()) != specification["model_configuration"]:
        report["integrity_error"] = "Source, protected configuration or model configuration changed during repeated evaluation"
    summary = aggregate(report["runs"], specification)
    report.update(summary)
    if report.get("integrity_error"):
        report["status"] = "failed"
    report["fixed_input_hashes"] = specification["fixed_input_hashes"]
    report["model_configuration"] = specification["model_configuration"]
    report["model_configuration_sha256"] = specification["model_configuration_sha256"]
    report["maximum_physical_model_calls"] = specification["maximum_physical_model_calls"]
    report["legacy"] = specification["legacy"]
    report["limitations"] = specification["limitations"]
    # New structured runs must not contain raw DOM/trace payloads. Legacy G6
    # compatibility artifacts are deliberately outside this privacy scan.
    from e2e_agent.suite_reporting import is_generated_report
    for path in output.rglob("*"):
        if path.is_file() and path.suffix != ".png":
            if path.suffix.lower() in {".har", ".zip"} or (path.suffix.lower() == ".html" and not is_generated_report(path)):
                report.update(status="failed", privacy_error="Raw browser evidence appeared in comparison artifacts")
            data = path.read_bytes()
            for value in (settings.deepseek_api_key, settings.qwen_api_key):
                if value and any(item.encode() in data for item in {value, quote(value, safe=""), quote_plus(value)}):
                    report.update(status="failed", privacy_error="API key appeared in comparison artifacts")
    report["plaintext_resource_leaks"] = 0 if not report.get("privacy_error") else None
    report["model_payload_guarding_verified"] = True
    report["privacy_scope"] = "GuardModel rejects fault switches, keys and demo resource values in every model payload; this new-run artifact scan rejects raw browser evidence and key variants. Random private checkpoint/export sentinels remain separately required by full G6 and native graph tests."
    write(output / "comparison-validation.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output))
    except Exception as error:
        report = {"schema_version": 1, "stage": 7, "status": "failed", "real_model": True,
                  "error": Settings.load().redact(type(error).__name__ + ": comparison could not complete")}
        write(args.output / "comparison-validation.json", report)
    print(json.dumps({"status": report["status"], "sample_count": report.get("sample_count"), "model_calls": report.get("model_calls")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
