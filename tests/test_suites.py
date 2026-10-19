import ast
from pathlib import Path
import tempfile
import unittest

from e2e_agent.actions import ActionError, ActionExecutor, compile_action
from e2e_agent.domain import Acceptance, Action, Budget
from e2e_agent.replay import read_result
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_demo import shop_suite


class SuiteJUnitTests(unittest.TestCase):
    expected = {"test_a": "a", "test_b": "b"}

    def case(self, name, identity, *, run="current", child=""):
        return f'<testcase name="{name}"><properties><property name="scenario_id" value="{identity}"/><property name="e2e_run_id" value="{run}"/></properties>{child}</testcase>'

    def result(self, cases, code=0):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            path.write_text("<testsuite>" + cases + "</testsuite>", encoding="utf-8")
            return read_result(path, code, expected_ids=self.expected, expected_run_id="current")

    def test_exact_identity_set_passes_and_business_failure_remains_failed(self):
        self.assertEqual(self.result(self.case("test_b", "b") + self.case("test_a", "a")).status, "passed")
        self.assertEqual(self.result(self.case("test_a", "a", child="<failure/>") + self.case("test_b", "b"), 1).status, "failed")

    def test_missing_zero_wrong_duplicate_and_wrong_scenario_rejected(self):
        for cases in ("", self.case("test_a", "a"), self.case("test_a", "a") * 2,
                      self.case("test_a", "a") + self.case("test_other", "b"),
                      self.case("test_a", "a") + self.case("test_b", "incorrect")):
            with self.subTest(cases=cases):
                self.assertEqual(self.result(cases).status, "error")

    def test_skip_old_run_and_unexpected_exit_rejected(self):
        for cases, code in ((self.case("test_a", "a", child="<skipped/>") + self.case("test_b", "b"), 0),
                            (self.case("test_a", "a", run="old") + self.case("test_b", "b", run="old"), 0),
                            (self.case("test_a", "a") + self.case("test_b", "b"), 2),
                            ('<testcase name="test_a"/><testcase name="test_b"/>', 0)):
            with self.subTest(code=code, cases=cases):
                self.assertEqual(self.result(cases, code).status, "error")


class StructuredPolicyTests(unittest.TestCase):
    def test_acceptance_operators_reject_inconsistent_expected_values(self):
        for operator, expected in (("visible", False), ("hidden", "yes"), ("count", True), ("count", -1), ("url", "")):
            with self.subTest(operator=operator, expected=expected), self.assertRaises(ValueError):
                Acceptance(rule_id="rule", target="testid:target", operator=operator, expected=expected)

    def test_navigation_cannot_expand_origin_or_namespace(self):
        executor = ActionExecutor(None, "http://127.0.0.1:123/shop/run", {}, Budget())
        self.assertEqual(executor.safe_url("login"), "http://127.0.0.1:123/shop/run/login")
        for target in ("https://example.com/", "http://127.0.0.1:123/admin", "http://u:p@127.0.0.1:123/shop/run/login"):
            with self.subTest(target=target), self.assertRaises(ActionError):
                executor.safe_url(target)

    def test_compilation_quotes_values_and_keeps_secret_as_reference(self):
        action = Action(id="fill", checkpoint_id="login", kind="fill", intent="Password", target="label:Password", resource_id="password")
        source = compile_action(action, "http://127.0.0.1")
        ast.parse("async def scenario():\n" + "\n".join("    " + line for line in source.splitlines()))
        self.assertIn("resource('password')", source)
        self.assertIn("await unique(", source)
        self.assertNotIn("first", source)
        with self.assertRaises(ActionError):
            compile_action(action.model_copy(update={"target": "python:__import__('os')"}), "http://localhost")

    def test_fill_and_navigation_reject_ambiguous_or_absent_data(self):
        for payload in ({"kind": "fill", "target": "label:Username"}, {"kind": "navigate", "value": None},
                        {"kind": "fill", "target": "label:Password", "value": "x", "resource_id": "password"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                Action(id="action", checkpoint_id="login", intent="action", **payload)

    def test_export_is_complete_and_independent_of_model_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            suite = shop_suite()
            manifest = export_suite(suite, directory, Path("X:/browsers"))
            self.assertEqual(set(manifest["tests"].values()), {"main-chain", "wrong-password", "unauthenticated"})
            self.assertEqual(len(manifest["tests"]), 3)
            source = (directory / "test_generated.py").read_text(encoding="utf-8")
            ast.parse(source)
            self.assertNotIn("langchain", source)
            self.assertNotIn("deepseek", source.lower())
            self.assertFalse((directory / ".env").exists())
            self.assertTrue((directory / "e2e_support/coordinator.py").is_file())
