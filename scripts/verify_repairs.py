"""Phase-6 browser policy suite; default also uses real diagnosis/repair models."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.actions import ActionError, ActionExecutor
from e2e_agent.agent_workflow import combined_outcome, run_agent_suite
from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.diagnostics import diagnose_execution
from e2e_agent.domain import Action, RepairPlan, Suite
from e2e_agent.repairs import RepairController
from e2e_agent.replay import run_replay
from e2e_agent.sample_shop import CONTRACT, shop_server
from e2e_agent.sms_demo import sms_suite
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite
from scripts.verify_diagnostics import FixedAssessment, GuardModel
from scripts.verify_resources import CapturingProvider, PHONE, scan

INPUT_VERSION = 2
FIXED_CASES = {"stale-target", "ambiguous-control", "bounded-async-wait", "normal-delayed-ui", "repair-budget-stop",
               "malicious-plan-rejected", "product-valid-login-denied", "product-invalid-password-accepted",
               "product-cart-quantity", "session-loss", "uncertain-add", "uncertain-send", "failure-rerun-inconsistent", "graph-repaired-export"}
REAL_CASES = {"stale-target", "product-valid-login-denied"}


class FixedRecovery:
    """Policy fixture; never counted as actual model ability."""
    def __init__(self, mode="normal"):
        self.mode, self.calls, self.limit = mode, [], 20

    async def call(self, role, schema, system, payload):
        if len(self.calls) >= self.limit:
            raise ValueError("Model budget exhausted")
        self.calls.append({"role": role, "status": "passed", "real": False})
        if role == "diagnose":
            return await FixedAssessment("agent_execution").call(role, schema, system, payload)
        original = Action.model_validate(payload["original_action"])
        targets = payload["allowed_equivalent_targets"]
        target = next((t for t in targets if t.startswith("testid:")), targets[0] if targets else original.target)
        replacement = original.model_copy(update={"target": target})
        actions = [replacement]
        if self.mode == "wait" or not targets:
            replacement = original
            actions = [Action(id=original.id + "-wait-" + str(payload["attempt"]), checkpoint_id=original.checkpoint_id,
                              kind="wait", target=original.target, intent="Bounded wait for the original control"), original]
        if self.mode == "malicious":
            actions = [replacement.model_copy(update={"kind": "observe"})]
        return schema.model_validate({"scenario_id": payload["scenario_id"], "action_id": original.id,
            "requirements_fingerprint": payload["requirements_fingerprint"], "protected_data_fingerprint": payload["protected_data_fingerprint"],
            "evidence_ids": [payload["evidence"]["evidence"][0]["id"]], "proposed_actions": [a.model_dump() for a in actions],
            "restart_checkpoint": payload["checkpoint_id"], "attempt": payload["attempt"],
            "validation_conditions": payload["required_validation_conditions"]})


def case_suite(identity):
    if identity == "uncertain-send":
        return sms_suite()
    data = shop_suite().model_dump()
    data["budget"]["max_actions"] = 16
    main = data["scenarios"][0]
    if identity in {"stale-target", "malicious-plan-rejected", "repair-budget-stop", "graph-repaired-export"}:
        next(a for a in main["actions"] if a["id"] == "submit-login")["target"] = "testid:login-old"
    if identity == "graph-repaired-export":
        for rule in data["rules"]: rule["source"]["reference"] = "contract:shop-v1"
        for scene in data["scenarios"]:
            for action in scene["actions"]:
                if action["kind"] == "fill":
                    action["field_role"] = "username" if action["resource_id"] == "username" else "password"
    if identity == "ambiguous-control":
        next(a for a in main["actions"] if a["id"] == "submit-login")["target"] = "button:Log in"
    if identity == "bounded-async-wait":
        data["scenarios"] = [main]
        data["rules"] = [r for r in data["rules"] if r["id"] in {"registration", "login", "cart", "refresh"}]
    return Suite.model_validate(data)


async def verify(output, offline, selected=None):
    settings = Settings.load()
    if not offline: settings.require_model()
    cases = FIXED_CASES if offline else REAL_CASES
    if selected: cases = cases & set(selected)
    report = {"stage": 6, "input_version": INPUT_VERSION, "real_model": not offline,
              "full_stage_specialty": selected is None, "scenarios": [], "input_hashes": {}}
    secrets = [PHONE, settings.deepseek_api_key, settings.qwen_api_key]
    fault_map = {"ambiguous-control": "ambiguous_buttons", "normal-delayed-ui": "delayed_response",
                 "product-valid-login-denied": "valid_login_denied", "product-invalid-password-accepted": "invalid_password_accepted",
                 "product-cart-quantity": "cart_quantity_wrong", "session-loss": "session_lost", "failure-rerun-inconsistent": "stale_locator"}
    for identity in sorted(cases):
        folder = output / identity
        folder.mkdir()
        suite = case_suite(identity)
        frozen = suite.model_dump_json(indent=2)
        (folder / "original-plan.json").write_text(frozen, encoding="utf-8")
        report["input_hashes"][identity] = hashlib.sha256(frozen.encode()).hexdigest()
        mode = "malicious" if identity == "malicious-plan-rejected" else ("wait" if identity in {"bounded-async-wait", "repair-budget-stop"} else "normal")
        model = FixedRecovery(mode) if offline else GuardModel(settings, audit_path=folder / "model-calls.json")
        controller = RepairController(settings, model, folder / "repairs")
        entry = {"id": identity, "status": "running", "model_calls": []}
        try:
            if identity == "graph-repaired-export":
                result = await run_agent_suite(settings, query=suite.query, run_dir=folder / "run", transport=model,
                    frozen_suite=suite, confirmed_sources={"contract:shop-v1": {"kind": "contract", "text": CONTRACT}}, repair=True)
                assert result["status"] == "passed" and result["repaired"] and result["repair_count"] == 1, result.get("error")
                assert result["complete_script"] and result["replay"]["status"] == "passed" and result["stability"] == "inconsistent"
                original = Suite.model_validate_json((folder / "run/original-plan.json").read_text(encoding="utf-8"))
                effective = Suite.model_validate_json((folder / "run/suite.json").read_text(encoding="utf-8"))
                assert any(a.target == "testid:login-old" for a in original.scenarios[0].actions)
                assert all(a.target != "testid:login-old" for a in effective.scenarios[0].actions)
                assert original.requirements_fingerprint() == suite.requirements_fingerprint()
                assert result["execution"]["initial_failures"] and result["execution"]["action_count"] == 16
                entry.update(status="passed", execution_status="passed", repaired=True, repair_count=1, replay_status="passed",
                             expectations_preserved=True, original_failure_preserved=True, model_calls=model.calls)
                report["scenarios"].append(entry)
                continue
            provider = CapturingProvider(secrets, use_test_delivery=True) if identity == "uncertain-send" else None
            # Strategy cannot access the page; schedule the controlled DOM change
            # through an executor fixture seam immediately before preflight.
            original_execute = ActionExecutor._execute
            changed = False
            async def injected(executor, action, conditions, event):
                nonlocal changed
                if identity == "bounded-async-wait" and action.id == "add-book" and not changed:
                    changed = True
                    await executor.page.evaluate("""() => {const b=document.querySelector('[data-testid=add-form] button'); const p=b.parentNode; b.remove(); setTimeout(()=>p.appendChild(b),500)}""")
                result = await original_execute(executor, action, conditions, event)
                if ((identity == "uncertain-add" and action.id == "add-book") or
                    (identity == "uncertain-send" and action.id == "send-code")):
                    raise ActionError("Transport acknowledgement lost after dispatch; outcome must be investigated")
                return result
            mutation = patch.object(ActionExecutor, "_execute", injected) if identity in {"bounded-async-wait", "uncertain-add", "uncertain-send"} else nullcontext()
            with shop_server() as fixture, mutation:
                execution = await execute_suite(suite, folder / "generation", settings.browsers_path, fixture=fixture,
                    faults={suite.scenarios[0].id: [fault_map[identity]]} if identity in fault_map and identity != "product-invalid-password-accepted" else
                           ({"wrong-password": [fault_map[identity]]} if identity == "product-invalid-password-accepted" else None),
                    resource_provider=provider, repair_controller=None if identity == "failure-rerun-inconsistent" else controller)
                if identity == "uncertain-add":
                    namespace = execution["scenarios"]["main-chain"]["namespace"]
                    with fixture[1].connection() as db:
                        assert db.execute("SELECT quantity FROM carts WHERE ns=?", (namespace,)).fetchone()[0] == 1, "A committed add was repeated"
            entry.update(execution_status=execution["status"], repaired=execution["repaired"], repair_count=execution["repair_count"])
            if identity in {"stale-target", "ambiguous-control", "bounded-async-wait"}:
                assert execution["status"] == "passed" and execution["repaired"] and execution["initial_status"] == "error"
                assert execution["stability"] == "inconsistent" and execution["initial_failures"]
                assert execution["scenarios"]["main-chain"]["stats"] == {"login_count": 1, "send_count": 0}
                effective = suite.model_dump()
                for scene in effective["scenarios"]:
                    scene["actions"] = next(s["actions"] for s in execution["sanitized_plan"]["scenarios"] if s["id"] == scene["id"])
                fixed = Suite.model_validate(effective)
                assert fixed.requirements_fingerprint() == suite.requirements_fingerprint()
                export_suite(fixed, folder / "export", settings.browsers_path)
                replay = await run_replay(settings, folder / "export")
                assert replay.status == "passed", replay.detail
                assert combined_outcome(execution, replay.status)["stability"] == "inconsistent"
                entry["replay_status"] = replay.status
            elif identity == "normal-delayed-ui":
                assert execution["status"] == "passed" and not execution["repaired"] and not controller.history
            elif identity == "repair-budget-stop":
                assert execution["status"] == "error" and execution["repair_count"] == 2 and not execution["repaired"]
                assert "budget exhausted" in controller.history[-1]["reason"]
                assert len([h for h in controller.history if h["decision"] == "approved"]) == 2
            elif identity == "malicious-plan-rejected":
                assert execution["status"] == "error" and not execution["repaired"]
                assert "changed operation" in controller.history[0]["reason"]
            elif identity.startswith("product-") or identity == "session-loss":
                assert execution["status"] == "failed" and not controller.history and not execution["repaired"]
                if identity == "session-loss": assert execution["scenarios"]["main-chain"]["stats"]["login_count"] == 1
                if identity == "product-valid-login-denied":
                    diagnostic_model = FixedAssessment("suspected_product") if offline else model
                    diagnosis = await diagnose_execution(settings, suite, execution, folder / "diagnosis", transport=diagnostic_model)
                    assert diagnosis["status"] == "completed" and diagnosis["assessment"]["diagnosis"]["attribution"] == "suspected_product"
                    assert diagnosis["automatic_repairs"] == 0
                    entry["attribution"] = "suspected_product"
            elif identity in {"uncertain-add", "uncertain-send"}:
                assert execution["status"] == "error" and not execution["repaired"] and not model.calls
                assert "uncertain" in controller.history[0]["reason"]
                if identity == "uncertain-send":
                    assert execution["scenarios"]["sms-chain"]["stats"] == {"login_count": 0, "send_count": 1}
                    assert [r["kind"] for r in provider.requests] == ["phone"]
            elif identity == "failure-rerun-inconsistent":
                assert execution["status"] == "error"
                path = folder / "generation/suite-result.json"
                before = path.read_bytes()
                export_suite(suite, folder / "export", settings.browsers_path)
                replay = await run_replay(settings, folder / "export")
                assert replay.status == "passed"
                combined = combined_outcome(execution, replay.status)
                assert combined["status"] == "error" and combined["stability"] == "inconsistent"
                assert path.read_bytes() == before
                (folder / "comparison.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
            assert suite.model_dump_json(indent=2) == frozen, "Original plan was mutated"
            assert execution["action_count"] <= suite.budget.max_actions
            entry.update(status="passed", expectations_preserved=True, original_failure_preserved=bool(execution.get("initial_failures")),
                         action_count=execution["action_count"])
        except Exception as error:
            entry.update(status="failed", error=settings.redact(f"{type(error).__name__}: {error}"))
        entry["model_calls"] = model.calls
        report["scenarios"].append(entry)
    report["checked_artifacts"] = scan(output, secrets)
    report["plaintext_resource_leaks"] = 0
    report["status"] = "passed" if len(report["scenarios"]) == len(cases) and all(s["status"] == "passed" for s in report["scenarios"]) else "failed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--case", action="append", choices=sorted(FIXED_CASES), help="Diagnostic subset; cannot satisfy G6")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        report = asyncio.run(verify(args.output, args.offline, args.case))
    except Exception as error:
        report = {"status": "failed", "real_model": not args.offline, "error": Settings.load().redact(f"{type(error).__name__}: {error}")}
    (args.output / "repair-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "real_model": not args.offline}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
