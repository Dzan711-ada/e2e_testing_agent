"""Frozen diagnostic evaluation: seven evidence fixtures and two actual browser failures."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.agents import RoleModel
from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.diagnostics import Assessment, DiagnosisAgent, InvalidAssessment, diagnose_execution
from e2e_agent.domain import Diagnosis, Suite
from e2e_agent.evidence import model_bundle
from e2e_agent.suite_demo import shop_suite
from scripts.diagnostic_fixtures import EXPECTED, HOLDOUT, INPUT_VERSION, fixed_bundle

FORBIDDEN_SWITCHES = ("valid_login_denied", "invalid_password_accepted", "cart_quantity_wrong", "cart_lost_on_refresh", "stale_locator", "session_lost")


class GuardModel(RoleModel):
    async def call(self, role, schema, system, payload):
        serialized = json.dumps(payload, ensure_ascii=False)
        assert not any(value in serialized for value in FORBIDDEN_SWITCHES), "Evaluator fault switch leaked into model input"
        assert not any(value and value in serialized for value in (self.settings.deepseek_api_key, self.settings.qwen_api_key,
                       "stage_user", "TestPass123!", "Wrong123!")), "Secret in model input"
        return await super().call(role, schema, system, payload)


class FixedAssessment:
    """Contract/routing fixture only; it is not evidence of model classification skill."""
    def __init__(self, expected):
        self.expected, self.calls = expected, []

    async def call(self, role, schema, system, payload):
        self.calls.append({"role": role, "status": "passed", "service": "offline contract fixture"})
        refs = tuple(e["id"] for e in payload["evidence"] if e["kind"] in {"assertion", "observation", "network", "action", "requirement"})
        return Assessment(diagnosis=Diagnosis(attribution=self.expected, evidence_ids=refs,
            explanation="Frozen contract response for interface verification", uncertainties=("Check alternative causes against the evidence",)),
            investigations=("Inspect the current target and business outcome",),
            clarification_questions=("What expected business rule applies?",) if self.expected == "requirements" else ())


async def verify(output, offline, selected=None):
    settings = Settings.load()
    if not offline:
        settings.require_model()
    report = {"stage": 5, "real_model": not offline, "input_version": INPUT_VERSION, "browser_evidence_version": 5, "scenarios": [],
              "automatic_repairs": 0, "holdout_ids": sorted(HOLDOUT), "full_stage_specialty": selected is None,
              "critical_ids": ["case-a", "case-b", "case-e", "browser-a", "browser-b"]}
    fixed_hashes = {}
    for identity, expected in EXPECTED.items():
        if selected and identity not in selected:
            continue
        folder = output / identity
        folder.mkdir()
        model = FixedAssessment(expected) if offline else GuardModel(settings, call_limit=2, audit_path=folder / "model-calls.json")
        record = {"id": identity, "expected": expected, "model_calls": []}
        try:
            if identity.startswith("case-"):
                bundle = fixed_bundle(identity)
                encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
                fixed_hashes[identity] = hashlib.sha256(encoded.encode()).hexdigest()
                (folder / "evidence.json").write_text(encoded, encoding="utf-8")
                assessment = await DiagnosisAgent(model).assess(bundle)
                (folder / "assessment.json").write_text(assessment.model_dump_json(indent=2), encoding="utf-8")
            else:
                data = shop_suite().model_dump()
                main = data["scenarios"][0]
                faults = None
                if identity == "browser-a":
                    action = next(a for a in main["actions"] if a["kind"] == "click" and a["checkpoint_id"] == "login")
                    action["target"] = "testid:login-old"
                else:
                    faults = {main["id"]: ["valid_login_denied"]}
                suite = Suite.model_validate(data)
                execution = await execute_suite(suite, folder / "generation", settings.browsers_path, faults=faults)
                assert execution["status"] == ("error" if identity == "browser-a" else "failed")
                actual_main = execution["scenarios"][main["id"]]
                for ref in ("username", "password"):
                    fills = [event for event in actual_main["events"] if event.get("resource_id") == ref and event["kind"] == "fill"]
                    assert len(fills) == 2 and all(event["resource_binding"]["input_matches"] is True for event in fills)
                    assert len({event["resource_binding"]["binding_id"] for event in fills}) == 1
                login_proof = next(cp["fixture_state"] for cp in actual_main["checkpoints"] if cp["id"] == "login")
                assert login_proof["account_exists"] is True and login_proof["password_matches"] is True
                assert login_proof["login_count"] == 0
                record["resource_consistency_proven"] = True
                result = await diagnose_execution(settings, suite, execution, folder / "diagnosis", transport=model)
                if result["status"] != "completed":
                    if result.get("rejected_assessment"):
                        record["rejected_assessment"] = result["rejected_assessment"]
                    raise ValueError(result.get("error"))
                assert result["automatic_repairs"] == 0 and result["execution_status"] == execution["status"]
                assessment = Assessment.model_validate(result["assessment"])
                record["browser_status"] = execution["status"]
            record.update(actual=assessment.diagnosis.attribution, evidence_ids=list(assessment.diagnosis.evidence_ids),
                          uncertainty_count=len(assessment.diagnosis.uncertainties), status="passed" if assessment.diagnosis.attribution == expected else "failed")
        except Exception as error:
            record.update(status="failed", actual="unvalidated", error=settings.redact(f"{type(error).__name__}: {error}"))
            if isinstance(error, InvalidAssessment):
                record["rejected_assessment"] = error.candidate
        record["model_calls"] = model.calls
        report["scenarios"].append(record)
    report["fixed_input_hashes"] = fixed_hashes
    report["critical_passed"] = all(s["status"] == "passed" for s in report["scenarios"] if s["id"] in report["critical_ids"])
    report["metrics"] = {"samples": len(report["scenarios"]), "correct": sum(s["status"] == "passed" for s in report["scenarios"]),
                         "unknown": sum(s.get("actual") == "unknown" for s in report["scenarios"]),
                         "scope": "frozen v1 evidence set plus two local-browser failures; not arbitrary websites"}
    report["status"] = "passed" if len(report["scenarios"]) == len(selected or EXPECTED) and all(s["status"] == "passed" for s in report["scenarios"]) else "failed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--case", action="append", choices=sorted(EXPECTED), help="Diagnostic subset only; cannot satisfy G5")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report = asyncio.run(verify(args.output, args.offline, set(args.case) if args.case else None))
    (args.output / "diagnostic-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "metrics": report["metrics"], "real_model": report["real_model"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
