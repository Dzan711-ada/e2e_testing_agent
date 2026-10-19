"""Run implemented refactor gates; unimplemented gates cannot produce acceptance."""
import argparse
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent.config import Settings
from e2e_agent.resources import Redactor
from scripts.verify_stage import (
    assert_configuration_unchanged, configuration_fingerprints, fingerprint,
    now, sources, write_json,
)

IMPLEMENTED = {"R1", "R2", "R3", "R4", "R5"}
R1_CHECKPOINT_TESTS = {
    "test_graph_checkpoint.InstalledCheckpointTests.test_nested_pause_preserves_position_and_resume_does_not_resend",
    "test_graph_checkpoint.InstalledCheckpointTests.test_waiting_runs_have_independent_checkpoints",
}
R2_GRAPH_TESTS = {
    "test_execution_graph.ProductionExecutionGraphTests.test_each_action_advances_actual_graph_state_and_preserves_scene_context",
    "test_execution_graph.ProductionExecutionGraphTests.test_failed_checkpoint_blocks_dependent_actions_and_keeps_other_scene_running",
    "test_execution_graph.ProductionExecutionGraphTests.test_resource_words_cannot_corrupt_public_control_ids_or_materialized_requirements",
    "test_execution_graph.ProductionExecutionGraphTests.test_same_directory_rerun_archives_old_audit_and_keeps_current_run_isolated",
}
R3_RESOURCE_TESTS = {
    "test_graph_resources.NativeGraphResourceTests.test_native_pause_clock_is_idempotent_and_closed_by_runtime_cleanup",
    "test_graph_resources.NativeGraphResourceTests.test_actual_production_graph_resumes_single_resource_request_without_replay",
    *{"test_redactor_cache.RedactorCacheTests.test_" + name for name in (
        "repeated_redaction_does_not_reencode_registered_values",
        "raw_url_python_json_variants_and_longest_values_stay_masked",
        "clear_removes_all_cached_private_variants_before_next_resource")},
    *{"test_graph_privacy.GraphPrivacyTests.test_" + name for name in (
        "planner_private_intent_cannot_enter_native_checkpoint", "planner_private_target_cannot_enter_native_checkpoint",
        "planner_private_literal_value_cannot_enter_native_checkpoint", "strategy_private_intent_cannot_enter_native_checkpoint",
        "strategy_private_target_cannot_enter_native_checkpoint", "repair_private_target_and_rejected_candidate_history_are_safe",
        "planner_exception_cannot_echo_rejected_private_candidate", "pydantic_rejection_cannot_persist_escaped_private_input",
        "private_candidate_cannot_hide_inside_public_id_substrings", "ordinary_exception_cannot_persist_private_repr",
        "public_control_ids_still_execute_when_resource_has_same_spelling")},
}
R4_AGENT_TESTS = {
    *{"test_agent_graph.NativeAgentGraphTests.test_" + name for name in (
        "native_parent_child_namespace_resumes_without_repeating_completed_actions",
        "cancel_after_successful_repair_preserves_effective_plan_for_fresh_restart",
        "replay_only_failure_diagnoses_the_actual_new_replay_record_and_sources",
        "cancelled_later_resource_wait_preserves_generated_batches_and_restart_only_plans_missing",
        "final_diagnosis_cannot_exceed_the_original_shared_model_budget",
        "replay_failure_rejects_old_or_successful_structured_records",
        "sensitive_frozen_action_is_rejected_before_browser_and_first_native_checkpoint")},
    *{"test_graph_repairs.NativeRepairGraphTests.test_" + name for name in (
        "real_graph_separates_diagnosis_execution_proposal_and_policy",
        "malicious_proposal_reaches_policy_and_cannot_reach_dispatch",
        "uncertain_side_effect_stops_before_either_model_role",
        "resource_failure_never_enters_repair_or_changes_lifetime",
        "product_diagnosis_stops_before_execution_agent_proposal",
        "graph_owned_repair_budget_stops_after_exactly_two_attempts",
        "action_budget_policy_refuses_proposal_before_replacement",
        "active_budget_stops_at_both_model_boundaries",
        "private_repair_plan_metadata_is_rejected_before_checkpoint",
        "public_collision_survives_diagnosis_proposal_and_policy")},
    *{"test_role_privacy.RoleModelBoundaryTests.test_" + name for name in (
        "public_resource_protocol_survives_secret_collisions_and_repair_keeps_execution_identity",
        "rejected_response_and_metadata_stay_private_while_fallback_keeps_budget_feedback",
        "unparsed_provider_completion_is_not_echoed_in_error_or_audit")},
}
PENDING = {}
SUITE_CASES = {
    "normal-generation-1", "normal-generation-2", "login-failure-blocking",
    "ambiguous-target-refused", "independent-suite-replay",
}
RESOURCE_CASES = {
    "sms-first", "sms-fresh-round", "missing-phone", "cancelled-phone",
    "missing-code", "wrong-code", "expired-code", "cancel-and-safe-restart",
    "ipc-replay", "replay-without-resources", "private-account-reference-replay",
    "standalone-export",
}


def unit_collection(output):
    match = re.search(r"Ran (\d+) tests? in", output)
    identities = set(re.findall(r"^test_\S+ \(([^)]+)\)", output, re.MULTILINE))
    if not match or not re.search(r"^OK\s*$", output, re.MULTILINE):
        raise ValueError("Offline regression did not finish with zero skips and failures")
    total = int(match[1])
    if not total or len(identities) != total:
        raise ValueError("Offline test identities are incomplete or duplicated")
    return total, identities


def exact_scenarios(report, expected):
    scenarios = report.get("scenarios", [])
    if report.get("status") != "passed" or len(scenarios) != len(expected):
        raise ValueError("Scenario report is unsuccessful or incomplete")
    if {item["id"] for item in scenarios} != expected:
        raise ValueError("Scenario identities differ from the required gate")
    if not all(item["actual"] == item["expected"] for item in scenarios):
        raise ValueError("Scenario outcome differs from its unchanged expectation")


def verified_previous_gate(path, expected_stage):
    previous = json.loads(path.read_text(encoding="utf-8"))
    if previous.get("stage") != expected_stage or previous.get("status") != "passed":
        raise ValueError(f"{expected_stage} must actually pass before the next gate")
    if previous.get("source_files") != previous.get("source_files_after") or previous.get("source_changes_during_verification") != []:
        raise ValueError("Previous gate source integrity evidence is incomplete")
    if previous.get("protected_files") != previous.get("protected_files_after") or previous.get("unresolved") != []:
        raise ValueError("Previous gate configuration/integrity evidence is unsuccessful")
    archive_path = path.parent / "source.zip"
    if fingerprint(archive_path) != previous.get("archive_sha256"):
        raise ValueError("Previous source snapshot is missing or changed")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(previous["source_files"]):
            raise ValueError("Previous source snapshot collection does not match its manifest")
        for name, expected in previous["source_files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("Previous source snapshot content differs: " + name)
    commands = previous.get("commands", [])
    required = {"offline-regression", "structured-suites", "resource-lifecycle-quick"}
    if expected_stage in {"R2", "R3", "R4"}:
        required |= {"fixed-role-workflows", "production-execution-graph"}
    if expected_stage in {"R3", "R4"}:
        required.remove("resource-lifecycle-quick")
        required |= {"resource-lifecycle-full", "agent-batch-recovery", "native-graph-resources"}
    if expected_stage == "R4":
        required |= {"real-role-workflows", "sms-real-model", "diagnosis-fixed", "diagnosis-real", "repairs-fixed", "repairs-real", "production-agent-graph"}
    if {item["id"] for item in commands} != required or len(commands) != len(required) or not all(item.get("exit_code") == 0 and not item.get("timed_out") for item in commands):
        raise ValueError("Previous command collection or outcome is incomplete")
    if previous.get("suites", {}).get("cases") != sorted(SUITE_CASES) or previous.get("resources", {}).get("cases") != sorted(RESOURCE_CASES):
        raise ValueError("Previous exact browser scenario collections are incomplete")
    groups = ["suites", "resources"] + (["agents_fixed", "execution_graph"] if expected_stage in {"R2", "R3", "R4"} else [])
    if expected_stage in {"R3", "R4"}:
        groups += ["agent_batches", "graph_resources"]
    if expected_stage == "R4":
        groups += ["agents_real", "sms_model", "diagnosis_fixed", "diagnosis_real", "repairs_fixed", "repairs_real", "agent_graph"]
    for group in groups:
        evidence = previous[group]
        if fingerprint(Path(evidence["path"])) != evidence.get("sha256"):
            raise ValueError("Previous browser report is missing or changed")
    count, identities = unit_collection((path.parent / "offline-regression.log").read_text(encoding="utf-8"))
    if count != previous["offline_tests"]["total"] or not R1_CHECKPOINT_TESTS <= identities:
        raise ValueError("Previous offline collection does not match its recorded count")
    if expected_stage in {"R2", "R3", "R4"} and not R2_GRAPH_TESTS <= identities:
        raise ValueError("Previous production graph behavior tests are incomplete")
    if expected_stage in {"R3", "R4"} and not R3_RESOURCE_TESTS <= identities:
        raise ValueError("Previous native resource/privacy/cache tests are incomplete")
    if expected_stage == "R4" and (not R4_AGENT_TESTS <= identities or previous.get("model_calls", 0) <= 0):
        raise ValueError("Previous R4 native parent/repair/actual-model evidence is incomplete")
    return previous, identities


def verify(stage, baseline_path, previous_path=None, *, g6_baseline=None, manual_path=None, wait_for_manual=0):
    if stage == "R5":
        return verify_final(baseline_path, previous_path, g6_baseline, manual_path, wait_for_manual)
    if stage not in IMPLEMENTED:
        raise ValueError(PENDING.get(stage, "Unknown refactor gate"))
    settings = Settings.load()
    redactor = Redactor()
    # These are existing fictional privacy sentinels, not user configuration.
    for value in (settings.deepseek_api_key, settings.qwen_api_key,
                  "15500001111", "SentinelAccount_8de50", "Sentinel-P@ss-3f67!"):
        if value:
            redactor.register(value)

    def safe(value):
        return redactor.text(settings.redact(str(value)))

    run_id = stage + "-" + now()[:19].replace("-", "").replace(":", "").replace("T", "-") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / "runtime/development/refactor" / run_id
    out.mkdir(parents=True, exist_ok=False)
    summary = out / "verification.json"
    before = sources()
    protected = configuration_fingerprints()
    record = {
        "schema_version": 1, "stage": stage, "run_id": run_id, "status": "running",
        "started": now(), "source_files": before, "protected_files": protected,
        "baseline": str(baseline_path), "commands": [], "model_calls": 0,
        "gate_scope": ("Runtime extraction only: all offline tests, structured suites and quick resource browser regression. No later-stage acceptance." if stage == "R1" else
                       "Graph-owned scene/checkpoint/action progression: all R1 regression, fixed-role workflows and actual production graph evidence. Native human-wait recovery and separated repair roles are not accepted by R2." if stage == "R2" else
                       "All R2 regression plus full resource long wait, safe batch restart, native graph pause/resume/cancel/isolation/expiry and saver/artifact privacy. No real model, actual-console or separated repair acceptance." if stage == "R3" else
                       "All R3 regression plus production parent/child routing, separated diagnosis/repair/validation, actual-model design/SMS/diagnosis/repair and independent replay analysis. No actual-console or R5 acceptance."),
        "unresolved": [],
    }
    write_json(summary, record)

    def run(identity, arguments, timeout):
        command = [sys.executable, *map(str, arguments)]
        entry = {"id": identity, "command": command, "cwd": str(ROOT), "started": now(),
                 "log": str(out / (identity + ".log")), "expected_exit_code": 0}
        record["commands"].append(entry)
        write_json(summary, record)
        print(f"[{now()}] {identity}", flush=True)
        started = time.monotonic()
        raw = b""
        try:
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                raw, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                entry["timed_out"] = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                raw, _ = process.communicate()
            entry["exit_code"] = process.returncode
            output = safe(raw.decode("utf-8", errors="replace"))
            Path(entry["log"]).write_text(output, encoding="utf-8")
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(summary, record)
        if entry.get("timed_out") or entry["exit_code"] != 0:
            raise ValueError(f"{identity} failed; see {entry['log']}")
        return output

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("stage") != "R0" or baseline.get("status") != "passed":
            raise ValueError("Refactor gates require a passed fresh R0 baseline")
        expected_commands = {"offline-regression", "browser-check", "original-smoke", "workflow-compatibility"}
        commands = baseline.get("commands", [])
        if len(commands) != len(expected_commands) or {item["id"] for item in commands} != expected_commands or not all(item["exit_code"] == 0 for item in commands):
            raise ValueError("R0 command evidence is incomplete")
        baseline_archive = baseline_path.parent / "source-baseline.zip"
        if fingerprint(baseline_archive) != baseline.get("archive_sha256"):
            raise ValueError("R0 source archive is missing or changed")
        total_before, tests_before = unit_collection((baseline_path.parent / "offline-regression.log").read_text(encoding="utf-8"))
        record.update(baseline_sha256=fingerprint(baseline_path), baseline_archive_sha256=fingerprint(baseline_archive),
                      previous_gate={"path": str(baseline_path), "sha256": fingerprint(baseline_path)})
        previous_tests = set()
        if stage in {"R2", "R3", "R4"}:
            if previous_path is None:
                raise ValueError("A passed preceding refactor verification record is required")
            previous, previous_tests = verified_previous_gate(previous_path, {"R2": "R1", "R3": "R2", "R4": "R3"}[stage])
            record["previous_gate"] = {"path": str(previous_path), "sha256": fingerprint(previous_path),
                                       "archive_sha256": previous["archive_sha256"]}
            record["changes_since_previous"] = {
                "added": sorted(before.keys() - previous["source_files"].keys()),
                "removed": sorted(previous["source_files"].keys() - before.keys()),
                "modified": sorted(path for path in before.keys() & previous["source_files"].keys() if before[path] != previous["source_files"][path]),
            }
        record["changes_since_r0"] = {
            "added": sorted(before.keys() - baseline["source_files"].keys()),
            "removed": sorted(baseline["source_files"].keys() - before.keys()),
            "modified": sorted(path for path in before.keys() & baseline["source_files"].keys() if before[path] != baseline["source_files"][path]),
        }
        record["configuration_changes_before_verification"] = sorted(
            path for path in protected.keys() | baseline["protected_files"].keys()
            if protected.get(path) != baseline["protected_files"].get(path))
        record["lock_sha256"] = fingerprint(ROOT / "uv.lock")
        if record["lock_sha256"] != baseline["source_files"]["uv.lock"]:
            raise ValueError("Dependency lock changed after R0")
        record["dependencies"] = {name: importlib.metadata.version(name) for name in (
            "langgraph", "langgraph-checkpoint", "langchain-core", "langchain-openai",
            "pydantic", "playwright", "pytest", "Flask")}
        with zipfile.ZipFile(out / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in before:
                archive.write(ROOT / path, path)
        record["archive_sha256"] = fingerprint(out / "source.zip")
        unit = run("offline-regression", ["-m", "unittest", "discover", "-s", "tests", "-v"], 180)
        total, identities = unit_collection(unit)
        if total < total_before or not tests_before <= identities:
            raise ValueError("R0 offline tests disappeared from the refactor collection")
        if not R1_CHECKPOINT_TESTS <= identities:
            raise ValueError("R1 installed-version nested-checkpoint tests are missing")
        if previous_tests and not previous_tests <= identities:
            raise ValueError("Previous-stage offline tests disappeared from the collection")
        if stage in {"R2", "R3", "R4"} and not R2_GRAPH_TESTS <= identities:
            raise ValueError("R2 production graph behavior/control-ID tests are missing")
        if stage in {"R3", "R4"} and not R3_RESOURCE_TESTS <= identities:
            raise ValueError("R3 native-resource/privacy tests are missing")
        if stage == "R4" and not R4_AGENT_TESTS <= identities:
            raise ValueError("R4 parent/role/privacy tests are missing")
        record["offline_tests"] = {"total": total, "passed": total, "failed": 0, "skipped": 0,
                                   "r0_total": total_before, "r0_identities_preserved": True,
                                   "r1_checkpoint_tests": sorted(R1_CHECKPOINT_TESTS),
                                   "r2_graph_tests": sorted(R2_GRAPH_TESTS) if stage in {"R2", "R3", "R4"} else [],
                                   "r3_resource_tests": sorted(R3_RESOURCE_TESTS) if stage in {"R3", "R4"} else [],
                                   "r4_agent_tests": sorted(R4_AGENT_TESTS) if stage == "R4" else []}
        run("structured-suites", ["scripts/verify_suites.py", "--output", out / "suites"], 300)
        suites_path = out / "suites/suite-validation.json"
        suites = json.loads(suites_path.read_text(encoding="utf-8"))
        exact_scenarios(suites, SUITE_CASES)
        record["suites"] = {"path": str(suites_path), "sha256": fingerprint(suites_path), "status": "passed", "cases": sorted(SUITE_CASES)}
        resource_flags = [] if stage in {"R3", "R4"} else ["--quick"]
        run("resource-lifecycle-full" if stage in {"R3", "R4"} else "resource-lifecycle-quick",
            ["scripts/verify_resources.py", *resource_flags, "--output", out / "resources"], 600)
        resources_path = out / "resources/resource-validation.json"
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
        exact_scenarios(resources, RESOURCE_CASES)
        if resources.get("long_wait_verified") is not (stage in {"R3", "R4"}):
            raise ValueError("Resource report does not match this gate's required long-wait scope")
        if stage in {"R3", "R4"}:
            first = next(item for item in resources["scenarios"] if item["id"] == "sms-first")
            if not first["timing"]["human_wait_seconds"] > first["worker_timeout"]:
                raise ValueError("Full resource validation did not wait beyond the actual worker timeout")
        if resources.get("model_calls") != 0 or resources.get("actual_console_input") is not False:
            raise ValueError("R1 resource automation must not claim model or actual human acceptance")
        privacy = resources.get("privacy", {})
        if privacy.get("plaintext_resource_leaks") != 0 or privacy.get("raw_dom_trace_files") != 0:
            raise ValueError("Resource privacy evidence is incomplete")
        record["resources"] = {"path": str(resources_path), "sha256": fingerprint(resources_path), "status": "passed",
                               "cases": sorted(RESOURCE_CASES), "long_wait_verified": stage in {"R3", "R4"}, "privacy": privacy}
        if stage in {"R2", "R3", "R4"}:
            run("fixed-role-workflows", ["scripts/verify_agents.py", "--offline", "--output", out / "agents-fixed"], 600)
            agents_path = out / "agents-fixed/agent-validation.json"
            agents = json.loads(agents_path.read_text(encoding="utf-8"))
            exact_scenarios(agents, {"clear-goal", "required-clarification"})
            if agents.get("real_model") is not False or agents.get("full_stage_specialty") is not True or not all(item["complete_script"] for item in agents["scenarios"]):
                raise ValueError("Fixed-role workflow evidence is incomplete or claims real model acceptance")
            record["agents_fixed"] = {"path": str(agents_path), "sha256": fingerprint(agents_path), "status": "passed",
                                      "cases": ["clear-goal", "required-clarification"], "real_model": False}
            run("production-execution-graph", ["scripts/verify_execution_graph.py", "--output", out / "execution-graph"], 600)
            graph_path = out / "execution-graph/execution-graph-validation.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            from scripts.verify_execution_graph import CASES, INPUT_VERSION
            exact_scenarios(graph, set(CASES))
            if graph.get("input_version") != INPUT_VERSION or graph.get("full_stage_specialty") is not True or graph.get("model_calls") != 0:
                raise ValueError("Production graph evidence version/scope is incomplete")
            if graph.get("native_human_resume_verified") is not False or graph.get("separated_repair_roles_verified") is not False:
                raise ValueError("R2 must not claim later-stage resource/repair acceptance")
            if not all(item.get("architecture_verified") is True and item.get("requirements_preserved") is True for item in graph["scenarios"]):
                raise ValueError("Production graph architecture or requirement evidence is incomplete")
            record["execution_graph"] = {"path": str(graph_path), "sha256": fingerprint(graph_path), "status": "passed",
                                         "cases": sorted(CASES), "input_version": INPUT_VERSION}
        if stage in {"R3", "R4"}:
            run("agent-batch-recovery", ["scripts/verify_agent_batches.py", "--output", out / "agent-batches"], 300)
            batches_path = out / "agent-batches/batch-validation.json"
            batches = json.loads(batches_path.read_text(encoding="utf-8"))
            batch_cases = {"frozen-partial-restart", "nonreusable-account-blocked", "runtime-account-first-and-replay"}
            if batches.get("status") != "passed" or len(batches.get("cases", [])) != len(batch_cases) or set(batches["cases"]) != batch_cases or batches.get("plaintext_resource_leaks") != 0:
                raise ValueError("Frozen batch/resource restart regression is incomplete")
            record["agent_batches"] = {"path": str(batches_path), "sha256": fingerprint(batches_path), "status": "passed", "cases": sorted(batch_cases)}
            run("native-graph-resources", ["scripts/verify_graph_resources.py", "--output", out / "graph-resources"], 600)
            native_path = out / "graph-resources/graph-resource-validation.json"
            native = json.loads(native_path.read_text(encoding="utf-8"))
            from scripts.verify_graph_resources import CASES as NATIVE_CASES, INPUT_VERSION as NATIVE_VERSION
            exact_scenarios(native, set(NATIVE_CASES))
            if native.get("input_version") != NATIVE_VERSION or native.get("full_stage_specialty") is not True or native.get("model_calls") != 0 or native.get("real_model") is not False or native.get("actual_console_input") is not False:
                raise ValueError("Native graph resource scope/version is incomplete")
            if native.get("native_human_resume_verified") is not True or native.get("separated_repair_roles_verified") is not False or native.get("plaintext_resource_leaks") != 0:
                raise ValueError("Native resource/privacy acceptance is incomplete or claims a later-stage result")
            if not all(item.get("native_graph_verified") is True and item.get("privacy_verified") is True for item in native["scenarios"]):
                raise ValueError("Native graph scenario evidence is incomplete")
            record["graph_resources"] = {"path": str(native_path), "sha256": fingerprint(native_path), "status": "passed", "input_version": NATIVE_VERSION,
                                         "cases": sorted(NATIVE_CASES), "actual_console_input": False}
        if stage == "R4":
            run("real-role-workflows", ["scripts/verify_agents.py", "--output", out / "agents-real"], 1200)
            agents_path = out / "agents-real/agent-validation.json"
            agents = json.loads(agents_path.read_text(encoding="utf-8"))
            exact_scenarios(agents, {"clear-goal", "required-clarification"})
            if agents.get("real_model") is not True or agents.get("full_stage_specialty") is not True or not all(item["complete_script"] for item in agents["scenarios"]):
                raise ValueError("Actual-model complete role workflows were not verified")
            actual_calls = sum(len(item["model_calls"]) for item in agents["scenarios"])
            if actual_calls <= 0:
                raise ValueError("Actual-model workflows contain no service call evidence")
            record["model_calls"] += actual_calls
            record["agents_real"] = {"path": str(agents_path), "sha256": fingerprint(agents_path), "status": "passed", "real_model": True,
                                     "cases": ["clear-goal", "required-clarification"], "model_calls": actual_calls}
            run("sms-real-model", ["scripts/verify_sms_model.py", "--output", out / "sms-model"], 600)
            sms_path = out / "sms-model/sms-model-validation.json"
            sms = json.loads(sms_path.read_text(encoding="utf-8"))
            if sms.get("status") != "passed" or sms.get("real_model") is not True or sms.get("plaintext_resource_leaks") != 0 or sms.get("guarded_payloads", 0) <= 0 or not sms.get("model_calls"):
                raise ValueError("Actual-model SMS/resource-payload acceptance is incomplete")
            record["model_calls"] += len(sms["model_calls"])
            record["sms_model"] = {"path": str(sms_path), "sha256": fingerprint(sms_path), "status": "passed", "real_model": True,
                                   "guarded_payloads": sms["guarded_payloads"], "plaintext_resource_leaks": 0, "model_calls": len(sms["model_calls"])}
            from scripts.diagnostic_fixtures import EXPECTED, INPUT_VERSION as DIAGNOSIS_VERSION, HOLDOUT
            diagnosis_reports = {}
            for mode, flags in (("fixed", ["--offline"]), ("real", [])):
                run("diagnosis-" + mode, ["scripts/verify_diagnostics.py", *flags, "--output", out / ("diagnosis-" + mode)], 600)
                path = out / ("diagnosis-" + mode) / "diagnostic-validation.json"
                diagnosis = json.loads(path.read_text(encoding="utf-8"))
                exact_scenarios(diagnosis, set(EXPECTED))
                if diagnosis.get("real_model") is not (mode == "real") or diagnosis.get("full_stage_specialty") is not True or diagnosis.get("input_version") != DIAGNOSIS_VERSION or diagnosis.get("browser_evidence_version") != 5:
                    raise ValueError("Diagnosis version/full input scope is incomplete")
                if diagnosis.get("critical_passed") is not True or diagnosis.get("automatic_repairs") != 0 or diagnosis.get("holdout_ids") != sorted(HOLDOUT):
                    raise ValueError("Diagnosis critical/holdout/no-repair expectations changed")
                if not all(item["actual"] == EXPECTED[item["id"]] and item["status"] == "passed" for item in diagnosis["scenarios"]):
                    raise ValueError("Diagnosis frozen expected attribution changed")
                if not all(item.get("resource_consistency_proven") is True for item in diagnosis["scenarios"] if item["id"].startswith("browser-")):
                    raise ValueError("Diagnosis browser resource consistency was not proven")
                diagnosis_reports[mode] = diagnosis
                model_calls = sum(len(item["model_calls"]) for item in diagnosis["scenarios"])
                if mode == "real":
                    if model_calls <= 0:
                        raise ValueError("Actual-model diagnosis contains no service evidence")
                    record["model_calls"] += model_calls
                record["diagnosis_" + mode] = {"path": str(path), "sha256": fingerprint(path), "status": "passed", "real_model": mode == "real",
                                               "input_version": DIAGNOSIS_VERSION, "cases": sorted(EXPECTED), "model_calls": model_calls}
            if diagnosis_reports["fixed"]["fixed_input_hashes"] != diagnosis_reports["real"]["fixed_input_hashes"]:
                raise ValueError("Fixed and actual-model diagnosis used different frozen evidence inputs")
            from scripts.verify_repairs import FIXED_CASES, REAL_CASES, INPUT_VERSION as REPAIR_VERSION
            repair_reports = {}
            for mode, flags in (("fixed", ["--offline"]), ("real", [])):
                run("repairs-" + mode, ["scripts/verify_repairs.py", *flags, "--output", out / ("repairs-" + mode)], 600)
                path = out / ("repairs-" + mode) / "repair-validation.json"
                repairs = json.loads(path.read_text(encoding="utf-8"))
                required = FIXED_CASES if mode == "fixed" else REAL_CASES
                rows = repairs.get("scenarios", [])
                if repairs.get("status") != "passed" or repairs.get("real_model") is not (mode == "real") or repairs.get("full_stage_specialty") is not True or repairs.get("input_version") != REPAIR_VERSION:
                    raise ValueError("Repair version/full input scope is incomplete")
                if len(rows) != len(required) or {item["id"] for item in rows} != required or not all(item["status"] == "passed" and item["expectations_preserved"] for item in rows):
                    raise ValueError("Repair exact cases or original expectations changed")
                if repairs.get("plaintext_resource_leaks") != 0:
                    raise ValueError("Repair privacy acceptance is incomplete")
                repair_reports[mode] = repairs
                model_calls = sum(len(item["model_calls"]) for item in rows)
                if mode == "real":
                    if model_calls <= 0:
                        raise ValueError("Actual-model repair contains no service evidence")
                    record["model_calls"] += model_calls
                record["repairs_" + mode] = {"path": str(path), "sha256": fingerprint(path), "status": "passed", "real_model": mode == "real",
                                             "input_version": REPAIR_VERSION, "cases": sorted(required), "model_calls": model_calls}
            if not all(repair_reports["fixed"]["input_hashes"].get(identity) == digest for identity, digest in repair_reports["real"]["input_hashes"].items()):
                raise ValueError("Fixed and actual-model repairs used different original inputs")
            run("production-agent-graph", ["scripts/verify_agent_graph.py", "--output", out / "agent-graph",
                "--agents-fixed", out / "agents-fixed", "--agents-real", out / "agents-real",
                "--repairs-fixed", out / "repairs-fixed", "--repairs-real", out / "repairs-real"], 600)
            parent_path = out / "agent-graph/agent-graph-validation.json"
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            from scripts.verify_agent_graph import CASES as PARENT_CASES, INPUT_VERSION as PARENT_VERSION
            exact_scenarios(parent, set(PARENT_CASES))
            if parent.get("input_version") != PARENT_VERSION or parent.get("full_stage_specialty") is not True or parent.get("real_model") is not False or parent.get("actual_console_input") is not False:
                raise ValueError("Production parent/child architecture scope/version is incomplete")
            if parent.get("parent_child_native_verified") is not True or parent.get("separated_repair_roles_verified") is not True or parent.get("plaintext_resource_leaks") != 0:
                raise ValueError("Native parent/child and separated repair architecture were not verified")
            record["agent_graph"] = {"path": str(parent_path), "sha256": fingerprint(parent_path), "status": "passed", "input_version": PARENT_VERSION,
                                    "cases": sorted(PARENT_CASES), "actual_console_input": False}
        record["status"] = "passed"
    except Exception as error:
        record["status"] = "failed"
        record["unresolved"].append(safe(f"{type(error).__name__}: {error}"))
    except BaseException:
        record["status"] = "cancelled"
        record["unresolved"].append("Gate cancelled before acceptance")
        raise
    finally:
        # Always verify source/configuration integrity, including failed commands.
        after = sources()
        record["source_files_after"] = after
        record["source_changes_during_verification"] = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        record["protected_files_after"] = configuration_fingerprints()
        try:
            if before != after:
                raise ValueError("Source changed during verification; a new complete gate is required")
            assert_configuration_unchanged(protected)
        except Exception as error:
            record["status"] = "failed"
            record["unresolved"].append(safe(f"{type(error).__name__}: {error}"))
        record["finished"] = now()
        write_json(summary, record)
    print(f"{stage}: {record['status']}; {summary}", flush=True)
    return record, summary


G6_COMMANDS = {
    "offline-regression", "browser-check", "original-smoke", "workflow-compatibility",
    "foreign-check", "foreign-demo", "controlled-shop", "structured-suites",
    "agents-fixed", "agents-real", "resource-lifecycle", "sms-real-model",
    "agent-batch-recovery", "diagnosis-fixed", "diagnosis-real", "repairs-fixed", "repairs-real",
}
FINAL_COMMANDS = {"full-g6", "production-execution-graph", "native-graph-resources", "production-agent-graph"}


def manifest_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_archive(path, expected, digest=None, allowed_extras=()):
    if digest is not None and fingerprint(path) != digest:
        raise ValueError("Source snapshot changed")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not set(expected) <= set(names) or not set(names) - set(expected) <= set(allowed_extras):
            raise ValueError("Source snapshot does not contain its exact manifest")
        for name, sha256 in expected.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != sha256:
                raise ValueError("Source snapshot content differs: " + name)


def verify_final_g6(data, path, before, r0_identities, r4_identities):
    """Check the result of the original G6 verifier; do not recreate its execution."""
    if data.get("stage") != 6 or data.get("gate") != "G6" or data.get("status") != "passed" or data.get("unresolved") != []:
        raise ValueError("The full original G6 gate must pass on the final source")
    if data.get("source_files") != before or sources() != before:
        raise ValueError("G6 was not executed against the same final source")
    commands = data.get("commands", [])
    if len(commands) != len(G6_COMMANDS) or {item["id"] for item in commands} != G6_COMMANDS or not all(item.get("exit_code") == 0 and not item.get("timed_out") for item in commands):
        raise ValueError("The full original G6 command collection is incomplete")
    verify_archive(path.parent / "source.zip", before)
    count, identities = unit_collection((path.parent / "offline-regression.log").read_text(encoding="utf-8"))
    mandatory = R1_CHECKPOINT_TESTS | R2_GRAPH_TESTS | R3_RESOURCE_TESTS | R4_AGENT_TESTS
    if count != data["offline_tests"]["total"] or not (r0_identities | r4_identities | mandatory) <= identities:
        raise ValueError("Final G6 dropped an earlier required test identity")
    resources = data["resources"]
    exact_scenarios(resources, RESOURCE_CASES)
    first = next(item for item in resources["scenarios"] if item["id"] == "sms-first")
    if resources.get("long_wait_verified") is not True or not first["timing"]["human_wait_seconds"] > first["worker_timeout"]:
        raise ValueError("Final G6 did not exceed the actual worker timeout in its human wait")
    if resources.get("privacy", {}).get("plaintext_resource_leaks") != 0 or resources.get("privacy", {}).get("raw_dom_trace_files") != 0:
        raise ValueError("Final G6 resource privacy evidence is incomplete")
    from scripts.diagnostic_fixtures import HOLDOUT
    for mode in ("fixed", "real"):
        if data["diagnosis_" + mode].get("holdout_ids") != sorted(HOLDOUT):
            raise ValueError("Final diagnosis holdout collection changed")
    for group in ("agents_real", "diagnosis_real", "repairs_real"):
        if not sum(len(item["model_calls"]) for item in data[group]["scenarios"]):
            raise ValueError("Final actual-model specialty has no service evidence: " + group)
    if not data["sms_model"].get("model_calls") or data.get("model_calls", 0) <= 0:
        raise ValueError("Final G6 actual-model service evidence is incomplete")
    return count, identities


def final_manual(path, record):
    """Require a new actual console acceptance bound to this exact final source."""
    from scripts.verify_stage import verify_manual
    data = json.loads(path.read_text(encoding="utf-8"))
    started = datetime.fromisoformat(data["started"])
    gate_started = datetime.fromisoformat(record["started"])
    if started.tzinfo is None or started < gate_started:
        raise ValueError("Manual acceptance predates this R5 attempt; start a fresh headed console run")
    if started > datetime.now(started.tzinfo):
        raise ValueError("Manual acceptance has an invalid future start time")
    if sources() != record["source_files"]:
        raise ValueError("Source changed before final manual acceptance")
    assert_configuration_unchanged(record["protected_files"])
    evidence = verify_manual(path)
    if not all(record["source_files"].get(name) == sha256 for name, sha256 in evidence["source_files"].items()):
        raise ValueError("Manual CORE does not match this R5 final snapshot")
    evidence.update(started=data["started"], r5_run_id=record["run_id"],
                    final_source_manifest_sha256=record["source_manifest_sha256"])
    return evidence


def check_final_integrity(record):
    if sources() != record["source_files"]:
        raise ValueError("Final source changed; a complete new R5 gate is required")
    assert_configuration_unchanged(record["protected_files"])
    if fingerprint(ROOT / "uv.lock") != record["lock_sha256"]:
        raise ValueError("Final dependency lock changed")


def check_automatic_evidence(record):
    manifest = record.get("automatic_evidence_manifest", {})
    if not manifest or record.get("automatic_evidence_manifest_sha256") != manifest_digest(manifest):
        raise ValueError("Final automatic evidence manifest is missing or changed")
    required = {str(Path(record[key]["path"]).resolve()) for key in ("g6", "execution_graph", "graph_resources", "agent_graph", "source_archive")}
    required.update(str(Path(item["log"]).resolve()) for item in record.get("commands", []) if "log" in item)
    if not required <= manifest.keys():
        raise ValueError("A required final report, source archive or command log is absent from the frozen evidence manifest")
    for key in ("g6", "execution_graph", "graph_resources", "agent_graph", "source_archive"):
        item = record[key]
        if manifest[str(Path(item["path"]).resolve())] != item["sha256"]:
            raise ValueError("Final report reference is inconsistent: " + key)
    for name, digest in manifest.items():
        path = Path(name).resolve()
        if not path.is_relative_to((ROOT / "runtime/development").resolve()) or fingerprint(path) != digest:
            raise ValueError("Final automatic evidence changed: " + name)
    verify_archive(Path(record["source_archive"]["path"]), record["source_files"], record["archive_sha256"])
    if manifest_digest(record["source_files"]) != record["source_manifest_sha256"]:
        raise ValueError("Final source manifest is inconsistent")
    commands = record.get("commands", [])
    if len(commands) != len(FINAL_COMMANDS) or {item["id"] for item in commands} != FINAL_COMMANDS or not all(item.get("exit_code") == 0 and not item.get("timed_out") for item in commands):
        raise ValueError("Final automatic command evidence is incomplete")
    for key in ("baseline", "previous_gate", "g6_baseline"):
        item = record[key]
        if fingerprint(Path(item["path"])) != item["sha256"]:
            raise ValueError("A preceding bound gate changed")
    previous, r4_ids = verified_previous_gate(Path(record["previous_gate"]["path"]), "R4")
    _, r0_ids = unit_collection((Path(record["baseline"]["path"]).parent / "offline-regression.log").read_text(encoding="utf-8"))
    g6_path = Path(record["g6"]["path"])
    verify_final_g6(json.loads(g6_path.read_text(encoding="utf-8")), g6_path, record["source_files"], r0_ids, r4_ids)


def complete_final_manual(record, summary, manual_path, wait_for_manual):
    """Waiting has its own status; it can never produce a successful R5 exit."""
    record.setdefault("automatic_completed", now())
    record.update(status="waiting_user", automatic_status="passed", manual_required=True,
                  manual_path=str(manual_path) if manual_path else None,
                  unresolved=["Fresh headed first and independent-replay console acceptance is required; R5 is not passed"])
    write_json(summary, record)
    print(f"[{now()}] R5 automatic verification passed; waiting for fresh actual console acceptance", flush=True)
    deadline = time.monotonic() + wait_for_manual
    while True:
        check_final_integrity(record)
        if manual_path and manual_path.exists():
            try:
                status = json.loads(manual_path.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError):
                status = "running"
            if status != "running":
                check_automatic_evidence(record)
                record["manual"] = final_manual(manual_path, record)
                record.update(status="passed", unresolved=[], completed=now())
                return
        if time.monotonic() >= deadline:
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def final_redactor():
    settings, redactor = Settings.load(), Redactor()
    for value in (settings.deepseek_api_key, settings.qwen_api_key, "15500001111", "15500000001",
                  "SentinelAccount_8de50", "Sentinel-P@ss-3f67!"):
        if value:
            redactor.register(value)
    return settings, lambda value: redactor.text(settings.redact(str(value)))


def finish_final_record(record, summary, safe):
    after = sources()
    record["source_files_after"] = after
    record["source_changes_during_verification"] = sorted(path for path in record["source_files"].keys() | after.keys() if record["source_files"].get(path) != after.get(path))
    record["protected_files_after"] = configuration_fingerprints()
    try:
        check_final_integrity(record)
    except Exception as error:
        record.update(status="failed", unresolved=[*record.get("unresolved", []), safe(f"{type(error).__name__}: {error}")])
    record["finished"] = now()
    write_json(summary, record)
    print(f"R5: {record['status']}; {summary}", flush=True)


def verify_final(baseline_path, previous_path, g6_baseline, manual_path=None, wait_for_manual=0):
    settings, safe = final_redactor()
    run_id = "R5-" + now()[:19].replace("-", "").replace(":", "").replace("T", "-") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / "runtime/development/refactor" / run_id
    out.mkdir(parents=True, exist_ok=False)
    summary = out / "verification.json"
    before, protected = sources(), configuration_fingerprints()
    record = {"schema_version": 1, "stage": "R5", "run_id": run_id, "status": "running", "started": now(),
              "source_files": before, "protected_files": protected, "source_manifest_sha256": manifest_digest(before),
              "commands": [], "model_calls": 0, "manual_required": True, "unresolved": [],
              "gate_scope": "Same final source: full original G6, all graph specialties, fresh headed actual console first/replay; pause before G7."}
    write_json(summary, record)

    def run(identity, arguments, timeout=600):
        entry = {"id": identity, "command": [sys.executable, *map(str, arguments)], "cwd": str(ROOT),
                 "started": now(), "expected_exit_code": 0, "log": str(out / (identity + ".log"))}
        record["commands"].append(entry)
        write_json(summary, record)
        print(f"[{now()}] {identity}", flush=True)
        started = time.monotonic()
        try:
            process = subprocess.Popen(entry["command"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                raw, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                entry["timed_out"] = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                raw, _ = process.communicate()
            entry["exit_code"] = process.returncode
            Path(entry["log"]).write_text(safe(raw.decode("utf-8", errors="replace")), encoding="utf-8")
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(summary, record)
        if entry.get("timed_out") or entry.get("exit_code") != 0:
            raise ValueError(identity + " failed; see " + entry["log"])

    try:
        if not 0 <= wait_for_manual <= 3600:
            raise ValueError("Manual wait must be between 0 and 3600 seconds")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("stage") != "R0" or baseline.get("status") != "passed":
            raise ValueError("R5 requires the passed R0 baseline")
        required_baseline = {"offline-regression", "browser-check", "original-smoke", "workflow-compatibility"}
        commands = baseline.get("commands", [])
        if len(commands) != len(required_baseline) or {item["id"] for item in commands} != required_baseline or not all(item.get("exit_code") == 0 and not item.get("timed_out") for item in commands):
            raise ValueError("R0 command evidence is incomplete")
        # R0 intentionally archived append-only progress as well; its whole-zip SHA stays mandatory.
        verify_archive(baseline_path.parent / "source-baseline.zip", baseline["source_files"], baseline["archive_sha256"],
                       allowed_extras={"docs/DEVELOPMENT_PROGRESS.md"})
        _, r0_ids = unit_collection((baseline_path.parent / "offline-regression.log").read_text(encoding="utf-8"))
        previous, previous_ids = verified_previous_gate(previous_path, "R4")
        expected_g6_baseline = original_g6_baseline()
        g6_baseline = expected_g6_baseline if g6_baseline is None else Path(g6_baseline).resolve()
        if g6_baseline != expected_g6_baseline:
            raise ValueError("R5 must reuse the original S0 baseline bound by the historical passed G6")
        record["baseline"] = {"path": str(baseline_path), "sha256": fingerprint(baseline_path)}
        record["previous_gate"] = {"path": str(previous_path), "sha256": fingerprint(previous_path), "archive_sha256": previous["archive_sha256"]}
        record["g6_baseline"] = {"path": str(g6_baseline), "sha256": fingerprint(g6_baseline)}
        record["lock_sha256"] = fingerprint(ROOT / "uv.lock")
        if record["lock_sha256"] != baseline["source_files"]["uv.lock"]:
            raise ValueError("Dependency lock changed after R0")
        record["changes_since_previous"] = {"added": sorted(before.keys() - previous["source_files"].keys()),
            "removed": sorted(previous["source_files"].keys() - before.keys()),
            "modified": sorted(path for path in before.keys() & previous["source_files"].keys() if before[path] != previous["source_files"][path])}
        record["configuration_changes_before_verification"] = sorted(path for path in protected.keys() | baseline["protected_files"].keys() if protected.get(path) != baseline["protected_files"].get(path))
        with zipfile.ZipFile(out / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in before:
                archive.write(ROOT / path, path)
        record["archive_sha256"] = fingerprint(out / "source.zip")
        record["source_archive"] = {"path": str(out / "source.zip"), "sha256": record["archive_sha256"]}
        # Reuse the original complete G6 implementation, including its baseline/previous-gate rules.
        from scripts.verify_stage import verify as verify_g6
        entry = {"id": "full-g6", "implementation": "scripts.verify_stage.verify", "stage": 6,
                 "baseline": str(g6_baseline), "started": now(), "expected_exit_code": 0}
        record["commands"].append(entry)
        write_json(summary, record)
        began = time.monotonic()
        g6, g6_path = verify_g6(6, g6_baseline)
        entry.update(exit_code=0 if g6.get("status") == "passed" else 1,
                     duration_seconds=round(time.monotonic() - began, 3), verification=str(g6_path))
        record["g6"] = {"path": str(g6_path), "sha256": fingerprint(g6_path), "status": g6["status"],
                        "archive_sha256": fingerprint(g6_path.parent / "source.zip") if (g6_path.parent / "source.zip").is_file() else None}
        write_json(summary, record)
        total, identities = verify_final_g6(g6, g6_path, before, r0_ids, previous_ids)
        record["offline_tests"] = {"total": total, "passed": total, "failed": 0, "skipped": 0,
                                   "r0_identities_preserved": True, "r4_identities_preserved": True, "identities": sorted(identities)}
        record["model_calls"] = g6["model_calls"]
        record["dependencies"] = g6["dependencies"]
        run("production-execution-graph", ["scripts/verify_execution_graph.py", "--output", out / "execution-graph"])
        run("native-graph-resources", ["scripts/verify_graph_resources.py", "--output", out / "graph-resources"])
        run("production-agent-graph", ["scripts/verify_agent_graph.py", "--output", out / "agent-graph",
            "--agents-fixed", g6_path.parent / "agents-fixed", "--agents-real", g6_path.parent / "agents-real",
            "--repairs-fixed", g6_path.parent / "repairs-fixed", "--repairs-real", g6_path.parent / "repairs-real"])
        from scripts.verify_execution_graph import CASES as CHILD_CASES, INPUT_VERSION as CHILD_VERSION
        from scripts.verify_graph_resources import CASES as RESOURCE_CASES_NATIVE, INPUT_VERSION as RESOURCE_VERSION
        from scripts.verify_agent_graph import CASES as PARENT_CASES, INPUT_VERSION as PARENT_VERSION
        for key, folder, filename, cases, version in (
            ("execution_graph", "execution-graph", "execution-graph-validation.json", CHILD_CASES, CHILD_VERSION),
            ("graph_resources", "graph-resources", "graph-resource-validation.json", RESOURCE_CASES_NATIVE, RESOURCE_VERSION),
            ("agent_graph", "agent-graph", "agent-graph-validation.json", PARENT_CASES, PARENT_VERSION)):
            path = out / folder / filename
            data = json.loads(path.read_text(encoding="utf-8"))
            exact_scenarios(data, set(cases))
            if data.get("input_version") != version or data.get("full_stage_specialty") is not True:
                raise ValueError("Final graph specialty version/scope is incomplete: " + key)
            if data.get("real_model", False) is not False or data.get("actual_console_input", False) is not False:
                raise ValueError("Fixed graph specialties cannot claim real-model/actual-human acceptance")
            if key == "execution_graph":
                if data.get("model_calls") != 0 or data.get("native_human_resume_verified") is not False or data.get("separated_repair_roles_verified") is not False or not all(item.get("architecture_verified") is True and item.get("requirements_preserved") is True for item in data["scenarios"]):
                    raise ValueError("Final execution graph evidence is incomplete")
            elif key == "graph_resources":
                if data.get("model_calls") != 0 or data.get("native_human_resume_verified") is not True or data.get("separated_repair_roles_verified") is not False or data.get("plaintext_resource_leaks") != 0 or not all(item.get("native_graph_verified") is True and item.get("privacy_verified") is True for item in data["scenarios"]):
                    raise ValueError("Final native resource/privacy evidence is incomplete")
            elif data.get("parent_child_native_verified") is not True or data.get("separated_repair_roles_verified") is not True or data.get("plaintext_resource_leaks") != 0:
                raise ValueError("Final parent/child role separation evidence is incomplete")
            record[key] = {"path": str(path), "sha256": fingerprint(path), "status": "passed", "input_version": version, "cases": sorted(cases)}
            write_json(summary, record)
        check_final_integrity(record)
        files = [out / "source.zip"]
        for directory in (g6_path.parent, out / "execution-graph", out / "graph-resources", out / "agent-graph"):
            files.extend(path for path in directory.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
        files.extend(Path(item["log"]) for item in record["commands"] if "log" in item)
        record["automatic_evidence_manifest"] = {str(path.resolve()): fingerprint(path) for path in sorted(set(files))}
        record["automatic_evidence_manifest_sha256"] = manifest_digest(record["automatic_evidence_manifest"])
        complete_final_manual(record, summary, manual_path, wait_for_manual)
    except Exception as error:
        record.update(status="failed", unresolved=[safe(f"{type(error).__name__}: {error}")])
    except BaseException:
        record.update(status="cancelled", unresolved=["Final gate cancelled before acceptance"])
        raise
    finally:
        finish_final_record(record, summary, safe)
    return record, summary


def resume_final(summary, manual_path=None, wait_for_manual=0):
    """Continue only the human boundary of an unchanged, fully automatic-passed R5."""
    _, safe = final_redactor()
    summary = summary.resolve()
    if not summary.is_relative_to((ROOT / "runtime/development/refactor").resolve()):
        raise ValueError("Resume must select a generated refactor verification.json")
    record = json.loads(summary.read_text(encoding="utf-8"))
    if record.get("stage") != "R5" or record.get("status") != "waiting_user" or record.get("automatic_status") != "passed":
        raise ValueError("Only an automatic-passed R5 waiting for human input can resume")
    try:
        if not 0 <= wait_for_manual <= 3600:
            raise ValueError("Manual wait must be between 0 and 3600 seconds")
        check_final_integrity(record)
        check_automatic_evidence(record)
        record.setdefault("continuations", []).append({"started": now(), "manual_path": str(manual_path) if manual_path else record.get("manual_path")})
        complete_final_manual(record, summary, manual_path or (Path(record["manual_path"]) if record.get("manual_path") else None), wait_for_manual)
    except Exception as error:
        record.update(status="failed", unresolved=[safe(f"{type(error).__name__}: {error}")])
    except BaseException:
        record.update(status="cancelled", unresolved=["Final gate cancelled while waiting for human acceptance"])
        raise
    finally:
        finish_final_record(record, summary, safe)
    return record, summary


def original_g6_baseline():
    historical = ROOT / "runtime/development/stage-6/20261017-003004-9fccb5b5/verification.json"
    data = json.loads(historical.read_text(encoding="utf-8"))
    if data.get("stage") != 6 or data.get("status") != "passed":
        raise ValueError("The historical passed G6 baseline reference is unavailable")
    baseline = Path(data["baseline"]).resolve()
    if fingerprint(baseline) != data.get("baseline_sha256"):
        raise ValueError("The historical G6-bound S0 baseline has changed")
    return baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=str.upper, choices=["R1", "R2", "R3", "R4", "R5"])
    parser.add_argument("--baseline", type=Path, help="Passed R0 verification.json; defaults to the latest R0 attempt")
    parser.add_argument("--previous", type=Path, help="Passed preceding refactor verification.json; defaults to the latest preceding-stage attempt")
    parser.add_argument("--g6-baseline", type=Path, help="Original passed S0 baseline.json for the unchanged G6 verifier")
    parser.add_argument("--manual-evidence", type=Path, help="Fresh headed actual-console manual-evidence.json required for R5")
    parser.add_argument("--wait-for-manual", type=int, default=0, help="Wait 0..3600 seconds after all automatic checks; zero leaves waiting_user")
    parser.add_argument("--resume-final", type=Path, help="Resume only human acceptance of an unchanged R5 waiting_user verification.json")
    args = parser.parse_args()
    if args.stage not in IMPLEMENTED:
        parser.error(f"{args.stage} is not implemented: {PENDING[args.stage]}")
    if not 0 <= args.wait_for_manual <= 3600:
        parser.error("Manual wait must be between 0 and 3600 seconds")
    if args.resume_final:
        if args.stage != "R5":
            parser.error("--resume-final only applies to R5")
        record, _ = resume_final(args.resume_final, args.manual_evidence, args.wait_for_manual)
        return 0 if record["status"] == "passed" else 1
    if args.stage != "R5" and (args.g6_baseline or args.manual_evidence or args.wait_for_manual):
        parser.error("Final G6/manual options only apply to R5")
    baselines = sorted((ROOT / "runtime/development/refactor").glob("R0-*/verification.json"))
    if not args.baseline and not baselines:
        parser.error("Capture and pass R0 before running any refactor gate")
    previous = args.previous
    if args.stage in {"R2", "R3", "R4", "R5"} and previous is None:
        preceding = {"R2": "R1", "R3": "R2", "R4": "R3", "R5": "R4"}[args.stage]
        previous_runs = sorted((ROOT / "runtime/development/refactor").glob(preceding + "-*/verification.json"))
        if not previous_runs:
            parser.error("Pass " + preceding + " before selecting " + args.stage)
        previous = previous_runs[-1]
    g6_baseline = args.g6_baseline
    if args.stage == "R5" and g6_baseline is None:
        g6_baseline = original_g6_baseline()
    record, _ = verify(args.stage, (args.baseline or baselines[-1]).resolve(), previous.resolve() if previous else None,
                       g6_baseline=g6_baseline.resolve() if g6_baseline else None,
                       manual_path=args.manual_evidence.resolve() if args.manual_evidence else None,
                       wait_for_manual=args.wait_for_manual)
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
