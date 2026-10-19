"""Portable projections of real R4 records; no browser, model or runtime history dependency."""
import ast
from copy import deepcopy
from html.parser import HTMLParser
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

from e2e_agent import suite_reporting
from e2e_agent.suite_reporting import is_generated_report, render_suite_report, write_suite_report


def sample():
    """Keep the existing agent-result/suite-result shapes, not a new report schema.

    Projected from R4-20261017-180012-283da8f6 replay-only-failure and repaired
    export records; values/paths are portable fictional examples.
    """
    conditions = [
        {"rule_id": "login", "target": "testid:identity", "operator": "equals", "expected": "Signed in: [RESOURCE]"},
        {"rule_id": "cart", "target": "testid:quantity", "operator": "equals", "expected": "1"},
    ]
    suite = {"schema_version": 2, "id": "shop-suite", "version": 2, "query": "测试登录和购物车数量为1",
        "rules": [{"id": identity, "statement": statement, "required": True,
                   "source": {"kind": "contract", "reference": "contract:shop-v1"}}
                  for identity, statement in (("login", "正确凭据登录"), ("cart", "加入一本书数量为1"))],
        "scenarios": [{"id": "main-chain", "description": "同一会话登录后加购", "depends_on": [],
            "checkpoints": [{"id": "login", "depends_on": [], "acceptance": [conditions[0]]},
                            {"id": "cart", "depends_on": ["login"], "acceptance": [conditions[1]]}],
            "actions": [{"id": "submit-login", "checkpoint_id": "login", "kind": "click", "target": "testid:login-submit"}]}]}
    checks = [{"id": identity, "status": "passed", "evidence": [
        {"id": "main-chain:" + identity + ":0", **condition, "actual": condition["expected"], "status": "passed",
         "condition_fingerprint": identity + "-condition-fingerprint"}]}
        for identity, condition in zip(("login", "cart"), conditions)]
    first = {"schema_version": 2, "run_id": "generation-unique", "suite_id": "shop-suite",
        "requirements_fingerprint": "frozen-fingerprint", "status": "passed", "initial_status": "error",
        "action_count": 10, "repaired": True, "repair_count": 1, "sanitized_plan": suite,
        "source_catalog": {"contract:shop-v1": {"kind": "contract", "text": "明确约定"}},
        "timing": {"active_seconds": 4.2, "human_wait_seconds": 0},
        "scenarios": {"main-chain": {"id": "main-chain", "status": "passed", "context_id": "context-first",
                                    "checkpoints": checks, "events": []}},
        "initial_failures": [{"scenario_id": "main-chain", "checkpoint_id": "login",
            "action": {"id": "submit-login", "target": "testid:login-old"}, "status": "error", "error": "Target absent before dispatch"}],
        "repair_history": [{"scenario_id": "main-chain", "checkpoint_id": "login", "action_id": "submit-login",
            "attempt": 1, "decision": "approved", "execution_status": "passed",
            "assessment": {"diagnosis": {"attribution": "agent_execution"}},
            "proposed_plan": {"proposed_actions": [{"id": "submit-login", "kind": "click", "target": "testid:login-submit"}]}}]}
    second = deepcopy(first)
    second.update(run_id="replay-unique", status="failed", initial_status="failed", repaired=False, repair_count=0)
    second["scenarios"]["main-chain"].update(status="failed", context_id="context-second")
    second["scenarios"]["main-chain"]["checkpoints"][0].update(status="failed", error="Recorded login assertion failed")
    second["scenarios"]["main-chain"]["checkpoints"][0]["evidence"][0].update(status="failed", actual="No authenticated identity")
    second["scenarios"]["main-chain"]["checkpoints"][1] = {"id": "cart", "status": "blocked", "blocked_by": "login", "evidence": []}
    result = {"query": suite["query"], "version": 2, "status": "error", "phase": "complete",
        "parent_run_id": "parent-unique", "suite": suite, "execution": first, "complete_script": True,
        "checkpoint_suite": "display_only", "answers": {"target_quantity": "1"}, "repaired": True,
        "repair_count": 1, "stability": "inconsistent", "model_calls": [{"role": "diagnose"}, {"role": "repair"}],
        "replay": {"status": "failed", "tests": 3, "failures": 1, "errors": 0, "skipped": 0, "exit_code": 1},
        "diagnostic_origin": "replay", "diagnostic_run_id": "replay-unique",
        "diagnostics": {"status": "completed", "origin": "replay", "execution_run_id": "replay-unique",
            "assessment": {"diagnosis": {"attribution": "agent_execution", "evidence_ids": ["ev-0002"],
                "counter_evidence_ids": ["ev-0003"], "explanation": "依据当次重跑证据调查操作", "uncertainties": ["尚未证明内部根因"]},
                "investigations": ["检查当前控件与操作事件"], "repair_recommendations": ["定位修复需经过程序批准"]}}}
    return result, second


class InspectHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.hrefs, self.tables = [], [], {}
        self.title = ""
        self.heading = self.cell = self.row = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
        if tag == "h2":
            self.heading = []
        if tag == "tr":
            self.row = []
        if tag in {"th", "td"}:
            self.cell = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.title = "".join(self.heading)
            self.heading = None
            self.tables[self.title] = []
        if tag in {"th", "td"}:
            self.row.append("".join(self.cell))
            self.cell = None
        if tag == "tr":
            self.tables[self.title].append(self.row)
            self.row = None


class SuiteReportingTests(unittest.TestCase):
    def test_actual_record_shapes_preserve_first_failure_repair_replay_difference_and_diagnosis(self):
        result, second = sample()
        before, second_before = deepcopy(result), deepcopy(second)
        markdown, document = render_suite_report(result, safe_replay_execution=second)
        parsed = InspectHTML(document)
        self.assertEqual(result, before)
        self.assertEqual(second, second_before)
        summary = dict(parsed.tables["套件总览"][1:])
        self.assertEqual(summary["状态"], "执行错误")
        self.assertEqual(summary["原始执行状态"], "执行错误")
        self.assertEqual(summary["首次最终状态"], "通过")
        self.assertEqual(summary["独立 Pytest"], "断言失败")
        self.assertEqual(summary["稳定性（原记录）"], "inconsistent")
        checkpoints = parsed.tables["检查点与首次/重跑差异"]
        self.assertEqual(checkpoints[1][3:6], ["通过", "断言失败", "不同"])
        self.assertEqual(checkpoints[2][3:6], ["通过", "被阻塞", "不同"])
        self.assertIn("testid:login-old", document)
        self.assertIn("approved", document)
        self.assertIn("ev-0002", document)
        self.assertIn("尚未证明内部根因", document)
        self.assertEqual(dict(parsed.tables["证据诊断"][1:])["诊断 run"], "replay-unique")
        self.assertIn("原始失败保留", markdown)

    def test_failed_assertion_is_executed_coverage_and_blocked_check_is_missing(self):
        result, _ = sample()
        first = result["execution"]
        first["status"] = result["status"] = "failed"
        first["scenarios"]["main-chain"]["checkpoints"][0]["evidence"][0]["status"] = "failed"
        first["scenarios"]["main-chain"]["checkpoints"][1] = {"id": "cart", "status": "blocked", "blocked_by": "login", "evidence": []}
        _, document = render_suite_report(result)
        rows = InspectHTML(document).tables["需求覆盖"]
        self.assertEqual(rows[1][5:9], ["1", "1", "0", "已执行，含未通过断言"])
        self.assertEqual(rows[2][5:9], ["1", "0", "0", "记录不足"])
        self.assertEqual(result["status"], "failed")

    def test_passed_record_without_matching_required_evidence_is_flagged_without_overwriting_status(self):
        for kind in ("different-value", "bool-versus-integer"):
            with self.subTest(kind=kind):
                result, _ = sample()
                result["status"] = "passed"
                item = result["execution"]["scenarios"]["main-chain"]["checkpoints"][0]["evidence"][0]
                if kind == "different-value":
                    item["expected"] = "Different frozen expectation"
                else:
                    result["suite"]["scenarios"][0]["checkpoints"][0]["acceptance"][0]["expected"] = True
                    item["expected"] = 1
                _, document = render_suite_report(result)
                self.assertIn("原记录标记通过，但必需断言记录不足或未通过", document)
                self.assertEqual(InspectHTML(document).tables["需求覆盖"][1][6:9], ["0", "0", "记录不足"])
                self.assertEqual(result["status"], "passed")

    def test_html_and_markdown_neutralize_untrusted_text_and_do_not_embed_unused_private_fields(self):
        result, _ = sample()
        attack = '<script>alert("x")</script><img src=x onerror=alert(1)>[open](javascript:alert(2))'
        result["query"] = attack
        result["suite"]["rules"][0]["statement"] = attack
        result["diagnostics"]["assessment"]["diagnosis"]["explanation"] = attack
        result["opaque_private_cache"] = {"password": "NeverEmbedPrivate9Z6"}
        result["execution"]["repair_history"][0]["proposed_plan"]["proposed_actions"][0]["value"] = "NeverEmbedPrivate9Z6"
        events = [{"id": "req", "ref": "phone", "status": "provided", "value": "NeverEmbedPrivate9Z6",
                   "delivery_hint": "NeverEmbedPrivate9Z6", "error": attack}]
        markdown, document = render_suite_report(result, safe_resource_events=events)
        parsed = InspectHTML(document)
        self.assertIn("&lt;script&gt;", document)
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("NeverEmbedPrivate9Z6", document + markdown)
        self.assertNotIn("<script", markdown.lower())
        self.assertIn("\\[open\\]", markdown)
        self.assertFalse({"script", "img", "iframe", "object", "embed", "form"} & {tag for tag, _ in parsed.tags})
        self.assertFalse(any(key.startswith("on") for _, attrs in parsed.tags for key in attrs))
        self.assertFalse(parsed.hrefs)
        self.assertTrue(any(attrs.get("http-equiv") == "Content-Security-Policy" for tag, attrs in parsed.tags if tag == "meta"))

    def test_waits_and_scripted_resources_do_not_claim_actual_human_acceptance(self):
        result, _ = sample()
        result["execution"]["timing"]["human_wait_seconds"] = 46.2
        result["pending_resource"] = {"logical_request_id": "logical", "ref": "sms_code", "kind": "sms_code",
            "scene_id": "main-chain", "step_id": "get-code", "status": "waiting_user", "challenge_id": "current-challenge"}
        events = [{"id": "req-phone", "ref": "phone", "kind": "phone", "status": "provided",
                   "scene_id": "main-chain", "step_id": "get-phone"}]
        _, document = render_suite_report(result, safe_resource_events=events)
        self.assertIn("未提供真实人工证据", document)
        self.assertIn("46.2", document)
        self.assertIn("current-challenge", document)
        self.assertNotIn("提供了真实人工控制台证据", document)
        manual = {"actual_console_input": True, "provider": "ConsoleProvider", "input_channel": "parent_console_stdin",
                  "headless": False, "status": "passed", "rounds": [{"id": "first", "run_id": "generation-unique"}]}
        _, assisted = render_suite_report(result, safe_manual_evidence=manual)
        self.assertIn("提供了真实人工控制台证据，状态：passed", assisted)

    def test_stale_or_foreign_replay_record_is_not_used_to_invent_checkpoint_results(self):
        for mismatch in ("same-run", "different-requirements"):
            with self.subTest(mismatch=mismatch):
                result, second = sample()
                if mismatch == "same-run":
                    second["run_id"] = result["execution"]["run_id"]
                else:
                    second["requirements_fingerprint"] = "other-version"
                second["scenarios"]["main-chain"]["checkpoints"][0]["error"] = "DiscardedForeignRecordCanary"
                _, document = render_suite_report(result, safe_replay_execution=second)
                self.assertNotIn("DiscardedForeignRecordCanary", document)
                self.assertIn("未用于逐项对照", document)
                self.assertEqual(InspectHTML(document).tables["检查点与首次/重跑差异"][1][4:6], ["未记录", "记录不足"])
                self.assertEqual(result["replay"]["status"], "failed")

    def test_cancelled_partial_plan_keeps_unexecuted_checkpoints_and_missing_replay_explicit(self):
        result, _ = sample()
        scene = {"id": "main-chain", "status": "blocked", "blocked_by": "resource",
                 "checkpoints": [{"id": "login", "status": "blocked", "blocked_by": "resource", "evidence": []}]}
        result.update(status="cancelled", complete_script=False, execution=None, scene_results={"main-chain": scene})
        result.pop("replay")
        result.pop("diagnostics")
        _, document = render_suite_report(result)
        parsed = InspectHTML(document)
        self.assertEqual(dict(parsed.tables["套件总览"][1:])["状态"], "已取消")
        self.assertEqual(dict(parsed.tables["套件总览"][1:])["完整脚本"], "false")
        self.assertEqual(parsed.tables["检查点与首次/重跑差异"][2][3:6], ["未记录", "未记录", "记录不足"])
        self.assertIn("resource", document)
        self.assertIn("未提供当次重跑结构化场景", document)

    def test_writer_only_changes_three_report_artifacts_and_links_only_local_existing_safe_artifacts(self):
        result, second = sample()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "run"
            directory.mkdir()
            outside = Path(folder) / "outside.png"
            outside.write_bytes(b"not read")
            screenshot = directory / "screen shot).png"
            screenshot.write_bytes(b"not read")
            canonical = directory / "plan.json"
            canonical.write_text('{"private":"DoNotReadCanonicalSecret77"}', encoding="utf-8")
            saved = directory / "agent-result.json"
            saved.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            result["execution"]["scenarios"]["main-chain"]["checkpoints"][0]["screenshot"] = str(screenshot)
            result["execution"]["scenarios"]["main-chain"]["checkpoints"][1]["screenshot"] = str(outside)
            paths = write_suite_report(directory, result, safe_replay_execution=second)
            document = Path(paths["html"]).read_text(encoding="utf-8")
            parsed = InspectHTML(document)
            self.assertIn("screen%20shot%29.png", parsed.hrefs)
            self.assertIn("plan.json", parsed.hrefs)
            self.assertFalse(any("outside" in href or ":" in href or href.startswith("/") for href in parsed.hrefs))
            self.assertNotIn("DoNotReadCanonicalSecret77", document)
            for name, payload in before.items():
                self.assertEqual((directory / name).read_bytes(), payload)
            self.assertEqual({path.name for path in directory.iterdir()}, set(before) | {"report.md", "report.html", "report-input.json"})
            self.assertTrue(is_generated_report(paths["html"]))
            self.assertNotIn("DoNotReadCanonicalSecret77", Path(paths["input"]).read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "output run directory"):
                write_suite_report(directory, result, artifact_root=Path(folder))

    def test_generated_html_requires_owned_input_exact_bytes_and_exact_filename(self):
        result, _ = sample()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            paths = write_suite_report(directory, result)
            document = Path(paths["html"])
            source = Path(paths["input"])
            original = document.read_bytes()
            self.assertTrue(is_generated_report(document))
            renamed = directory / "other.html"
            renamed.write_bytes(original)
            self.assertFalse(is_generated_report(renamed))
            document.write_bytes(original + b"<!-- changed -->")
            self.assertFalse(is_generated_report(document))
            document.write_text('<html><body><script>alert(1)</script>e2e-suite-report</body></html>', encoding="utf-8")
            self.assertFalse(is_generated_report(document))
            document.write_bytes(original)
            data = json.loads(source.read_text(encoding="utf-8"))
            data["format"] = "arbitrary-html-with-a-marker"
            source.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(is_generated_report(document))
            source.unlink()
            self.assertFalse(is_generated_report(document))

    def test_report_input_cannot_introduce_script_urls_or_raw_executable_markup(self):
        result, _ = sample()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            paths = write_suite_report(directory, result)
            source, document = Path(paths["input"]), Path(paths["html"])
            original = json.loads(source.read_text(encoding="utf-8"))
            for href in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                         "//attacker.example/payload", "../outside.png", "%2e%2e/outside.png"):
                with self.subTest(href=href):
                    changed = deepcopy(original)
                    changed["sections"][-1]["rows"] = [[{"kind": "local_link", "label": "Open", "href": href}]]
                    source.write_text(json.dumps(changed), encoding="utf-8")
                    self.assertFalse(is_generated_report(document))
            changed = deepcopy(original)
            changed["sections"][0]["rows"][0][1] = '<svg onload="alert(1)"><script>alert(2)</script>'
            markdown, escaped = suite_reporting._render_view(changed, directory)
            source.write_text(json.dumps(changed), encoding="utf-8")
            document.write_bytes(escaped.encode("utf-8"))
            self.assertTrue(is_generated_report(document))
            self.assertFalse({"svg", "script"} & {tag for tag, _ in InspectHTML(escaped).tags})
            self.assertNotIn("<script", markdown)

    def test_non_json_live_handles_are_rejected_and_module_has_only_stdlib_dependencies(self):
        result, _ = sample()
        result["browser"] = object()
        with self.assertRaises(TypeError):
            render_suite_report(result)
        tree = ast.parse(inspect.getsource(suite_reporting))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.append(node.module.split(".")[0])
        self.assertTrue(set(imports) <= sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
