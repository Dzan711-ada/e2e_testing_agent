"""Browser/graph verification; omit --offline to additionally call model APIs."""

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent.app import run_test
from e2e_agent.cli import DEFAULT_QUERY, progress
from e2e_agent.codegen import save_json
from e2e_agent.config import Settings
from e2e_agent.llm import DemoPlanner, ModelPlanner
from e2e_agent.sample import sample_server


class DeliberatelyFailingPlanner(DemoPlanner):
    async def plan(self, query, target_url):
        plan = await super().plan(query, target_url)
        plan.actions[1].description = "填写不一致密码"
        return plan

    async def code(self, **payload):
        if payload["action"]["description"] == "填写不一致密码":
            return ('await page.get_by_test_id("passwordInput").fill("TestPass123!")\n'
                    'await page.get_by_test_id("confirmPasswordInput").fill("Different123!")')
        return await super().code(**payload)


async def validate(settings, *, offline):
    report = {}
    with sample_server() as url:
        for label, planner, expected in [
            ("fixed_browser_flow", DemoPlanner(), "passed"),
            ("deliberate_assertion_failure", DeliberatelyFailingPlanner(), "failed"),
        ]:
            result = await run_test(settings, query=DEFAULT_QUERY, target_url=url, planner=planner,
                                    sample_site=True, progress=progress)
            report[label] = {"status": result["status"], "run_dir": result["run_dir"], "expected": expected}
            if result["status"] != expected:
                raise AssertionError(f"{label}: expected {expected}, got {result['status']}; {result['run_dir']}")
        if not offline:
            result = await run_test(settings, query=DEFAULT_QUERY, target_url=url, planner=ModelPlanner(settings),
                                    sample_site=True, progress=progress)
            report["live_model_flow"] = {"status": result["status"], "run_dir": result["run_dir"]}
            if result["status"] != "passed":
                raise AssertionError(f"Live workflow failed; inspect {result['run_dir']}")
    save_json(settings.runtime_path / "smoke_validation.json", report)
    return report


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="不调用模型；仍使用真实 Chromium/Pytest")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.env_file)
    if not args.offline:
        settings.require_model()
    report = asyncio.run(validate(settings, offline=args.offline))
    for name, value in report.items():
        print(f"{name}: {value['status']}")
    print(f"验证记录：{settings.runtime_path / 'smoke_validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
