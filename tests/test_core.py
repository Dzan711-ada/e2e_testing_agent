import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from e2e_agent.codegen import render_test, validate_snippet
from e2e_agent.config import Settings, validate_url
from e2e_agent.models import TestPlan
from e2e_agent.replay import read_result


class ConfigTests(unittest.TestCase):
    def test_http_and_https_targets(self):
        self.assertEqual(validate_url(" https://example.com/path?q=a "), "https://example.com/path?q=a")
        self.assertEqual(validate_url("http://127.0.0.1:1234"), "http://127.0.0.1:1234")
        for url in ("file:///tmp/test.html", "example.com", "http://a:bad", "http://a:0", "https://u:p@a/", "https://a b/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_environment_precedence_and_no_cross_file_pollution(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "first.env", Path(folder) / "second.env"
            first.write_text('DEEPSEEK_API_KEY="first-secret"\n', encoding="utf-8")
            second.write_text('DEEPSEEK_API_KEY="second-secret"\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Settings.load(first).deepseek_api_key, "first-secret")
                self.assertEqual(Settings.load(second).deepseek_api_key, "second-secret")
                os.environ["DEEPSEEK_API_KEY"] = "environment-secret"
                self.assertEqual(Settings.load(first).deepseek_api_key, "environment-secret")

    def test_invalid_limits_and_no_secret_repr(self):
        settings = Settings(deepseek_api_key="hidden-key")
        self.assertNotIn("hidden-key", repr(settings))
        self.assertEqual(settings.redact("failed hidden-key"), "failed [REDACTED]")
        for entry in ("WORKER_TIMEOUT=nan", "MAX_ACTIONS=0", "HEADLESS=maybe"):
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
                path = Path(folder) / "bad.env"
                path.write_text(entry, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(path)


class PlanAndCodeTests(unittest.TestCase):
    def test_final_step_requires_assertion(self):
        with self.assertRaises(ValueError):
            TestPlan(actions=[dict(description="Click", kind="action")])
        with self.assertRaises(ValueError):
            TestPlan(actions=[dict(description=" ", kind="assertion")])
        self.assertEqual(TestPlan(actions=[dict(description="Check", kind="assertion")]).actions[0].kind, "assertion")

    def test_async_code_and_markdown_fences(self):
        source = 'await page.get_by_role("button", name="Register").click()'
        self.assertEqual(validate_snippet("```python\n" + source + "\n```"), source)
        self.assertTrue(validate_snippet('await expect(page.locator("#success")).to_be_visible()', assertion=True))

    def test_assertion_step_cannot_be_a_click(self):
        with self.assertRaises(ValueError):
            validate_snippet('await page.locator("button").click()', assertion=True)

    def test_unsupported_code_and_vacuous_assertion(self):
        sources = ['import os\nawait page.title()', 'while True:\n await page.title()',
                   'try:\n await page.title()\nexcept:\n pass',
                   'exec("x")\nawait page.title()', 'assert True\nawait page.title()',
                   'await page.__class__()', '# only a comment', 'break\nawait page.title()']
        for source in sources:
            with self.subTest(source=source), self.assertRaises((ValueError, SyntaxError)):
                validate_snippet(source)

    def test_export_is_valid_with_quoted_url_and_regex(self):
        script = render_test(target_url='https://example.com/?q="a"&x=\\1',
                             snippets=['await expect(page.locator("#success")).to_have_text(re.compile(r"OK\\d+"))'],
                             headless=True, action_timeout_ms=1000, browsers_path=Path("X:/browser files"))
        ast.parse(script)
        self.assertIn("def test_generated_scenario():", script)
        self.assertIn('from playwright.async_api import async_playwright, expect', script)
        self.assertNotIn("# Next Action", script)


class ReplayResultTests(unittest.TestCase):
    def result(self, xml, code=0):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            if xml is not None:
                path.write_text(xml, encoding="utf-8")
            return read_result(path, code)

    def test_one_executed_test_passes(self):
        self.assertEqual(self.result('<testsuites><testsuite><testcase name="test_x"/></testsuite></testsuites>').status, "passed")

    def test_no_test_skipped_and_multiple_tests_do_not_pass(self):
        for xml in ('<testsuites/>', '<testsuite><testcase><skipped/></testcase></testsuite>',
                    '<testsuite><testcase/><testcase/></testsuite>'):
            with self.subTest(xml=xml):
                self.assertEqual(self.result(xml).status, "error")

    def test_failure_error_and_exit_code(self):
        self.assertEqual(self.result('<testsuite><testcase><failure/></testcase></testsuite>', 1).status, "failed")
        self.assertEqual(self.result('<testsuite><testcase><error/></testcase></testsuite>', 1).status, "error")
        self.assertEqual(self.result('<testsuite><testcase/></testsuite>', 2).status, "error")

    def test_missing_and_malformed_reports(self):
        self.assertEqual(self.result(None).status, "error")
        self.assertEqual(self.result('broken').status, "error")


if __name__ == "__main__":
    unittest.main()
