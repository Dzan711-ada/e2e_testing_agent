import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from e2e_agent.app import run_test
from e2e_agent.config import Settings
from e2e_agent.models import BrowserResult, ReplayResult, TestPlan


class FakeBrowser:
    def __init__(self, settings, run_dir):
        self.closed = False
        self.calls = []
        self.results = []
        self.open_ok = True

    async def start(self, url):
        self.calls.append(("start", url))
        return BrowserResult(ok=self.open_ok, html='<button>Go</button><div id="success">OK</div>', error="" if self.open_ok else "Cannot open")

    async def execute(self, code, step, assertion):
        self.calls.append(("execute", step, assertion))
        return self.results.pop(0) if self.results else BrowserResult(ok=True, html="<div>OK</div>")

    async def close(self):
        self.closed = True


class ScriptedPlanner:
    def __init__(self, snippets=None):
        self.snippets = snippets or ['await page.get_by_text("Go").click()',
                                     'await expect(page.locator("#success")).to_be_visible()']
        self.requests = []

    async def plan(self, query, target_url):
        return TestPlan(actions=[dict(description="点击", kind="action"), dict(description="检查结果", kind="assertion")])

    async def code(self, **payload):
        self.requests.append(payload)
        return self.snippets[len(self.requests) - 1]


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(root=self.root, deepseek_api_key="should-not-leak")
        self.browsers = []
        self.replays = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    def browser_factory(self, settings, run_dir):
        browser = FakeBrowser(settings, run_dir)
        self.browsers.append(browser)
        return browser

    async def replay(self, settings, run_dir):
        self.replays.append(run_dir)
        return ReplayResult(status="passed", exit_code=0, tests=1)

    async def run_case(self, **overrides):
        args = dict(settings=self.settings, query="点击后检查结果", target_url="https://example.com",
                    planner=ScriptedPlanner(), browser_factory=self.browser_factory, replay_runner=self.replay)
        args.update(overrides)
        return await run_test(**args)

    async def test_full_graph_uses_current_dom_and_prior_code(self):
        planner = ScriptedPlanner()
        result = await self.run_case(planner=planner)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.replays), 1)
        self.assertEqual(len([c for c in self.browsers[0].calls if c[0] == "start"]), 1)
        self.assertTrue(self.browsers[0].closed)
        self.assertEqual(planner.requests[1]["html"], "<div>OK</div>")
        self.assertIn('get_by_text("Go")', planner.requests[1]["previous_code"])
        run_dir = Path(result["run_dir"])
        self.assertTrue((run_dir / "test_generated.py").is_file())
        self.assertTrue((run_dir / "report.md").is_file())
        self.assertNotIn("should-not-leak", (run_dir / "result.json").read_text(encoding="utf-8"))

    async def test_final_step_without_assertion_is_rejected(self):
        result = await self.run_case(planner=ScriptedPlanner(['await page.title()', 'await page.title()']))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["phase"], "validate")
        self.assertFalse(self.replays)
        self.assertFalse((Path(result["run_dir"]) / "test_generated.py").exists())
        self.assertTrue((Path(result["run_dir"]) / "partial_steps.py").is_file())

    async def test_failed_final_assertion_is_exported_and_replayed(self):
        def factory(settings, run_dir):
            browser = self.browser_factory(settings, run_dir)
            browser.results = [BrowserResult(ok=True), BrowserResult(ok=False, error="Actual: hidden", error_type="AssertionError")]
            return browser

        async def replay(settings, run_dir):
            return ReplayResult(status="failed", exit_code=1, tests=1, failures=1)

        result = await self.run_case(browser_factory=factory, replay_runner=replay)
        self.assertEqual(result["status"], "failed")
        self.assertTrue((Path(result["run_dir"]) / "test_generated.py").is_file())
        self.assertFalse(result["events"][-1]["ok"])

    async def test_inconsistent_replay_is_not_reported_as_stable_pass(self):
        def factory(settings, run_dir):
            browser = self.browser_factory(settings, run_dir)
            browser.results = [BrowserResult(ok=True), BrowserResult(ok=False, error="hidden", error_type="AssertionError")]
            return browser
        result = await self.run_case(browser_factory=factory)
        self.assertEqual(result["status"], "error")
        self.assertIn("结果不一致", result["error"])

    async def test_early_runtime_failure_does_not_export_complete_test(self):
        def factory(settings, run_dir):
            browser = self.browser_factory(settings, run_dir)
            browser.results = [BrowserResult(ok=False, error="No matching button", error_type="TimeoutError")]
            return browser
        result = await self.run_case(browser_factory=factory)
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.replays)
        self.assertTrue(self.browsers[0].closed)

    async def test_model_failure_still_produces_redacted_report(self):
        class BrokenPlanner(ScriptedPlanner):
            async def plan(self, query, target_url):
                raise RuntimeError("Service failed should-not-leak")
        result = await self.run_case(planner=BrokenPlanner())
        self.assertEqual(result["status"], "error")
        self.assertIn("[REDACTED]", result["error"])
        self.assertTrue(self.browsers[0].closed)
        self.assertTrue((Path(result["run_dir"]) / "report.md").exists())

    async def test_plan_budget_is_enforced_before_browser_start(self):
        result = await self.run_case(settings=replace(self.settings, max_actions=1))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.browsers[0].calls, [])

    async def test_run_directories_are_isolated(self):
        first, second = await self.run_case(), await self.run_case()
        self.assertNotEqual(first["run_dir"], second["run_dir"])


if __name__ == "__main__":
    unittest.main()
