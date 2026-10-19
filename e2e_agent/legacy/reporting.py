"""Report generation uses execution results rather than an LLM verdict."""

from pathlib import Path

from .codegen import save_json


LABELS = {"passed": "通过", "failed": "测试失败", "error": "处理未完成", "running": "处理中"}


def write_report(run_dir: Path, state: dict, settings) -> dict:
    result = {key: value for key, value in state.items() if key not in {"html", "current_code"}}
    result["run_dir"] = str(run_dir)
    result["error"] = settings.redact(result.get("error", ""))
    # Result and report can contain business inputs and test data, but never API credentials.
    for event in result.get("events", []):
        event["error"] = settings.redact(event.get("error", ""))
    save_json(run_dir / "result.json", result)
    lines = ["# 网页端到端测试报告", "", f"- 状态：{LABELS[result['status']]}",
             f"- 网址：{result['target_url']}", f"- 阶段：{result.get('phase', '')}",
             f"- 需求：{result['query']}", "", "## 执行步骤", ""]
    for index, action in enumerate(result.get("actions", []), 1):
        event = next((e for e in result.get("events", []) if e["step"] == index), None)
        outcome = "未执行" if event is None else ("成功" if event["ok"] else "失败")
        lines.append(f"{index}. [{outcome}] {action['description']}")
    if result.get("error"):
        lines.extend(["", "## 诊断", "", "```text", result["error"], "```"])
    if result.get("replay"):
        replay = result["replay"]
        lines.extend(["", "## 独立重跑", "", f"用例数：{replay['tests']}；失败：{replay['failures']}；"
                      f"错误：{replay['errors']}；跳过：{replay['skipped']}；退出码：{replay['exit_code']}。"])
    lines.extend(["", "## 产物", ""])
    for name in ("test_generated.py", "partial_steps.py", "junit.xml", "pytest.log",
                 "generation-trace.zip", "replay-trace.zip", "replay.png", "browser-worker.log", "result.json"):
        if (run_dir / name).is_file():
            lines.append(f"- [{name}]({name})")
    lines.extend(["", "通过表示独立 Pytest 用例通过；断言是否完整表达业务需求仍需检查。",
                  "本基线不自动修改失败断言，也不实现失败后的自主重试。", ""])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result
