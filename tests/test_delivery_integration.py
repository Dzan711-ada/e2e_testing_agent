"""Delivery integration boundaries, without a real browser or model."""
import asyncio
from dataclasses import replace
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from e2e_agent import agent_workflow as workflow
from e2e_agent.actions import ActionError, ActionExecutor, locator_expression
from e2e_agent.config import Settings
from e2e_agent.domain import Budget, Suite
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge
from e2e_agent.models import ReplayResult
from e2e_agent.practice_resources import public_demo_resources
from e2e_agent.resources import ScriptedProvider
from e2e_agent.site_profiles import PRACTICE_PROFILE, PRACTICE_START_URL, validate_profile_url
from e2e_agent.suite_codegen import export_suite
from e2e_agent.suite_reporting import write_suite_report
from scripts.verify_resources import scan
from test_agent_graph import FrozenTransport, fixture_suite, sources
from test_execution_graph import DeterministicServices
from test_site_profiles import practice_data


class DeliveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_text_compiler_anchors_and_escapes_observed_refs(self):
        class Page:
            def get_by_role(self, role):
                self.role = role
                return self
            def filter(self, *, has_text):
                self.pattern = has_text
                return self
        for role in ("button", "link"):
            page, text = Page(), "Login.*[owned]'\\name"
            locator = eval(locator_expression(role + ":" + text), {"page": page, "re": re})
            self.assertIs(locator, page)
            self.assertEqual(page.role, role)
            self.assertIsNotNone(page.pattern.fullmatch(" \n" + text + "\t"))
            self.assertIsNone(page.pattern.fullmatch("Login-other-owned"))
            self.assertIsNone(page.pattern.fullmatch(text + " extra"))
            self.assertIsNone(page.pattern.fullmatch("extra " + text))

    async def test_public_profile_masks_demo_values_before_first_native_checkpoint(self):
        values = public_demo_resources(PRACTICE_PROFILE)
        with tempfile.TemporaryDirectory() as folder:
            settings, model = replace(Settings(), root=Path(folder)), FrozenTransport()
            host = workflow.AgentGraphHost(settings, model, (None, None), Path(folder),
                ScriptedProvider(values), site_profile=PRACTICE_PROFILE)
            try:
                state = workflow.initial_agent_state(" ".join(values.values()), host=host,
                    confirmed_answers={"note": list(values.values())})
                bridge = AsyncNodeBridge()
                graph = workflow.build_agent_graph(settings, model, workflow.ScriptedAnswers([]),
                    (None, None), Path(folder), host.resource_provider, host=host, bridge=bridge)
                session = AsyncGraphSession(graph, bridge)
                config = {"configurable": {"thread_id": host.run_id}}
                await session.ainvoke(state, config, interrupt_before=["observe"])
                snapshot = await session.aget_state(config)
                self.assertEqual(snapshot.next, ("observe",))
                self.assertIsNone(host.runtime)
                for serialized in (json.dumps(state), json.dumps(snapshot.values),
                                   json.dumps(host.safe({"error": list(values.values())}))):
                    for value in values.values():
                        self.assertNotIn(value, serialized)
                self.assertEqual(model.calls, [])
            finally:
                host.clear_resources()

    async def test_disabled_diagnosis_controls_actual_parent_route_without_model_call(self):
        suite, services, model = fixture_suite(), DeterministicServices(failure=("alpha", "first")), FrozenTransport()
        with tempfile.TemporaryDirectory() as folder, services.patched(), \
             patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
             patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="failed", exit_code=1))), \
             patch("e2e_agent.diagnostics.diagnose_execution", new_callable=AsyncMock) as diagnose:
            directory = Path(folder) / "run"
            result = await workflow.run_agent_suite(replace(Settings(), root=Path(folder)), query=suite.query,
                run_dir=directory, frozen_suite=suite, confirmed_sources=sources(suite), transport=model,
                final_diagnose_enabled=False)
            self.assertNotEqual(result["status"], "passed")
            self.assertFalse(result["final_diagnose_enabled"])
            self.assertEqual(result["diagnostics"]["status"], "disabled")
            self.assertEqual(model.calls, [])
            diagnose.assert_not_called()
            events = json.loads((directory / "parent-events.json").read_text(encoding="utf-8"))
            self.assertIn("analyze", [event["node"] for event in events])
            self.assertNotIn("final_diagnose", [event["node"] for event in events])
            self.assertTrue((directory / "report.html").is_file())

    async def test_fixture_faults_are_private_services_not_graph_or_model_input(self):
        suite, model = fixture_suite(), FrozenTransport()
        fixture_faults = {"alpha": ["valid_login_denied"]}
        with tempfile.TemporaryDirectory() as folder:
            host = workflow.AgentGraphHost(replace(Settings(), root=Path(folder)), model, (None, None),
                Path(folder), ScriptedProvider({}), faults=fixture_faults)
            try:
                state = workflow.initial_agent_state(suite.query, host=host, frozen_suite=suite,
                                                     confirmed_sources=sources(suite))
                fixture_faults["alpha"].append("session_lost")
                self.assertEqual(host.runtime.faults, {"alpha": ["valid_login_denied"]})
                self.assertNotIn("valid_login_denied", json.dumps(state))
                self.assertNotIn("valid_login_denied", json.dumps(model.calls))
            finally:
                host.clear_resources()

    async def test_selected_profile_url_policy_restricts_routes_and_keeps_local_prefix(self):
        executor = ActionExecutor(None, PRACTICE_START_URL, {}, Budget(),
            url_policy=lambda url: validate_profile_url(PRACTICE_PROFILE, url))
        self.assertEqual(executor.safe_url("login"), PRACTICE_START_URL + "login")
        self.assertEqual(executor.safe_url("authenticate"), PRACTICE_START_URL + "authenticate")
        with self.assertRaises(ValueError):
            executor.safe_url("download")
        local = ActionExecutor(None, "http://127.0.0.1/shop/owned", {}, Budget())
        with self.assertRaises(ActionError):
            local.safe_url("http://127.0.0.1/shop/another")

    async def test_public_export_contains_refs_without_demo_values_or_model_modules(self):
        suite = Suite.model_validate(practice_data(with_actions=True))
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            export_suite(suite, directory, Path("X:/browser-fixture"))
            self.assertTrue((directory / "e2e_support/site_profiles.py").is_file())
            self.assertFalse((directory / "e2e_support/practice_resources.py").exists())
            self.assertFalse((directory / "e2e_support/agents.py").exists())
            texts = "\n".join(path.read_text(encoding="utf-8") for path in directory.rglob("*") if path.is_file())
            for value in public_demo_resources(PRACTICE_PROFILE).values():
                self.assertNotIn(value, texts)
            self.assertIn('"acquisition": "runtime"', texts)

    async def test_privacy_scan_accepts_exact_report_and_rejects_renamed_or_tampered_html(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_suite_report(root, {"status": "blocked", "query": "<script>unsafe</script>", "model_calls": []})
            self.assertGreaterEqual(scan(root, ["PrivateSentinelForReports!92"]), 3)
            (root / "report.html").write_text("<html><input value='raw-dom'></html>", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "Sensitive raw evidence"):
                scan(root, [])

    async def test_cli_routes_selected_site_to_new_graph_and_rejects_mixed_scope(self):
        from e2e_agent.cli import main
        with tempfile.TemporaryDirectory() as folder, \
             patch("e2e_agent.core.workflow.run", AsyncMock(return_value={"status": "passed", "run_dir": folder})) as run:
            self.assertEqual(await asyncio.to_thread(main, ["run", "--site", PRACTICE_PROFILE, "--query", "只测试正确凭据登录成功"]), 0)
            self.assertEqual(run.call_args.kwargs["target_url"], "https://the-internet.herokuapp.com/login")
            self.assertEqual(run.call_args.kwargs["goal"], "只测试正确凭据登录成功")
            self.assertEqual(await asyncio.to_thread(main, ["run", "--site", PRACTICE_PROFILE, "--url", PRACTICE_START_URL]), 1)
            self.assertEqual(run.call_count, 1)
