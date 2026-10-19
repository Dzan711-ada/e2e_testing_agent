"""Required actual console acceptance: headed first run and parent-console pytest replay."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.replay import run_replay
from e2e_agent.resources import ConsoleProvider
from e2e_agent.sms_demo import sms_suite
from e2e_agent.suite_codegen import export_suite

CORE = ["e2e_agent/" + name for name in ("resources.py", "resource_bridge.py", "actions.py", "action_policy.py", "coordinator.py", "execution_runtime.py", "execution_graph.py", "graph_state.py", "graph_checkpoint.py", "replay.py", "suite_codegen.py", "sms_demo.py", "domain.py", "export_launcher.py", "sample_shop.py", "evidence.py")]
CORE.append("scripts/manual_sms.py")
CORE.append("e2e_agent/site_profiles.py")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CORE}


async def verify(output, report):
    settings = replace(Settings.load(), headless=False)
    provider, suite = ConsoleProvider(allow_test_delivery=True), sms_suite()
    (output / "plan.json").write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    print("人工验收：使用虚构测试手机号 15500000001；按控制台提示输入并直接回车。浏览器仅观察，不手动发码或提交。", flush=True)
    try:
        first = await execute_suite(suite, output / "generation", settings.browsers_path, headless=False,
                                    resource_provider=provider, public_fictional=True)
        report["first"] = first
        assert first["status"] == "passed", "First console-assisted chain did not pass"
        export_suite(suite, output, settings.browsers_path, headless=False, public_fictional=True)
        print("开始独立 Pytest 重跑：这是新一轮，需要再次提供虚构手机号和新的当次验证码。", flush=True)
        replay = await run_replay(settings, output, resource_provider=provider)
        report["replay"] = replay.model_dump()
        assert replay.status == "passed" and replay.tests == 1
        second = json.loads((output / "replay-suite/suite-result.json").read_text(encoding="utf-8"))
        for result in (first, second):
            main = result["scenarios"]["sms-chain"]
            assert main["stats"] == {"login_count": 1, "send_count": 1} and main["contexts_created"] == 1
        assert first["run_id"] != second["run_id"]
        assert len(provider.requests) == 4 and all(r["status"] == "provided" for r in provider.requests)
        assert hashes() == report["source_files"], "Resource implementation changed during human acceptance"
        report.update(status="passed", actual_console_input=True, console_requests=provider.requests,
                      rounds=[{"id": "first", "run_id": first["run_id"], "status": first["status"]},
                              {"id": "independent-replay", "run_id": second["run_id"], "status": second["status"]}])
    finally:
        provider.closed = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report = {"schema_version": 1, "stage": 4, "status": "running", "actual_console_input": False,
              "input_channel": "parent_console_stdin", "provider": "ConsoleProvider", "headless": False,
              "resource_mode": "public_fictional_local_sms", "source_files": hashes(),
              "started": datetime.now(timezone(timedelta(hours=8))).isoformat()}
    path = args.output / "manual-evidence.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        asyncio.run(verify(args.output, report))
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed", actual_console_input=False, error=Settings.load().redact(f"{type(error).__name__}: {error}"))
    finally:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"人工验收：{report['status']}；证据：{path}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
