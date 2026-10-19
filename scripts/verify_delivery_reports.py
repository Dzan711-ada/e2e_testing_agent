"""Validate delivery reports from explicit current-run records; no browser/model calls."""
import argparse
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings
from e2e_agent.resources import Redactor
from e2e_agent.suite_reporting import STATUS, is_generated_report, write_suite_report

INPUT_VERSION = 1
REQUIRED_SECTIONS = ("summary", "scenarios", "coverage", "human", "diagnosis", "repairs", "replay", "artifacts")
PRIVATE_SENTINELS = ("15500001111", "15500000001", "SentinelAccount_8de50", "Sentinel-P@ss-3f67!",
                     "BatchAccountSentinelQ87", "BatchPasswordSentinelQ87!", "BatchWrongSentinelQ87!")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def child(root, relative):
    path = (root / relative).resolve()
    need(path.is_relative_to(root.resolve()), "An input path escaped its explicit root")
    return path


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.attributes = set(), []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attributes.extend(attrs)


def check_escaped_untrusted_text():
    """A separate rendering safety probe, never a business run or model result."""
    with tempfile.TemporaryDirectory(prefix="delivery-report-escape-") as folder:
        directory = Path(folder)
        payload = '<script>alert(1)</script><svg onload="alert(2)">[open](javascript:alert(3))'
        paths = write_suite_report(directory, {"status": "blocked", "query": payload})
        html_path = Path(paths["html"])
        rendered = html_path.read_text(encoding="utf-8")
        parsed = Markup(rendered)
        need(not ({"script", "svg", "iframe", "object", "embed", "img"} & parsed.tags), "Untrusted text became active markup")
        need(not any(name.startswith("on") for name, _ in parsed.attributes), "An event handler entered the report")
        need("&lt;script&gt;" in rendered and is_generated_report(html_path), "Escaped report did not validate")
        html_path.write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
        need(not is_generated_report(html_path), "Renamed raw HTML was accepted as a program report")
    return True


def report_files(directory):
    return [directory / "report.md", directory / "report.html", directory / "report-input.json"]


def assert_program_report(directory, expected_status, *, actual_human=False):
    files = report_files(directory)
    need(all(path.is_file() for path in files), "Unified report artifacts are missing")
    need(is_generated_report(directory / "report.html"), "HTML is not the exact escaped program report")
    view = load(directory / "report-input.json")
    sections = {section["id"]: section for section in view["sections"]}
    need(set(REQUIRED_SECTIONS) <= set(sections), "Required unified report sections are missing")
    summary = dict(sections["summary"]["rows"])
    need(summary.get("状态") == STATUS.get(expected_status, expected_status), "Report overwrote or mismatched the recorded business status")
    paragraphs = " ".join(sections["human"]["paragraphs"])
    if actual_human:
        need("提供了真实人工控制台证据" in paragraphs, "Actual console evidence was not shown")
    else:
        need("提供了真实人工控制台证据" not in paragraphs, "Automatic resources were mislabeled as actual human participation")
    return files


def register_source(path, tracked):
    need(path.is_file(), "A required current-run source record is missing")
    before = sha(path)
    record = load(path)
    tracked[path] = before
    return record


def parent_report(directory, allowed_root, checked, tracked, scan_files, *, label, expected_status=None):
    directory = directory.resolve()
    need(directory.is_relative_to(allowed_root.resolve()), "Parent report escaped its selected verification root")
    source = directory / "agent-result.json"
    data = register_source(source, tracked)
    status = data.get("status")
    need(status in {"passed", "failed", "error", "blocked", "cancelled", "waiting_user"}, "A parent result has no explicit business status")
    if expected_status is not None:
        need(status == expected_status, "A manifest business status differs from its actual JSON record")
    files = assert_program_report(directory, status)
    summary = dict(next(section for section in load(directory / "report-input.json")["sections"]
                        if section["id"] == "summary")["rows"])
    execution = data.get("execution") or {}
    for label_name, original in (("父 run", data.get("parent_run_id")),
                                 ("首次 run", execution.get("run_id", data.get("run_id"))),
                                 ("需求指纹", execution.get("requirements_fingerprint", data.get("requirements_fingerprint")))):
        if original is not None:
            need(summary.get(label_name) == str(original), "A parent report is bound to another run or requirement version")
    for path in files:
        tracked[path] = sha(path)
    scan_files.update([source, *files])
    checked.append({"id": label, "run_dir": str(directory), "json_path": str(source),
                    "business_status": status, "json_sha256": sha(source),
                    "markdown_sha256": sha(files[0]), "html_sha256": sha(files[1]),
                    "input_sha256": sha(files[2]), "unaltered": True, "original_report_unchanged": True})


def safe_sidecar(path, tracked, scan_files, default=None):
    if not path.is_file():
        return default
    value = register_source(path, tracked)
    scan_files.add(path)
    return value


def copy_screenshots(record, source_root, destination, prefix):
    """Copy only existing masked PNG references under the explicit execution root."""
    copied = []
    index = 0
    for scene in record.get("scenarios", {}).values():
        for checkpoint in scene.get("checkpoints", []):
            value = checkpoint.get("screenshot")
            if not isinstance(value, str) or not value:
                continue
            candidate = Path(value)
            candidate = (candidate if candidate.is_absolute() else source_root / candidate).resolve()
            if not candidate.is_relative_to(source_root.resolve()) or candidate.suffix.lower() != ".png" or not candidate.is_file():
                checkpoint.pop("screenshot", None)
                continue
            index += 1
            target = destination / "evidence" / (prefix + "-" + str(index) + ".png")
            target.parent.mkdir(parents=True, exist_ok=True)
            before = sha(candidate)
            target.write_bytes(candidate.read_bytes())
            need(sha(target) == before == sha(candidate), "Screenshot copy changed the source artifact")
            checkpoint["screenshot"] = target.relative_to(destination).as_posix()
            copied.append({"source": str(candidate), "path": str(target), "sha256": before})
    return copied


def display_projection(execution, *, replay=None):
    suite = execution.get("sanitized_plan")
    need(isinstance(suite, dict), "Coordinator display requires sanitized_plan, never a canonical plan file")
    return {"status": execution["status"], "phase": "complete", "suite": deepcopy(suite),
            "query": suite.get("query"), "version": suite.get("version"), "execution": execution,
            "checkpoint_suite": "display_only", "complete_script": False,
            "repaired": execution.get("repaired", False), "repair_count": execution.get("repair_count", 0),
            "stability": execution.get("stability", "unverified"),
            **({"replay": replay} if replay is not None else {})}


def coordinator_copy(source, output, checked, tracked, scan_files, *, label, expected_status,
                     replay_source=None, manual=None):
    execution = register_source(source, tracked)
    need(execution.get("status") == expected_status, "Coordinator source status differs from its unchanged expectation")
    output.mkdir(parents=True, exist_ok=False)
    shown = deepcopy(execution)
    copied = copy_screenshots(shown, source.parent, output, "first")
    options = {}
    if replay_source is not None:
        replay = register_source(replay_source, tracked)
        need(replay.get("run_id") != execution.get("run_id"), "Independent report needs a fresh replay run")
        need(replay.get("requirements_fingerprint") == execution.get("requirements_fingerprint"),
             "Independent report cannot mix requirement versions")
        shown_replay = deepcopy(replay)
        copied += copy_screenshots(shown_replay, replay_source.parent, output, "replay")
        options["safe_replay_execution"] = shown_replay
        scan_files.add(replay_source)
    events = safe_sidecar(source.parent / "resource-events.json", tracked, scan_files, [])
    options["safe_resource_events"] = events
    if manual is not None:
        options["safe_manual_evidence"] = manual
        options["safe_replay_resource_events"] = safe_sidecar(
            replay_source.parent.parent / "replay-resource-events.json", tracked, scan_files, [])
    state = display_projection(shown, replay=manual.get("replay") if manual is not None else None)
    if manual is not None:
        state["complete_script"] = True
    paths = write_suite_report(output, state, **options)
    files = assert_program_report(output, expected_status, actual_human=manual is not None)
    scan_files.update([source, *files])
    checked.append({"id": label, "run_dir": str(output), "json_path": str(source),
                    "business_status": execution["status"], "json_sha256": sha(source),
                    "markdown_sha256": sha(Path(paths["markdown"])), "html_sha256": sha(Path(paths["html"])),
                    "input_sha256": sha(Path(paths["input"])), "unaltered": True,
                    "projection_only": True, "copied_screenshots": copied})


def verify_automatic(args, checked, tracked, scan_files):
    g6, comparison, public = (path.resolve() for path in (args.g6, args.comparison, args.public))
    groups = (
        ("fixed-clear", "agents-fixed/clear-goal"),
        ("fixed-clarification", "agents-fixed/required-clarification"),
        ("real-clear", "agents-real/clear-goal"),
        ("real-clarification", "agents-real/required-clarification"),
        ("repaired-parent", "repairs-fixed/graph-repaired-export/run"),
        ("sms-parent", "sms-model/run"),
    )
    for label, relative in groups:
        parent_report(child(g6, relative), g6, checked, tracked, scan_files, label=label)
    comparison_path = comparison / "comparison-validation.json"
    manifest = register_source(comparison_path, tracked)
    scan_files.add(comparison_path)
    need(manifest.get("schema_version") == 1 and manifest.get("input_version") == 1, "Comparison report protocol changed")
    runs = manifest.get("runs", [])
    need(len(runs) == 18, "Every one of the 18 comparison runs must have a delivery report")
    seen = set()
    for index, run in enumerate(runs, 1):
        directory = Path(run["run_dir"]).resolve()
        need(directory not in seen, "Comparison report runs are duplicated")
        seen.add(directory)
        parent_report(directory, comparison, checked, tracked, scan_files, label="comparison-" + str(index),
                      expected_status=run["business_status"])
    public_path = public / "public-site-validation.json"
    public_manifest = register_source(public_path, tracked)
    scan_files.add(public_path)
    local_rows, public_rows = public_manifest.get("local_cases", []), public_manifest.get("public_cases", [])
    need(public_manifest.get("schema_version") == 1 and public_manifest.get("input_version") == 1,
         "Public/local report collection is missing")
    expected_cases = {"valid-login", "invalid-password"}
    need(len(local_rows) == 2 and {row["id"] for row in local_rows} == expected_cases
         and all(row.get("status") == "passed" and row.get("native_graph_verified") is True for row in local_rows),
         "Both actual local protocol cases must pass before reporting public compatibility")
    blocked = (public_manifest.get("status") == "blocked_public" and public_manifest.get("public_status") == "blocked"
               and public_manifest.get("compatibility_verified") is False)
    passed = (public_manifest.get("status") == "passed" and public_manifest.get("public_status") == "passed"
              and public_manifest.get("compatibility_verified") is True)
    need(blocked or passed, "Public reporting requires an explicit passed or externally blocked outcome")
    need(len(public_rows) == 2 and {row["id"] for row in public_rows} == expected_cases,
         "The exact two public protocol case records are required")
    if passed:
        need(all(row.get("status") == "passed" and row.get("native_graph_verified") is True for row in public_rows),
             "Public compatibility requires both actual native protocol cases")
    seen, local_seen, public_seen, unstarted = set(), set(), set(), []
    for row in local_rows:
        directory = Path(row["run_dir"]).resolve()
        if directory not in seen:
            seen.add(directory)
            parent_report(directory, public, checked, tracked, scan_files, label="public-parent-" + str(len(seen)))
        local_seen.add(directory)
    for row in public_rows:
        directory = Path(row["run_dir"]).resolve()
        need(directory.is_relative_to(public), "A public protocol record escaped its explicit verification root")
        if not (directory / "agent-result.json").is_file():
            connectivity = public_manifest.get("public_connectivity", {})
            need(blocked and connectivity.get("status") == "blocked"
                 and connectivity.get("http_response_received") is False and row.get("agent_result") is None
                 and row.get("status") == "blocked" and row.get("native_graph_verified") is False,
                 "An actual public run result is missing without preflight-only connection blocking")
            unstarted.append({"id": row["id"], "run_dir": str(directory), "status": "blocked",
                              "reason": "Public connection preflight blocked before any parent graph started"})
            continue
        need(directory not in local_seen, "Public and local reports must use separate actual parent runs")
        if directory not in seen:
            seen.add(directory)
            parent_report(directory, public, checked, tracked, scan_files, label="public-parent-" + str(len(seen)))
        public_seen.add(directory)
    need(local_seen and (public_seen or (blocked and len(unstarted) == 2)),
         "Local and started public runs must be represented without inventing an unstarted parent graph")
    for label, relative, expected in (
        ("coordinator-failed", "suites/login-failure/suite-result.json", "failed"),
        ("coordinator-blocked", "resources/missing-code/suite-result.json", "blocked"),
        ("coordinator-cancelled", "resources/cancelled-phone/suite-result.json", "cancelled"),
    ):
        coordinator_copy(child(g6, relative), args.output / "cases" / label, checked, tracked, scan_files,
                         label=label, expected_status=expected)
    return {"mode": "automatic", "actual_console_input": False, "comparison_runs": len(runs),
            "public_case_rows": len(local_rows) + len(public_rows), "public_parent_reports": len(public_seen),
            "local_parent_reports": len(local_seen), "public_preflight_blocked": bool(unstarted),
            "public_unstarted_cases": unstarted,
            "public_connectivity": public_manifest.get("public_connectivity", {})}


def verify_manual_only(args, checked, tracked, scan_files):
    # Deliberately no automatic manifest/verification record access in this mode.
    from scripts.verify_stage import verify_manual
    source = args.manual_evidence.resolve()
    manual = register_source(source, tracked)
    need(manual.get("stage") == 4 and manual.get("status") == "passed"
         and manual.get("actual_console_input") is True and manual.get("provider") == "ConsoleProvider"
         and manual.get("headless") is False and manual.get("input_channel") == "parent_console_stdin",
         "Manual report requires passed actual headed parent-console evidence")
    binding = verify_manual(source)
    need(binding.get("actual_console_input") is True, "A real passed console record is mandatory")
    first_path = child(source.parent, "generation/suite-result.json")
    second_path = child(source.parent, "replay-suite/suite-result.json")
    first, second = load(first_path), load(second_path)
    need(first == manual.get("first"), "Manual first record differs from its actual source artifact")
    need(first.get("status") == second.get("status") == "passed", "Both actual manual rounds must pass")
    expected_runs = {row["run_id"] for row in manual["rounds"]}
    need(expected_runs == {first.get("run_id"), second.get("run_id")} and len(expected_runs) == 2,
         "Manual report rounds do not match the two actual runs")
    coordinator_copy(first_path, args.output / "console-first-replay", checked, tracked, scan_files,
                     label="actual-console-first-replay", expected_status="passed", replay_source=second_path, manual=manual)
    scan_files.add(source)
    return {"mode": "manual-only", "actual_console_input": True,
            "manual_evidence": {"path": str(source), "sha256": sha(source)},
            "manual_rounds": manual["rounds"], "automatic_manifest_read_or_updated": False}


def scan_selected_files(files, redactor):
    checked = 0
    for path in sorted(files):
        if path.suffix.lower() == ".html":
            need(is_generated_report(path), "Unverified HTML cannot bypass the raw evidence rule")
        need(path.suffix.lower() not in {".zip", ".har"}, "Raw trace/network evidence cannot be a report artifact")
        text = path.read_bytes().decode("utf-8", errors="replace")
        need(redactor.text(text) == text, "A private sentinel or configured API key leaked in a selected report artifact")
        checked += 1
    return checked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g6", type=Path)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--public", type=Path)
    parser.add_argument("--manual-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.manual_evidence:
        if any((args.g6, args.comparison, args.public)):
            parser.error("Manual-only mode cannot read automatic collections")
    elif not all((args.g6, args.comparison, args.public)):
        parser.error("Automatic mode requires --g6, --comparison and --public")
    args.output = args.output.resolve()
    selected_roots = [path.resolve() for path in (args.g6, args.comparison, args.public) if path]
    if args.manual_evidence:
        selected_roots.append(args.manual_evidence.resolve().parent)
    need(not any(args.output == root or root.is_relative_to(args.output) for root in selected_roots),
         "Report output must not contain an original input collection")
    args.output.mkdir(parents=True, exist_ok=False)
    settings, redactor = Settings.load(), Redactor()
    for value in (*PRIVATE_SENTINELS, settings.deepseek_api_key, settings.qwen_api_key):
        if value:
            redactor.register(value)
    report = {"schema_version": 1, "input_version": INPUT_VERSION, "status": "running", "checked_reports": [],
              "required_sections": list(REQUIRED_SECTIONS), "plaintext_resource_leaks": None,
              "escaped_untrusted_html": False, "new_browser_model_calls": 0}
    tracked, scan_files = {}, set()
    try:
        report.update(verify_manual_only(args, report["checked_reports"], tracked, scan_files) if args.manual_evidence else
                      verify_automatic(args, report["checked_reports"], tracked, scan_files))
        report["escaped_untrusted_html"] = check_escaped_untrusted_text()
        report["privacy_checked_files"] = scan_selected_files(scan_files, redactor)
        for path, before in tracked.items():
            need(sha(path) == before, "An original result or historical report changed during report validation")
        report.update(status="passed", plaintext_resource_leaks=0, original_sources_and_reports_unchanged=True,
                      privacy_scope="Selected safe result/sidecar records and program report artifacts. Full run privacy remains in the G6/public/comparison verifiers.")
    except Exception as error:
        report.update(status="failed", error=redactor.text(type(error).__name__ + ": " + str(error)))
    summary = args.output / "delivery-reports-validation.json"
    summary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checked_reports": len(report["checked_reports"]),
                      "summary": str(summary)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
