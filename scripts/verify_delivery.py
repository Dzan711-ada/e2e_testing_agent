"""G7 delivery gate: fresh full G6, graphs, comparison, public site and real human acceptance."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings
from e2e_agent.resources import Redactor
from scripts.verify_refactor import (G6_COMMANDS, exact_scenarios, manifest_digest, original_g6_baseline,
                                     unit_collection, verify_archive, verify_final_g6)
from scripts.verify_stage import (assert_configuration_unchanged, configuration_fingerprints,
                                  fingerprint, now, sources, verify_manual, write_json)

REQUIRED_SECTIONS = {"summary", "scenarios", "coverage", "human", "diagnosis", "repairs", "replay", "artifacts"}
COMMANDS = {"full-g6", "production-execution-graph", "native-graph-resources", "production-agent-graph",
            "repeated-comparison", "public-site", "delivery-reports"}
G7_TESTS = {
    *{"test_delivery_integration.DeliveryIntegrationTests.test_" + name for name in (
        "disabled_diagnosis_controls_actual_parent_route_without_model_call", "fixture_faults_are_private_services_not_graph_or_model_input",
        "selected_profile_url_policy_restricts_routes_and_keeps_local_prefix", "public_export_contains_refs_without_demo_values_or_model_modules",
        "privacy_scan_accepts_exact_report_and_rejects_renamed_or_tampered_html", "cli_routes_selected_site_to_new_graph_and_rejects_mixed_scope",
        "public_profile_masks_demo_values_before_first_native_checkpoint",
        "control_text_compiler_anchors_and_escapes_observed_refs")},
    *{"test_comparison.RepeatedComparisonTests.test_" + name for name in (
        "fixed_inputs_are_identical_across_arms_and_cannot_raise_budgets", "complete_grid_preserves_expected_failures_and_denominators",
        "failed_repeat_is_retained_instead_of_best_run_selection", "missing_or_duplicate_samples_cannot_satisfy_complete_grid",
        "changed_input_or_model_configuration_is_rejected", "product_false_pass_and_non_equivalent_repair_are_rejected",
        "original_baseline_is_explicitly_noncomparable")},
    *{"test_site_profiles.SiteProfilePolicyTests.test_" + name for name in (
        "profile_is_exact_confirmed_target_and_url_route_allowlist", "resource_values_exist_only_in_separate_project_provider",
        "profile_recognition_requires_exact_start_and_contract_source", "fixed_two_case_contract_accepts_design_and_complete_typed_actions",
        "frozen_checks_cannot_be_dropped_weakened_or_relabelled", "resource_permissions_lifecycle_and_initial_context_are_frozen",
        "budget_caps_and_unsupported_business_scope_are_not_silently_reduced", "practice_schema_keeps_actions_empty_and_target_fixed_local_default_unchanged")},
    *{"test_site_profiles.PracticeRoleProtocolTests.test_" + name for name in (
        "design_role_uses_selected_profile_without_public_credential_values", "execution_role_plans_both_typed_protocols_from_observed_controls",
        "execution_rejects_wrong_resource_literal_scope_and_unobserved_control")},
    *{"test_suite_reporting.SuiteReportingTests.test_" + name for name in (
        "actual_record_shapes_preserve_first_failure_repair_replay_difference_and_diagnosis",
        "failed_assertion_is_executed_coverage_and_blocked_check_is_missing",
        "passed_record_without_matching_required_evidence_is_flagged_without_overwriting_status",
        "html_and_markdown_neutralize_untrusted_text_and_do_not_embed_unused_private_fields",
        "waits_and_scripted_resources_do_not_claim_actual_human_acceptance",
        "stale_or_foreign_replay_record_is_not_used_to_invent_checkpoint_results",
        "cancelled_partial_plan_keeps_unexecuted_checkpoints_and_missing_replay_explicit",
        "writer_only_changes_three_report_artifacts_and_links_only_local_existing_safe_artifacts",
        "generated_html_requires_owned_input_exact_bytes_and_exact_filename",
        "report_input_cannot_introduce_script_urls_or_raw_executable_markup",
        "non_json_live_handles_are_rejected_and_module_has_only_stdlib_dependencies")},
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_path(value):
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def bound(path):
    return {"path": str(path.resolve()), "sha256": fingerprint(path)}


def check_historical_r5(path):
    """Verify the historical boundary against its own source, never today's CORE."""
    data = load(path)
    if data.get("stage") != "R5" or data.get("status") != "passed" or data.get("automatic_status") != "passed" or data.get("unresolved") != []:
        raise ValueError("G7 requires a genuinely passed final R5")
    if data["source_files"] != data.get("source_files_after") or data.get("source_changes_during_verification") != [] or data["protected_files"] != data.get("protected_files_after"):
        raise ValueError("R5 source/configuration integrity did not pass")
    verify_archive(evidence_path(data["source_archive"]["path"]), data["source_files"], data["archive_sha256"])
    manifest = data["automatic_evidence_manifest"]
    if manifest_digest(manifest) != data.get("automatic_evidence_manifest_sha256"):
        raise ValueError("R5 automatic evidence manifest changed")
    for value, sha256 in manifest.items():
        item = evidence_path(value)
        if not item.is_relative_to((ROOT / "runtime/development").resolve()) or fingerprint(item) != sha256:
            raise ValueError("R5 automatic evidence changed")
    commands = data.get("commands", [])
    expected = {"full-g6", "production-execution-graph", "native-graph-resources", "production-agent-graph"}
    if len(commands) != len(expected) or {x["id"] for x in commands} != expected or not all(x.get("exit_code") == 0 and not x.get("timed_out") for x in commands):
        raise ValueError("R5 exact command collection is incomplete")
    g6_path = evidence_path(data["g6"]["path"])
    if fingerprint(g6_path) != data["g6"]["sha256"]:
        raise ValueError("R5 G6 evidence changed")
    g6 = load(g6_path)
    if g6.get("status") != "passed" or g6["source_files"] != data["source_files"] or {c["id"] for c in g6["commands"]} != G6_COMMANDS:
        raise ValueError("R5 did not bind a full same-source G6")
    count, identities = unit_collection((g6_path.parent / "offline-regression.log").read_text(encoding="utf-8"))
    if count != data["offline_tests"]["total"] or identities != set(data["offline_tests"]["identities"]):
        raise ValueError("R5 test identities do not match the completed regression")
    manual_ref = data["manual"]
    manual_path = evidence_path(manual_ref["path"])
    if fingerprint(manual_path) != manual_ref["sha256"]:
        raise ValueError("R5 actual human evidence changed")
    manual = load(manual_path)
    if not (manual.get("stage") == 4 and manual.get("status") == "passed" and manual.get("actual_console_input") is True and
            manual.get("provider") == "ConsoleProvider" and manual.get("headless") is False and
            manual.get("input_channel") == "parent_console_stdin" and manual.get("resource_mode") == "public_fictional_local_sms"):
        raise ValueError("R5 actual human evidence has the wrong channel or outcome")
    if manual["source_files"] != manual_ref["source_files"] or not all(data["source_files"].get(name) == sha for name, sha in manual["source_files"].items()):
        raise ValueError("Historical R5 manual CORE differs from its own source snapshot")
    rounds, requests = manual["rounds"], manual["console_requests"]
    if len(rounds) != 2 or {r["id"] for r in rounds} != {"first", "independent-replay"} or len({r["run_id"] for r in rounds}) != 2 or not all(r["status"] == "passed" for r in rounds):
        raise ValueError("Historical R5 human rounds are incomplete")
    if len(requests) != 4 or [r["kind"] for r in requests] != ["phone", "sms_code", "phone", "sms_code"] or not all(r["status"] == "provided" for r in requests) or requests[1]["challenge_id"] == requests[3]["challenge_id"]:
        raise ValueError("Historical R5 human resource lineage is incomplete")
    if manual["replay"]["status"] != "passed" or manual["replay"]["tests"] != 1 or datetime.fromisoformat(manual["started"]) < datetime.fromisoformat(data["started"]):
        raise ValueError("Historical R5 human replay or freshness failed")
    if manual_ref.get("final_source_manifest_sha256") != data["source_manifest_sha256"]:
        raise ValueError("R5 human evidence was not bound to its final source")
    return data, identities


def integrity(record):
    if sources() != record["source_files"]:
        raise ValueError("G7 source changed; the full delivery gate must rerun")
    assert_configuration_unchanged(record["protected_files"])
    if fingerprint(ROOT / "uv.lock") != record["lock_sha256"]:
        raise ValueError("G7 dependency lock changed")


def redactor():
    settings, redact = Settings.load(), Redactor()
    for item in (settings.deepseek_api_key, settings.qwen_api_key, "15500000001", "15500001111"):
        if item:
            redact.register(item)
    return lambda value: redact.text(settings.redact(str(value)))


def run_command(record, summary, identity, arguments, *, timeout=600, allowed=(0,)):
    safe = redactor()
    entry = {"id": identity, "command": [sys.executable, *map(str, arguments)], "cwd": str(ROOT),
             "started": now(), "log": str(summary.parent / (identity + ".log")), "allowed_exit_codes": list(allowed)}
    record["commands"].append(entry)
    write_json(summary, record)
    print(f"[{now()}] {identity}", flush=True)
    began = time.monotonic()
    try:
        child = subprocess.Popen(entry["command"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            raw, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            entry["timed_out"] = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                child.kill()
            raw, _ = child.communicate()
        entry["exit_code"] = child.returncode
        Path(entry["log"]).write_text(safe(raw.decode("utf-8", errors="replace")), encoding="utf-8")
    finally:
        entry["duration_seconds"] = round(time.monotonic() - began, 3)
        write_json(summary, record)
    if entry.get("timed_out") or entry.get("exit_code") not in allowed:
        raise ValueError(identity + " did not complete; see " + entry["log"])
    return entry["exit_code"]


def validate_reports(path, allowed_roots):
    data = load(path)
    if data.get("status") != "passed" or data.get("input_version") != 1 or data.get("schema_version") != 1 or not data.get("checked_reports"):
        raise ValueError("Actual delivery report integration did not pass")
    if not REQUIRED_SECTIONS <= set(data.get("required_sections", [])) or data.get("plaintext_resource_leaks") != 0 or data.get("escaped_untrusted_html") is not True:
        raise ValueError("Delivery report safety/required sections are incomplete")
    from e2e_agent.suite_reporting import is_generated_report
    for item in data["checked_reports"]:
        directory = evidence_path(item["run_dir"])
        if not any(directory.is_relative_to(root.resolve()) for root in allowed_roots) or item.get("unaltered") is not True:
            raise ValueError("Report integration used an old/foreign run or changed business status")
        json_path = evidence_path(item.get("json_path", directory / "agent-result.json"))
        if not any(json_path.is_relative_to(root.resolve()) for root in allowed_roots) or fingerprint(json_path) != item["json_sha256"] or load(json_path).get("status") != item["business_status"]:
            raise ValueError("Report integration changed or misreported its actual business JSON")
        if fingerprint(directory / "report.md") != item["markdown_sha256"] or fingerprint(directory / "report.html") != item["html_sha256"] or not is_generated_report(directory / "report.html"):
            raise ValueError("Controlled report content differs from its actual input")
    return data


def automatic_evidence(record):
    manifest = record.get("automatic_evidence_manifest", {})
    if not manifest or manifest_digest(manifest) != record.get("automatic_evidence_manifest_sha256"):
        raise ValueError("G7 automatic evidence manifest is missing or changed")
    for key in ("g6", "execution_graph", "graph_resources", "agent_graph", "comparison", "public", "reports", "source_archive"):
        item = record[key]
        path = evidence_path(item["path"])
        if manifest.get(str(path)) != item["sha256"]:
            raise ValueError("A required G7 report is missing from the immutable manifest")
    if not {str(evidence_path(c["log"])) for c in record.get("commands", []) if "log" in c} <= manifest.keys():
        raise ValueError("A required automatic G7 command log is absent from the manifest")
    for value, sha in manifest.items():
        path = evidence_path(value)
        if not path.is_relative_to((ROOT / "runtime").resolve()) or fingerprint(path) != sha:
            raise ValueError("An immutable G7 automatic artifact changed")
    verify_archive(evidence_path(record["source_archive"]["path"]), record["source_files"], record["archive_sha256"])
    if manifest_digest(record["source_files"]) != record["source_manifest_sha256"]:
        raise ValueError("G7 source manifest is inconsistent")
    for key in ("baseline", "previous_gate"):
        if fingerprint(evidence_path(record[key]["path"])) != record[key]["sha256"]:
            raise ValueError("A bound G7 preceding record changed")
    baseline_path = evidence_path(record["baseline"]["path"])
    baseline = load(baseline_path)
    if (baseline.get("stage") != 7 or baseline.get("kind") != "preimplementation_snapshot" or
            baseline.get("status") != "captured_not_gate_passed" or
            evidence_path(baseline["previous_gate"]["path"]) != evidence_path(record["previous_gate"]["path"]) or
            baseline["previous_gate"]["sha256"] != record["previous_gate"]["sha256"]):
        raise ValueError("G7 preimplementation snapshot no longer binds the accepted preceding gate")
    verify_archive(baseline_path.parent / "source-baseline.zip", baseline["source_files"], baseline["archive_sha256"])
    _, old_ids = check_historical_r5(evidence_path(record["previous_gate"]["path"]))
    g6_path = evidence_path(record["g6"]["path"])
    _, identities = verify_final_g6(load(g6_path), g6_path, record["source_files"], old_ids, old_ids)
    if not G7_TESTS <= identities:
        raise ValueError("A mandatory final G7 test identity is missing")
    if len(record["commands"]) != len(COMMANDS) or {c["id"] for c in record["commands"]} != COMMANDS or not all(c.get("exit_code") == 0 and not c.get("timed_out") for c in record["commands"]):
        raise ValueError("G7 automatic command set is incomplete or blocked")


def manual_boundary(record, summary, manual_path, wait_for_manual):
    record.update(status="waiting_user", automatic_status="passed", manual_required=True,
                  manual_path=str(manual_path) if manual_path else None,
                  unresolved=["Fresh same-source headed actual ConsoleProvider first/replay acceptance is required; G7 is not passed"])
    record.setdefault("automatic_completed", now())
    write_json(summary, record)
    deadline = time.monotonic() + wait_for_manual
    while True:
        integrity(record)
        if manual_path and manual_path.exists():
            try:
                status = load(manual_path).get("status")
            except (OSError, ValueError):
                status = "running"
            if status != "running":
                automatic_evidence(record)
                raw = load(manual_path)
                began = datetime.fromisoformat(raw["started"])
                if began.tzinfo is None or not datetime.fromisoformat(record["started"]) <= began <= datetime.now(began.tzinfo):
                    raise ValueError("G7 manual acceptance must start after this G7 attempt, with a valid timezone")
                manual = verify_manual(manual_path)
                if not all(record["source_files"].get(name) == sha for name, sha in manual["source_files"].items()):
                    raise ValueError("Current G7 manual CORE differs from final source")
                manual.update(started=raw["started"], g7_run_id=record["run_id"], final_source_manifest_sha256=record["source_manifest_sha256"])
                record["manual"] = manual
                # Human reporting creates new evidence; immutable automatic reports are untouched.
                separate = {"commands": []}
                run_command(separate, summary.parent / "manual-report-execution.json", "manual-delivery-reports",
                    ["scripts/verify_delivery_reports.py", "--manual-evidence", manual_path, "--output", summary.parent / "manual-reports"])
                report_path = summary.parent / "manual-reports/delivery-reports-validation.json"
                validate_reports(report_path, [summary.parent / "manual-reports", manual_path.parent])
                record["manual_reports"] = bound(report_path)
                record["manual_report_commands"] = separate["commands"]
                human_files = {p for root in (summary.parent / "manual-reports", manual_path.parent) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
                human_files.update({summary.parent / "manual-report-execution.json", *(Path(c["log"]) for c in separate["commands"])})
                record["manual_evidence_manifest"] = {str(p.resolve()): fingerprint(p) for p in sorted(human_files)}
                record["manual_evidence_manifest_sha256"] = manifest_digest(record["manual_evidence_manifest"])
                if fingerprint(manual_path) != record["manual"]["sha256"]:
                    raise ValueError("Fresh manual evidence changed during report integration")
                integrity(record)
                automatic_evidence(record)
                record.update(status="passed", unresolved=[], completed=now())
                return
        if time.monotonic() >= deadline:
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def finish(record, summary):
    record["source_files_after"] = sources()
    record["source_changes_during_verification"] = sorted(p for p in record["source_files"].keys() | record["source_files_after"].keys() if record["source_files"].get(p) != record["source_files_after"].get(p))
    record["protected_files_after"] = configuration_fingerprints()
    try:
        integrity(record)
    except Exception as error:
        record.update(status="failed", unresolved=[*record.get("unresolved", []), redactor()(str(error))])
    record["finished"] = now()
    write_json(summary, record)
    print(f"G7: {record['status']}; {summary}", flush=True)


def verify(baseline_path, previous_path=None, manual_path=None, wait_for_manual=0):
    run_id = now()[:19].replace("-", "").replace(":", "").replace("T", "-") + "-" + uuid.uuid4().hex[:8]
    out = ROOT / "runtime/development/stage-7" / run_id
    out.mkdir(parents=True, exist_ok=False)
    summary = out / "verification.json"
    before = sources()
    record = {"schema_version": 1, "stage": 7, "gate": "G7", "run_id": run_id, "started": now(), "status": "running",
              "source_files": before, "source_manifest_sha256": manifest_digest(before),
              "protected_files": configuration_fingerprints(), "lock_sha256": fingerprint(ROOT / "uv.lock"),
              "commands": [], "model_calls": 0, "unresolved": [], "manual_required": True}
    write_json(summary, record)
    try:
        if not 0 <= wait_for_manual <= 3600:
            raise ValueError("Manual wait must be 0..3600 seconds")
        baseline = load(baseline_path)
        if baseline.get("stage") != 7 or baseline.get("kind") != "preimplementation_snapshot" or baseline.get("status") != "captured_not_gate_passed":
            raise ValueError("G7 needs its captured preimplementation source baseline")
        verify_archive(baseline_path.parent / "source-baseline.zip", baseline["source_files"], baseline["archive_sha256"])
        previous_path = evidence_path(previous_path or baseline["previous_gate"]["path"])
        if fingerprint(previous_path) != baseline["previous_gate"]["sha256"]:
            raise ValueError("G7 baseline no longer binds the passed R5")
        previous, old_ids = check_historical_r5(previous_path)
        if baseline["source_files"] != previous["source_files"] or record["lock_sha256"] != previous["lock_sha256"]:
            raise ValueError("G7 baseline/source lock does not match R5")
        record.update(baseline=bound(baseline_path), previous_gate=bound(previous_path))
        record["configuration_changes_before_verification"] = sorted(p for p in record["protected_files"].keys() | baseline["protected_files"].keys() if record["protected_files"].get(p) != baseline["protected_files"].get(p))
        record["changes_since_R5"] = {"added": sorted(before.keys() - previous["source_files"].keys()),
            "removed": sorted(previous["source_files"].keys() - before.keys()),
            "modified": sorted(p for p in before.keys() & previous["source_files"].keys() if before[p] != previous["source_files"][p])}
        with zipfile.ZipFile(out / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name in before:
                archive.write(ROOT / name, name)
        record["archive_sha256"] = fingerprint(out / "source.zip")
        record["source_archive"] = bound(out / "source.zip")
        # Original complete G6 is rerun; no R5 gate/status is modified or re-executed.
        from scripts.verify_stage import verify as verify_g6
        began = time.monotonic()
        entry = {"id": "full-g6", "implementation": "scripts.verify_stage.verify", "stage": 6, "started": now()}
        record["commands"].append(entry)
        write_json(summary, record)
        g6, g6_path = verify_g6(6, original_g6_baseline())
        entry.update(exit_code=0 if g6.get("status") == "passed" else 1, duration_seconds=round(time.monotonic() - began, 3), verification=str(g6_path))
        record["g6"] = bound(g6_path)
        write_json(summary, record)
        total, ids = verify_final_g6(g6, g6_path, before, old_ids, old_ids)
        if not G7_TESTS <= ids:
            raise ValueError("New G7 report/profile/repeated-evaluation test identities are missing")
        record["offline_tests"] = {"total": total, "passed": total, "failed": 0, "skipped": 0, "R5_identities_preserved": True, "identities": sorted(ids), "g7_required": sorted(G7_TESTS)}
        record["model_calls"] = g6["model_calls"]
        record["legacy_reference"] = {"comparable_to_shop_ablation": False, "real_s0_model_ablation": False,
            "scope": "Current fixed legacy normal/deliberate-failure compatibility; not a cross-contract improvement claim", "scenarios": g6["scenarios"]}
        run_command(record, summary, "production-execution-graph", ["scripts/verify_execution_graph.py", "--output", out / "execution-graph"])
        run_command(record, summary, "native-graph-resources", ["scripts/verify_graph_resources.py", "--output", out / "graph-resources"])
        run_command(record, summary, "production-agent-graph", ["scripts/verify_agent_graph.py", "--output", out / "agent-graph",
            "--agents-fixed", g6_path.parent / "agents-fixed", "--agents-real", g6_path.parent / "agents-real",
            "--repairs-fixed", g6_path.parent / "repairs-fixed", "--repairs-real", g6_path.parent / "repairs-real"])
        from scripts.verify_execution_graph import CASES as CHILD_CASES, INPUT_VERSION as CHILD_VERSION
        from scripts.verify_graph_resources import CASES as RESOURCE_CASES, INPUT_VERSION as RESOURCE_VERSION
        from scripts.verify_agent_graph import CASES as PARENT_CASES, INPUT_VERSION as PARENT_VERSION
        for key, folder, filename, cases, version in (
            ("execution_graph", "execution-graph", "execution-graph-validation.json", CHILD_CASES, CHILD_VERSION),
            ("graph_resources", "graph-resources", "graph-resource-validation.json", RESOURCE_CASES, RESOURCE_VERSION),
            ("agent_graph", "agent-graph", "agent-graph-validation.json", PARENT_CASES, PARENT_VERSION)):
            path = out / folder / filename
            data = load(path)
            exact_scenarios(data, set(cases))
            if data.get("input_version") != version or data.get("full_stage_specialty") is not True:
                raise ValueError("G7 graph input scope/version mismatch")
            if key == "execution_graph" and (data.get("model_calls") != 0 or not all(x.get("architecture_verified") is True and x.get("requirements_preserved") is True for x in data["scenarios"])):
                raise ValueError("Execution graph architecture was not verified")
            if key == "graph_resources" and (data.get("native_human_resume_verified") is not True or data.get("plaintext_resource_leaks") != 0 or not all(x.get("native_graph_verified") is True and x.get("privacy_verified") is True for x in data["scenarios"])):
                raise ValueError("Native graph resource/privacy verification is incomplete")
            if key == "agent_graph" and (data.get("parent_child_native_verified") is not True or data.get("separated_repair_roles_verified") is not True or data.get("plaintext_resource_leaks") != 0):
                raise ValueError("Parent/child roles were not verified")
            record[key] = bound(path)
        run_command(record, summary, "repeated-comparison", ["scripts/verify_comparison.py", "--output", out / "comparison"], timeout=7200)
        comparison_path = out / "comparison/comparison-validation.json"
        comparison = load(comparison_path)
        from scripts.verify_comparison import INPUT_VERSION, REPEATS, aggregate, digest
        spec = load(out / "comparison/comparison-inputs.json")
        measured = aggregate(comparison["runs"], spec)
        if comparison.get("status") != "passed" or comparison.get("real_model") is not True or comparison.get("input_version") != INPUT_VERSION or comparison.get("repeats") != REPEATS or comparison.get("specification_sha256") != digest(spec) or measured["status"] != "passed" or not measured["all_samples_retained"]:
            raise ValueError("Repeated real-model comparison is incomplete or selected successful runs")
        if comparison.get("legacy_comparable") is not False or comparison.get("plaintext_resource_leaks") != 0 or comparison.get("model_payload_guarding_verified") is not True or comparison.get("model_calls") != measured["model_calls"] or measured["model_calls"] > spec["maximum_physical_model_calls"]:
            raise ValueError("Comparison privacy or comparability claims are invalid")
        record["comparison"] = bound(comparison_path)
        record["model_calls"] += measured["model_calls"]
        code = run_command(record, summary, "public-site", ["scripts/verify_public_site.py", "--output", out / "public"], timeout=1200, allowed=(0, 2))
        public_path = out / "public/public-site-validation.json"
        public = load(public_path)
        if public.get("input_version") != 1 or public.get("schema_version") != 1 or public.get("local_status") != "passed" or public.get("public_attempted") is not True or public.get("real_model") is not True:
            raise ValueError("Public-site local protocol or actual attempt is missing")
        if not public.get("local_role_calls") or not all(call.get("real") is False for call in public["local_role_calls"]) or public.get("local_privacy", {}).get("status") != "passed":
            raise ValueError("Local protocol mirror is missing or claims real-model capability")
        if public.get("source_files") != before or public.get("source_files_after") != before or public.get("source_changes_during_verification") != [] or public.get("protected_files") != record["protected_files"] or public.get("protected_files_after") != record["protected_files"]:
            raise ValueError("Public-site source/configuration boundary differs from this G7")
        if public.get("model_calls") != len(public.get("model_attempts", [])):
            raise ValueError("Public-site model totals differ from actual service audit")
        for field in ("local_cases", "public_cases") if code == 0 else ("local_cases",):
            rows = public.get(field, [])
            if len(rows) != 2 or {r["id"] for r in rows} != {"valid-login", "invalid-password"} or not all(r.get("status") == "passed" and r.get("native_graph_verified") is True for r in rows):
                raise ValueError("Public-site exact protocol scenario evidence is incomplete")
        blocked = code == 2 and public.get("status") == "blocked_public" and public.get("public_status") == "blocked" and public.get("compatibility_verified") is False
        if not blocked and not (code == 0 and public.get("status") == "passed" and public.get("public_status") == "passed" and public.get("compatibility_verified") is True):
            raise ValueError("Public site did not establish real compatibility or an honest external block")
        if not blocked and (not public.get("model_calls") or public.get("public_privacy", {}).get("status") != "passed"):
            raise ValueError("Actual public-site model/privacy verification is incomplete")
        record["public"] = bound(public_path)
        calls = public.get("model_calls", [])
        record["model_calls"] += len(calls) if isinstance(calls, list) else calls
        run_command(record, summary, "delivery-reports", ["scripts/verify_delivery_reports.py", "--g6", g6_path.parent,
            "--comparison", out / "comparison", "--public", out / "public", "--output", out / "reports"])
        report_path = out / "reports/delivery-reports-validation.json"
        validate_reports(report_path, [g6_path.parent, out / "comparison", out / "public", out / "reports"])
        record["reports"] = bound(report_path)
        integrity(record)
        files = [out / "source.zip"]
        for directory in (g6_path.parent, *(out / p for p in ("execution-graph", "graph-resources", "agent-graph", "comparison", "public", "reports"))):
            files.extend(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        # Original fixed smoke runs live in runtime/<run-id>, outside the G6
        # wrapper directory. Bind their actual artifacts without claiming the
        # new structured-run privacy policy applies to legacy raw DOM/traces.
        for case in g6["scenarios"]:
            legacy_dir = evidence_path(case["result_path"]).parent
            if not legacy_dir.is_relative_to((ROOT / "runtime").resolve()):
                raise ValueError("Legacy compatibility artifacts escaped the project runtime")
            files.extend(p for p in legacy_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        files.extend(Path(c["log"]) for c in record["commands"] if "log" in c)
        record["automatic_evidence_manifest"] = {str(p.resolve()): fingerprint(p) for p in sorted(set(files))}
        record["automatic_evidence_manifest_sha256"] = manifest_digest(record["automatic_evidence_manifest"])
        if blocked:
            record.update(status="blocked", automatic_status="blocked_public", unresolved=["Selected public practice site was externally blocked; compatibility and G7 are not passed"])
        else:
            manual_boundary(record, summary, manual_path, wait_for_manual)
    except Exception as error:
        record.update(status="failed", unresolved=[redactor()(f"{type(error).__name__}: {error}")])
    except BaseException:
        record.update(status="cancelled", unresolved=["G7 interrupted before acceptance"])
        raise
    finally:
        finish(record, summary)
    return record, summary


def resume(summary, manual_path=None, wait_for_manual=0):
    summary = evidence_path(summary)
    if not summary.is_relative_to((ROOT / "runtime/development/stage-7").resolve()):
        raise ValueError("Only a generated G7 record can resume")
    record = load(summary)
    if record.get("stage") != 7 or record.get("status") != "waiting_user" or record.get("automatic_status") != "passed":
        raise ValueError("Only an automatic-passed G7 waiting for fresh human acceptance may resume")
    try:
        if not 0 <= wait_for_manual <= 3600:
            raise ValueError("Manual wait must be 0..3600 seconds")
        integrity(record)
        automatic_evidence(record)
        record.setdefault("continuations", []).append({"started": now()})
        manual_boundary(record, summary, manual_path or (evidence_path(record["manual_path"]) if record.get("manual_path") else None), wait_for_manual)
    except Exception as error:
        record.update(status="failed", unresolved=[redactor()(f"{type(error).__name__}: {error}")])
    except BaseException:
        record.update(status="cancelled", unresolved=["G7 interrupted at the human boundary"])
        raise
    finally:
        finish(record, summary)
    return record, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--manual-evidence", type=Path)
    parser.add_argument("--wait-for-manual", type=int, default=0)
    parser.add_argument("--resume-final", "--resume", dest="resume", type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_for_manual <= 3600:
        parser.error("Manual wait must be 0..3600 seconds")
    if args.resume:
        record, _ = resume(args.resume, args.manual_evidence.resolve() if args.manual_evidence else None, args.wait_for_manual)
    else:
        candidates = sorted((ROOT / "runtime/development/stage-7").glob("*-baseline-*/baseline.json"))
        if not args.baseline and not candidates:
            parser.error("Capture G7 preimplementation baseline first")
        record, _ = verify((args.baseline or candidates[-1]).resolve(), args.previous,
                           args.manual_evidence.resolve() if args.manual_evidence else None, args.wait_for_manual)
    return 0 if record["status"] == "passed" else 2 if record["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
