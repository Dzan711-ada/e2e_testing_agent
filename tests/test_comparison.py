"""Pure evaluation-contract tests; synthetic rows are not production graph evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from e2e_agent.config import Settings
from e2e_agent.domain import Suite
from e2e_agent.repairs import protected_fingerprint, required_validation_conditions
from scripts.verify_comparison import ARMS, CASES, aggregate, capture_row, digest, protocol, run_schedule, write


def rows_for(spec):
    rows = []
    for identity, arm, repeat in run_schedule():
        normal, repair = identity == "clear-goal", identity == "stale-target" and arm == "with_repair"
        row = {"case_id": identity, "arm": arm, "repeat": repeat,
            "input_sha256": spec["fixed_input_hashes"][identity],
            "model_configuration_sha256": spec["model_configuration_sha256"],
            "input_unchanged": True, "trace_verified": True, "diagnosis_flag_verified": True,
            "diagnostic_source_verified": True, "repair_path_verified": repair,
            "requirements_preserved": True, "complete_script": True,
            "business_status": "passed" if normal or repair else "error",
            "execution_status": "passed" if normal or repair else ("error" if identity == "stale-target" else "failed"),
            "replay_status": "error" if identity == "stale-target" and not repair else "passed",
            "repaired": repair, "repair_count": int(repair), "initial_failure_count": int(repair),
            "equivalent_repair": repair, "attribution": None if normal or arm == "planning_execution" else (
                "agent_execution" if identity == "stale-target" else "suspected_product"),
            "model_roles": ["design", "execute"] if normal else (["diagnose", "repair"] if repair else (
                ["diagnose"] if arm != "planning_execution" else [])), "validation_errors": []}
        row["model_call_count"] = len(row["model_roles"])
        row["model_calls"] = [{"role": role} for role in row["model_roles"]]
        rows.append(row)
    return rows


class RepeatedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.spec = protocol(Settings())

    def test_fixed_inputs_are_identical_across_arms_and_cannot_raise_budgets(self):
        before = deepcopy(self.spec)
        for identity, arm, repeat in run_schedule():
            self.assertEqual(digest(self.spec["cases"][identity]), self.spec["fixed_input_hashes"][identity])
            suite = self.spec["cases"][identity]["suite"]
            if suite:
                self.assertLessEqual(suite["budget"]["max_actions"], Settings().max_actions)
                self.assertTrue(all(rule["source"]["reference"] == "contract:shop-v1" for rule in suite["rules"]))
        self.assertEqual(self.spec, before)
        # The fixed input must survive the same JSON boundary as original-plan
        # and export artifacts. Pydantic's default Python-mode tuples otherwise
        # make equivalent repair comparison fail before graph evidence is read.
        self.assertEqual(json.loads(json.dumps(self.spec)), self.spec)
        original = self.spec["cases"]["stale-target"]["suite"]
        frozen_suite = Suite.model_validate(original)
        effective = deepcopy(original)
        scene = effective["scenarios"][0]
        action = next(a for a in scene["actions"] if a["id"] == "submit-login")
        action["target"] = "testid:login-submit"
        history = {"decision": "approved", "execution_status": "passed", "scenario_id": scene["id"],
            "action_id": action["id"], "failed_event": {"side_effect_attempted": False},
            "assessment": {"diagnosis": {"attribution": "agent_execution"}},
            "proposed_plan": {"proposed_actions": [deepcopy(action)],
                "requirements_fingerprint": frozen_suite.requirements_fingerprint(),
                "protected_data_fingerprint": protected_fingerprint(frozen_suite),
                "validation_conditions": list(required_validation_conditions(frozen_suite))}}
        parent = [{"node": node, "run_id": "parent-json-boundary"} for node in ("export", "replay", "analyze")]
        events = [{"node": node, "run_id": "child-json-boundary", "graph_event_index": index,
                   "next_step": "execute_action" if node == "validate_repair" else "unused"}
                  for index, node in enumerate(("diagnose_failure", "plan_repair", "validate_repair", "execute_action"), 1)]
        execution = {"run_id": "child-json-boundary", "status": "passed", "repaired": True,
            "repair_history": [history], "repair_count": 1, "initial_failures": [{"status": "error"}],
            "requirements_fingerprint": frozen_suite.requirements_fingerprint()}
        result = {"status": "passed", "complete_script": True, "execution": execution,
                  "replay": {"status": "passed"}, "final_diagnose_enabled": True}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            directory = Path(temporary)
            (directory / "generation").mkdir()
            for filename, content in (("agent-result.json", result), ("original-plan.json", original),
                                      ("suite.json", effective), ("parent-events.json", parent),
                                      ("generation/execution-events.json", events)):
                write(directory / filename, content)
            for filename, content in (("parent-events.jsonl", parent), ("generation/execution-events.jsonl", events)):
                (directory / filename).write_text("\n".join(json.dumps(item) for item in content), encoding="utf-8")
            row = capture_row(directory, "stale-target", "with_repair", 1, self.spec, [], 0)
        self.assertTrue(row["equivalent_repair"])
        self.assertTrue(row["requirements_preserved"])
        self.assertTrue(row["trace_verified"])
        self.assertEqual(self.spec, before, "Repair capture must not mutate any frozen input")

    def test_complete_grid_preserves_expected_failures_and_denominators(self):
        rows = rows_for(self.spec)
        result = aggregate(rows, self.spec)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["all_samples_retained"])
        self.assertEqual(result["sample_count"], 18)
        self.assertEqual(len(result["groups"]), 9)
        self.assertTrue(all(group["sample_count"] == 2 for group in result["groups"]))
        self.assertTrue(any(row["business_status"] == "error" for row in rows))

    def test_failed_repeat_is_retained_instead_of_best_run_selection(self):
        rows = rows_for(self.spec)
        rows[0]["business_status"] = "error"
        result = aggregate(rows, self.spec)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["all_samples_retained"])
        group = next(g for g in result["groups"] if g["case_id"] == "clear-goal" and g["arm"] == "planning_execution")
        self.assertEqual(group["sample_count"], 2)
        self.assertEqual(group["expected_outcome_count"], 1)
        self.assertIn("error", group["business_statuses"])

    def test_missing_or_duplicate_samples_cannot_satisfy_complete_grid(self):
        rows = rows_for(self.spec)
        for altered in (rows[:-1], rows[:-1] + [deepcopy(rows[0])]):
            with self.subTest(size=len(altered)):
                result = aggregate(altered, self.spec)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["all_samples_retained"])

    def test_changed_input_or_model_configuration_is_rejected(self):
        for key in ("input_sha256", "model_configuration_sha256"):
            with self.subTest(key=key):
                rows = rows_for(self.spec)
                rows[0][key] = "altered"
                self.assertEqual(aggregate(rows, self.spec)["status"], "failed")

    def test_product_false_pass_and_non_equivalent_repair_are_rejected(self):
        rows = rows_for(self.spec)
        next(r for r in rows if r["case_id"] == "product-valid-login-denied")["business_status"] = "passed"
        next(r for r in rows if r["case_id"] == "stale-target" and r["arm"] == "with_repair")["equivalent_repair"] = False
        result = aggregate(rows, self.spec)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("hidden" in error for error in result["validation_errors"]))
        self.assertTrue(any("equivalent" in error for error in result["validation_errors"]))

    def test_original_baseline_is_explicitly_noncomparable(self):
        self.assertFalse(self.spec["legacy"]["comparable"])
        self.assertFalse(self.spec["legacy"]["actual_s0_model_ablation"])
        self.assertEqual(self.spec["legacy"]["status"], "n/a")


if __name__ == "__main__":
    unittest.main()
