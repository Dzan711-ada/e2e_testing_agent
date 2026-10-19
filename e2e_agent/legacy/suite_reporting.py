"""Static suite reports from existing sanitized records, using only stdlib.

Callers own privacy and current-run binding. This module never loads canonical
plans, invokes a model, changes a verdict or discovers arbitrary sidecars.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import html
import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

STATUS = {"pending": "待执行", "running": "执行中", "waiting_user": "等待输入",
          "passed": "通过", "failed": "断言失败", "error": "执行错误",
          "blocked": "被阻塞", "cancelled": "已取消"}
ATTRIBUTION = {"suspected_product": "疑似产品问题", "agent_execution": "Agent 操作问题",
               "environment": "环境问题", "requirements": "需求问题", "unknown": "未知"}
ARTIFACTS = ("agent-result.json", "plan.json", "partial-plan.json", "suite.json",
             "suite-manifest.json", "test_generated.py", "junit.xml", "pytest.log",
             "parent-events.json", "generation/execution-events.json",
             "generation/suite-result.json", "replay-suite/suite-result.json")
FORMAT = "e2e-suite-report"
SECTION_IDS = ("summary", "scenarios", "checkpoints", "assertions", "coverage", "human", "resources",
               "blocked", "initial_failures", "repairs", "diagnosis", "replay", "artifacts")
TITLES = ("套件总览", "业务链与会话隔离", "检查点与首次/重跑差异", "记录的确定性断言", "需求覆盖",
          "需求澄清与人工参与", "资源请求（仅引用和当次身份）", "阻塞、失败与未完成", "原始失败保留",
          "修复历史", "证据诊断", "独立 Pytest 汇总", "本地证据与脚本")


def _obj(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _status(value):
    return STATUS.get(value, _text(value) if value else "未记录")


def _snapshot(value):
    if not isinstance(value, Mapping):
        raise TypeError("A sanitized JSON object is required")
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


@dataclass(frozen=True)
class _Link:
    label: str
    href: str


def _link(value, root, label=None):
    if root is None or not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    if "://" in value or value.lower().startswith(("javascript:", "data:", "file:", "vbscript:")):
        return None
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".json", ".xml", ".log", ".py", ".md", ".txt"}:
        return None
    relative = path.relative_to(root).as_posix()
    return _Link(label or relative, quote(relative, safe="/._-"))


def _md(value):
    if isinstance(value, _Link):
        return "[" + _md(value.label) + "](" + value.href + ")"
    text = html.escape(_text(value), quote=False).replace("\r\n", "\n").replace("\r", "\n")
    for character in "\\" + chr(96) + "*_{}[]()#+!|":
        text = text.replace(character, "\\" + character)
    return text.replace("\n", " / ")


def _html(value):
    if isinstance(value, _Link):
        return '<a href="' + html.escape(value.href, quote=True) + '">' + html.escape(value.label) + "</a>"
    return html.escape(_text(value), quote=True)


def _checkpoints(scene):
    return {item.get("id"): item for item in _list(_obj(scene).get("checkpoints"))}


def _ids(planned, *records):
    result = [item["id"] for item in planned if "id" in item]
    for record in records:
        result.extend(identity for identity in record if identity not in result)
    return result


def _coverage(suite, scenes):
    counts, statuses = {}, {}
    for scene in _list(suite.get("scenarios")):
        actual = _checkpoints(scenes.get(scene.get("id")))
        for checkpoint in _list(scene.get("checkpoints")):
            evidence = _list(actual.get(checkpoint.get("id"), {}).get("evidence"))
            used = set()
            for condition in _list(checkpoint.get("acceptance")):
                identity = condition.get("rule_id")
                counts[identity] = counts.get(identity, 0) + 1
                match = next((i for i, item in enumerate(evidence) if i not in used
                    and type(item.get("expected")) is type(condition.get("expected")) and all(
                    item.get(key) == condition.get(key) for key in ("rule_id", "target", "operator", "expected"))), None)
                if match is not None:
                    used.add(match)
                statuses.setdefault(identity, []).append(None if match is None else evidence[match].get("status"))
    return counts, statuses


def _coverage_label(count, statuses):
    if not count:
        return "无计划断言"
    if sum(value is not None for value in statuses) < count:
        return "记录不足"
    return "记录断言均通过" if all(value == "passed" for value in statuses) else "已执行，含未通过断言"


def _replay_view(result, first, candidate):
    if candidate is None:
        return {}, "未提供当次重跑结构化场景；Pytest 汇总不能代替逐检查点证据。"
    record = _snapshot(candidate)
    expected = first.get("requirements_fingerprint") or result.get("requirements_fingerprint")
    if not record.get("run_id") or record.get("run_id") == first.get("run_id"):
        return {}, "重跑结构化记录没有独立 run ID，未用于逐项对照。"
    if expected and record.get("requirements_fingerprint") != expected:
        return {}, "重跑结构化记录需求指纹不匹配，未用于逐项对照。"
    return record, ""


def _build_view(safe_result, *, safe_replay_execution=None, safe_resource_events=(),
                        safe_replay_resource_events=(), safe_manual_evidence=None, artifact_root=None):
    """Select only displayed fields from sanitized presentation data.

    Do not pass canonical/raw run_agent_suite return values. No whole input JSON,
    resource value, delivery hint, browser storage or raw DOM is embedded.
    """
    result = _snapshot(safe_result)
    first = _obj(result.get("execution"))
    suite = _obj(first.get("sanitized_plan")) or _obj(result.get("suite"))
    first_scenes = _obj(first.get("scenarios")) if result.get("execution") is not None else _obj(result.get("scene_results"))
    summary = _obj(result.get("replay")) or _obj(result.get("replay_result"))
    second, replay_note = _replay_view(result, first, safe_replay_execution)
    second_scenes = _obj(second.get("scenarios"))
    resources = json.loads(json.dumps(list(safe_resource_events), ensure_ascii=False))
    replay_resources = json.loads(json.dumps(list(safe_replay_resource_events), ensure_ascii=False))
    manual = _snapshot(safe_manual_evidence) if safe_manual_evidence is not None else {}
    root = Path(artifact_root).resolve() if artifact_root is not None else None
    sections = []

    def add(title, headers, rows, paragraphs=()):
        sections.append((title, headers, list(rows), [paragraph for paragraph in paragraphs if paragraph]))

    add("套件总览", ("项目", "原记录"), [
        ("状态", _status(result.get("status"))), ("套件", suite.get("id", first.get("suite_id"))),
        ("需求", result.get("query", suite.get("query"))), ("版本", result.get("version", suite.get("version"))),
        ("父 run", result.get("parent_run_id")), ("首次 run", first.get("run_id", result.get("run_id"))),
        ("原始执行状态", _status(first.get("initial_status", first.get("status")))),
        ("首次最终状态", _status(first.get("status", result.get("execution_status")))),
        ("独立 Pytest", _status(summary.get("status"))), ("独立执行 run", second.get("run_id")),
        ("稳定性（原记录）", result.get("stability", first.get("stability", "unverified"))),
        ("完整脚本", result.get("complete_script", False)),
        ("发生修复", result.get("repaired", first.get("repaired", False))),
        ("修复次数", result.get("repair_count", first.get("repair_count", 0))),
        ("动作尝试数", first.get("action_count", result.get("action_count"))),
        ("模型调用记录数", len(_list(result.get("model_calls")))),
        ("需求指纹", first.get("requirements_fingerprint", result.get("requirements_fingerprint"))),
        ("阶段", result.get("phase")), ("错误/停止原因", result.get("error")),
    ], ["本报告只展示原记录，不改变执行状态、断言或模型结论。failed 不等于确认产品 Bug，unknown 是有效结论。",
        "首次和独立重跑是两轮业务执行；修复后均通过也不抹去首次失败。"])

    plans = _list(suite.get("scenarios"))
    by_id = {item.get("id"): item for item in plans}
    scene_rows, cp_rows, assertions, stopped = [], [], [], []
    for identity in _ids(plans, first_scenes, second_scenes):
        plan, a, b = by_id.get(identity, {}), _obj(first_scenes.get(identity)), _obj(second_scenes.get(identity))
        scene_rows.append((identity, plan.get("description"), ", ".join(plan.get("depends_on", [])) or "—",
            _status(a.get("status")), _status(b.get("status")), a.get("context_id"), b.get("context_id"),
            a.get("blocked_by") or a.get("error")))
        for label, scene in (("首次场景", a), ("重跑场景", b)):
            if scene.get("blocked_by") or scene.get("status") in {"blocked", "cancelled", "error"}:
                stopped.append((label, identity, "—", _status(scene.get("status")), scene.get("blocked_by") or scene.get("error")))
        one, two = _checkpoints(a), _checkpoints(b)
        planned = _list(plan.get("checkpoints"))
        lookup = {item.get("id"): item for item in planned}
        for cp_id in _ids(planned, one, two):
            x, y = one.get(cp_id, {}), two.get(cp_id, {})
            sx, sy = x.get("status"), y.get("status")
            difference = "记录不足" if sx is None or sy is None else "一致" if sx == sy else "不同"
            cp_rows.append((identity, cp_id, ", ".join(lookup.get(cp_id, {}).get("depends_on", [])) or "—",
                _status(sx), _status(sy), difference, x.get("blocked_by") or x.get("error"),
                y.get("blocked_by") or y.get("error"), _link(x.get("screenshot"), root, "首次遮罩截图") or "—"))
            for label, checkpoint in (("首次", x), ("重跑", y)):
                if checkpoint.get("blocked_by") or checkpoint.get("status") in {"blocked", "cancelled", "error", "failed"}:
                    stopped.append((label + "检查点", identity, cp_id, _status(checkpoint.get("status")),
                                    checkpoint.get("blocked_by") or checkpoint.get("error")))
                for item in _list(checkpoint.get("evidence")):
                    assertions.append((label, identity, cp_id, item.get("rule_id"), item.get("target"),
                        item.get("operator"), item.get("expected"), item.get("actual"), _status(item.get("status")),
                        item.get("id"), item.get("condition_fingerprint")))
    add("业务链与会话隔离", ("场景", "说明", "依赖场景", "首次", "重跑", "首次 context", "重跑 context", "首次原因"), scene_rows)
    add("检查点与首次/重跑差异", ("场景", "检查点", "依赖", "首次", "重跑", "对照", "首次原因", "重跑原因", "证据"), cp_rows,
        [replay_note or "比较的是最终检查点状态，不替代原始失败、修复历史及记录中的 stability。"])
    add("记录的确定性断言", ("轮次", "场景", "检查点", "需求", "目标", "比较", "预期", "实际", "状态", "证据 ID", "条件指纹"), assertions)

    counts, coverage = _coverage(suite, first_scenes)
    _, second_coverage = _coverage(suite, second_scenes)
    rows = []
    for rule in _list(suite.get("rules")):
        identity, source = rule.get("id"), _obj(rule.get("source"))
        count, observed = counts.get(identity, 0), coverage.get(identity, [])
        rows.append((identity, rule.get("statement"), source.get("kind"), source.get("reference"), rule.get("required", True),
            count, sum(value is not None for value in observed), observed.count("passed"), _coverage_label(count, observed),
            _coverage_label(count, second_coverage.get(identity, [])) if second else "未提供逐项记录"))
    inconsistent = any(rule.get("required", True) and (
        not counts.get(rule.get("id")) or _coverage_label(counts[rule["id"]], coverage.get(rule["id"], [])) != "记录断言均通过")
        for rule in _list(suite.get("rules")))
    warning = "原记录标记通过，但必需断言记录不足或未通过；请核对原证据，报告没有改写状态。" if (
        inconsistent and (first.get("status") == "passed" or result.get("status") == "passed")) else ""
    add("需求覆盖", ("需求", "规则", "来源类型", "来源引用", "必需", "计划断言", "首次已记录", "首次通过", "首次覆盖记录", "重跑覆盖记录"), rows,
        ["按计划 rule/target/operator/expected 关联已记录断言。失败断言仍算已执行，缺失记录不算通过。",
         "覆盖展示不证明需求本身完整或正确，不根据模型文字补造通过证据。", warning])

    actual_human = (manual.get("actual_console_input") is True and manual.get("provider") == "ConsoleProvider"
                    and manual.get("input_channel") == "parent_console_stdin" and manual.get("headless") is False)
    participation = "提供了真实人工控制台证据，状态：" + _text(manual.get("status")) if actual_human else "未提供真实人工证据；请求、答复和等待时间本身不证明真人输入。"
    timing = _obj(first.get("timing"))
    add("需求澄清与人工参与", ("答复 ID", "已脱敏答复"), _obj(result.get("answers")).items(),
        [participation, "主动执行秒数：" + _text(timing.get("active_seconds")) + "；资源/人工等待秒数：" + _text(timing.get("human_wait_seconds")),
         "固定答案和 ScriptedProvider 不冒称真实人工。"])
    resource_rows = []
    for label, events in (("首次", resources), ("重跑", replay_resources)):
        for event in events:
            resource_rows.append((label, event.get("id"), event.get("ref"), event.get("kind"), event.get("scene_id"),
                event.get("step_id"), event.get("status"), event.get("challenge_id"), event.get("expires_at"), event.get("error")))
    pending = _obj(result.get("pending_resource"))
    if pending:
        resource_rows.append(("当前等待", pending.get("logical_request_id"), pending.get("ref"), pending.get("kind"),
            pending.get("scene_id"), pending.get("step_id"), pending.get("status"), pending.get("challenge_id"),
            pending.get("expires_at"), "等待继续，不表示重发或已完成"))
    add("资源请求（仅引用和当次身份）", ("轮次", "请求 ID", "资源引用", "类型", "场景", "步骤", "状态", "challenge", "expiry", "原因"), resource_rows,
        ["不展示资源 value、delivery_hint、缓存或浏览器存储；没有 sidecar 时不推造请求次数。"])
    add("阻塞、失败与未完成", ("范围", "场景", "检查点", "状态", "原因/前置依赖"), stopped, [result.get("error")])

    failures = _list(first.get("initial_failures")) or _list(result.get("initial_failures"))
    add("原始失败保留", ("场景", "检查点", "动作", "原目标", "原状态", "错误"), [
        (item.get("scenario_id"), item.get("checkpoint_id"), _obj(item.get("action")).get("id"),
         _obj(item.get("action")).get("target"), _status(item.get("status")), item.get("error")) for item in failures])
    rows = []
    for item in _list(first.get("repair_history")) or _list(result.get("repair_history")):
        diagnosis = _obj(_obj(item.get("assessment")).get("diagnosis"))
        proposed = "; ".join(_text(action.get("id")) + ": " + _text(action.get("kind")) + " @ " + _text(action.get("target"))
                            for action in _list(_obj(item.get("proposed_plan")).get("proposed_actions")))
        rows.append((item.get("scenario_id"), item.get("checkpoint_id"), item.get("action_id"), item.get("attempt"),
            item.get("decision"), item.get("execution_status"), ATTRIBUTION.get(diagnosis.get("attribution"), diagnosis.get("attribution")),
            proposed or "—", item.get("reason")))
    add("修复历史", ("场景", "检查点", "原动作", "次数", "审核", "执行", "诊断归因", "提议动作（无输入值）", "停止/拒绝原因"), rows,
        ["提议、程序批准与实际结果分开。批准或修复后通过不抹去原始失败。"])

    diagnostics = _obj(result.get("diagnostics"))
    assessment = _obj(diagnostics.get("assessment"))
    diagnosis = _obj(assessment.get("diagnosis"))
    attribution = diagnosis.get("attribution", diagnostics.get("attribution"))
    add("证据诊断", ("项目", "原记录"), [
        ("诊断状态", diagnostics.get("status", "未触发或未提供")), ("归因", ATTRIBUTION.get(attribution, attribution)),
        ("来源轮次", diagnostics.get("origin", result.get("diagnostic_origin"))),
        ("诊断 run", diagnostics.get("execution_run_id", result.get("diagnostic_run_id"))),
        ("解释", diagnosis.get("explanation", diagnostics.get("error"))), ("证据 ID", diagnosis.get("evidence_ids")),
        ("反证 ID", diagnosis.get("counter_evidence_ids")), ("不确定性", diagnosis.get("uncertainties", diagnostics.get("uncertainties"))),
        ("待澄清", assessment.get("clarification_questions")), ("调查建议", assessment.get("investigations")),
        ("修复建议（不是执行记录）", assessment.get("repair_recommendations")), ("覆盖发现", assessment.get("coverage_findings"))])
    add("独立 Pytest 汇总", ("项目", "原记录"), [("状态", _status(summary.get("status"))),
        ("用例数", summary.get("tests")), ("失败", summary.get("failures")), ("错误", summary.get("errors")),
        ("跳过", summary.get("skipped")), ("阻塞", summary.get("blocked")), ("退出码", summary.get("exit_code")),
        ("说明", summary.get("detail"))])
    add("本地证据与脚本", ("文件",), [(link,) for name in ARTIFACTS if (link := _link(name, root))],
        ["仅链接指定运行目录内的现有文件，不扫描 canonical 或外部目录。完整脚本沿用 complete_script 原字段，部分计划不冒充完整套件。"])

    def cell(value):
        if isinstance(value, _Link):
            return {"kind": "local_link", "label": value.label, "href": value.href}
        return _text(value)
    return {"format": FORMAT, "version": 1, "sections": [
        {"id": identity, "title": title, "headers": list(headers),
         "rows": [[cell(value) for value in row] for row in rows], "paragraphs": list(paragraphs)}
        for identity, (title, headers, rows, paragraphs) in zip(SECTION_IDS, sections)]}


def _validate_view(view, root):
    if not isinstance(view, dict) or set(view) != {"format", "version", "sections"} or view["format"] != FORMAT or type(view["version"]) is not int or view["version"] != 1:
        raise ValueError("Not an owned suite-report view")
    sections = view["sections"]
    if not isinstance(sections, list) or len(sections) != len(SECTION_IDS):
        raise ValueError("Report sections differ from the owned format")
    for identity, title, section in zip(SECTION_IDS, TITLES, sections):
        if not isinstance(section, dict) or set(section) != {"id", "title", "headers", "rows", "paragraphs"}:
            raise ValueError("Invalid report section")
        if section["id"] != identity or section["title"] != title:
            raise ValueError("Report section identity changed")
        if not isinstance(section["headers"], list) or not 1 <= len(section["headers"]) <= 12 or not all(isinstance(value, str) for value in section["headers"]):
            raise ValueError("Invalid report table header")
        if not isinstance(section["paragraphs"], list) or not all(isinstance(value, str) for value in section["paragraphs"]):
            raise ValueError("Invalid report paragraph")
        if not isinstance(section["rows"], list):
            raise ValueError("Invalid report table")
        for row in section["rows"]:
            if not isinstance(row, list) or len(row) != len(section["headers"]):
                raise ValueError("Invalid report row")
            for cell in row:
                if isinstance(cell, str):
                    continue
                if not isinstance(cell, dict) or set(cell) != {"kind", "label", "href"} or cell["kind"] != "local_link" or not isinstance(cell["label"], str) or not isinstance(cell["href"], str):
                    raise ValueError("Invalid report cell")
                href = cell["href"]
                decoded = unquote(href)
                parsed = urlsplit(href)
                if root is None or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path != href or "\\" in decoded or ":" in decoded:
                    raise ValueError("Report link is not a local artifact")
                if quote(decoded, safe="/._-") != href or _link(decoded, root) != _Link(decoded, href):
                    raise ValueError("Report link escapes or differs from its local artifact")


def _render_view(view, root=None):
    _validate_view(view, root)
    markdown = ["# 网页端到端测试套件报告", ""]
    body = ["<h1>网页端到端测试套件报告</h1>"]
    for section in view["sections"]:
        title, headers, rows, paragraphs = (section[key] for key in ("title", "headers", "rows", "paragraphs"))
        rows = [[_Link(value["label"], value["href"]) if isinstance(value, dict) else value for value in row] for row in rows]
        markdown.extend(["## " + title, ""])
        body.append('<section id="' + section["id"] + '"><h2>' + html.escape(title) + "</h2>")
        for paragraph in paragraphs:
            markdown.extend([_md(paragraph), ""])
            body.append("<p>" + _html(paragraph) + "</p>")
        markdown.extend(["| " + " | ".join(map(_md, headers)) + " |", "| " + " | ".join("---" for _ in headers) + " |"])
        body.append('<div class="table"><table><thead><tr>' + "".join("<th>" + _html(head) + "</th>" for head in headers) + "</tr></thead><tbody>")
        for row in rows or [("无记录",) + ("—",) * (len(headers) - 1)]:
            markdown.append("| " + " | ".join(map(_md, row)) + " |")
            body.append("<tr>" + "".join("<td>" + _html(cell) + "</td>" for cell in row) + "</tr>")
        markdown.append("")
        body.append("</tbody></table></div></section>")
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>网页端到端测试套件报告</title>
<style>body{font:16px/1.65 system-ui,sans-serif;color:#182536;background:#f4f6f9;margin:0;padding:28px}main{max-width:1440px;margin:auto}h1{font-size:28px}h2{font-size:21px;margin-top:0}section{background:white;border:1px solid #dce2ea;border-radius:10px;padding:20px;margin:18px 0}p{max-width:100ch}.table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;border:1px solid #dce2ea;padding:8px;overflow-wrap:anywhere;min-width:6ch}th{background:#edf1f6}a{color:#1457b3}td{white-space:pre-wrap}</style>
</head><body><main>""" + "".join(body) + "</main></body></html>\n"
    return "\n".join(markdown), document


def render_suite_report(safe_result, *, safe_replay_execution=None, safe_resource_events=(),
                        safe_replay_resource_events=(), safe_manual_evidence=None, artifact_root=None):
    """Return escaped Markdown/HTML without changing inputs or reading plans."""
    view = _build_view(safe_result, safe_replay_execution=safe_replay_execution,
        safe_resource_events=safe_resource_events, safe_replay_resource_events=safe_replay_resource_events,
        safe_manual_evidence=safe_manual_evidence, artifact_root=artifact_root)
    root = Path(artifact_root).resolve() if artifact_root is not None else None
    return _render_view(view, root)


def write_suite_report(directory, safe_result, **options):
    """Write two reports and their minimal deterministic input; state stays intact."""
    directory = Path(directory)
    if options.get("artifact_root") is not None and Path(options["artifact_root"]).resolve() != directory.resolve():
        raise ValueError("Report links must use the output run directory")
    view = _build_view(safe_result, **{**options, "artifact_root": directory})
    markdown, document = _render_view(view, directory.resolve())
    directory.mkdir(parents=True, exist_ok=True)
    paths = {"markdown": directory / "report.md", "html": directory / "report.html", "input": directory / "report-input.json"}
    paths["markdown"].write_text(markdown, encoding="utf-8", newline="\n")
    paths["html"].write_text(document, encoding="utf-8", newline="\n")
    paths["input"].write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return {kind: str(path) for kind, path in paths.items()}


def is_generated_report(path):
    """Recognize only byte-exact escaped report.html plus its owned local input.

    A copied name, marker, or arbitrary raw DOM cannot qualify. This is a format
    and rendering safety check, not a substitute for plaintext-secret scans.
    """
    try:
        path = Path(path)
        if path.name != "report.html" or not path.is_file() or path.is_symlink():
            return False
        root = path.parent.resolve()
        source = root / "report-input.json"
        if not source.is_file() or source.is_symlink():
            return False
        view = json.loads(source.read_text(encoding="utf-8"))
        _, document = _render_view(view, root)
        return path.read_bytes() == document.encode("utf-8")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return False
