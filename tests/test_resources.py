import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote_plus

from e2e_agent.domain import ResourceRef
from e2e_agent.resources import (ActiveClock, ConsoleProvider, Redactor, ResourceCancelled, ResourceExpired,
                                 ResourceManager, ResourceRequest, ResourceUnavailable, ScriptedProvider)
from e2e_agent.resource_bridge import ReplayProvider, ResourceBroker
from e2e_agent.replay import read_result
from e2e_agent.coordinator import aggregate
from e2e_agent.agent_workflow import ConsoleAnswers
from types import SimpleNamespace


def specs():
    return [ResourceRef(id="phone", kind="phone", run_id="per-execution", reusable=True),
            ResourceRef(id="sms_code", kind="sms_code", run_id="per-execution")]


def request(**updates):
    values = dict(id="r", ref="phone", kind="phone", purpose="Test fixture", scene_id="scene", step_id="step", run_id="run", wait_timeout=1, public_fictional=True)
    values.update(updates)
    return ResourceRequest(**values)


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def manager(self, provider, run="run", **kwargs):
        folder = self.root / run
        folder.mkdir(exist_ok=True)
        return ResourceManager(specs(), run, provider, ActiveClock(), folder, **kwargs)

    async def test_wait_excluded_from_active_budget_and_phone_reused(self):
        provider = ScriptedProvider({"phone": "15500001111"}, delay=.2)
        manager = self.manager(provider)
        await manager.request("phone", "scene", "phone", "Provide fictional phone")
        await manager.request("phone", "scene", "again", "Reuse current phone")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(manager.resolve("phone"), "15500001111")
        self.assertGreaterEqual(manager.clock.wait_seconds, .15)
        self.assertLess(manager.clock.active_seconds, manager.clock.wait_seconds / 2)
        self.assertNotIn("15500001111", manager.audit_path.read_text())

    async def test_otp_one_time_challenge_run_and_expiry_binding(self):
        manager = self.manager(ScriptedProvider({"sms_code": "987654"}))
        await manager.request("sms_code", "scene", "otp", "Current challenge", challenge_id="challenge-a", phone_ref="phone", expires_at=time.time() + 60)
        self.assertEqual(manager.resolve("sms_code", challenge_id="challenge-a"), "987654")
        with self.assertRaises(ResourceExpired):
            manager.resolve("sms_code", challenge_id="challenge-a")
        manager.current_challenge = "challenge-b"
        with self.assertRaises(ResourceExpired):
            manager.resolve("sms_code", challenge_id="challenge-b")
        fresh = self.manager(ScriptedProvider({}), run="fresh")
        with self.assertRaises(ResourceUnavailable):
            fresh.resolve("sms_code")
        expired = self.manager(ScriptedProvider({"sms_code": "987654"}), run="expired")
        with self.assertRaises(ResourceExpired):
            await expired.request("sms_code", "scene", "otp", "Expired challenge", challenge_id="old", phone_ref="phone", expires_at=time.time() - 1)

    async def test_missing_invalid_cancel_and_deadline_are_not_success(self):
        providers = [ScriptedProvider({}), ScriptedProvider({"phone": "invalid"}),
                     ScriptedProvider({"phone": ResourceCancelled("Cancelled")}), ScriptedProvider({"phone": "15500001111"}, delay=.2)]
        for index, provider in enumerate(providers):
            manager = self.manager(provider, run=str(index), wait_timeout=.03)
            with self.subTest(index=index), self.assertRaises(ResourceUnavailable):
                await manager.request("phone", "scene", "phone", "Test")
            self.assertIn(manager.events[-1]["status"], {"blocked", "cancelled"})

    async def test_pending_wait_is_cancelled_and_no_secret_is_persisted(self):
        manager = self.manager(ScriptedProvider({"phone": "15500001111"}, delay=5))
        task = asyncio.create_task(manager.request("phone", "scene", "phone", "Pause"))
        await asyncio.sleep(.03)
        self.assertEqual(json.loads(manager.audit_path.read_text())[-1]["status"], "waiting_user")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(json.loads(manager.audit_path.read_text())[-1]["status"], "cancelled")
        self.assertNotIn("15500001111", manager.audit_path.read_text())

    async def test_console_noninteractive_eof_and_cancel_are_bounded(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        with patch("sys.stdin", io.StringIO()), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ResourceUnavailable):
                await ConsoleProvider().acquire(request())
        with patch("sys.stdin", TTY()), patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(ResourceUnavailable):
                await ConsoleProvider().acquire(request())
        with patch("sys.stdin", TTY()), patch("builtins.input", return_value=":cancel"):
            with self.assertRaises(ResourceCancelled):
                await ConsoleProvider().acquire(request())

    async def test_console_clarification_cancel_releases_loop_without_executor_reader(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        started, release = threading.Event(), threading.Event()
        def pending_input(_):
            started.set()
            release.wait(2)
            return "unused"
        provider = ConsoleProvider()
        try:
            with patch("sys.stdin", TTY()), patch("builtins.input", side_effect=pending_input):
                task = asyncio.create_task(ConsoleAnswers(provider).aanswer([SimpleNamespace(id="quantity", text="Required quantity")]))
                for _ in range(20):
                    if started.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(started.is_set())
                readers = [t for t in threading.enumerate() if t.name == "single-console-resource"]
                self.assertTrue(readers and all(t.daemon for t in readers))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, .5)
                self.assertTrue(provider.closed)
        finally:
            release.set()

    async def test_sensitive_resource_metadata_cannot_enable_echo(self):
        provider = ScriptedProvider({"phone": "15500001111"})
        manager = ResourceManager([ResourceRef(id="phone", kind="phone", sensitive=False, run_id="per-execution")],
                                  "run", provider, ActiveClock(), self.root)
        await manager.request("phone", "scene", "phone", "Phone input")
        self.assertTrue(provider.requests[0]["sensitive"])

    async def test_replay_proxy_authenticates_and_has_no_child_stdin(self):
        provider = ScriptedProvider({"phone": "15500001111"})
        async with ResourceBroker(provider, "launch", specs()) as broker:
            with patch.dict(os.environ, broker.environment):
                self.assertEqual(await ReplayProvider().acquire(request()), "15500001111")
            host, port = broker.environment["E2E_RESOURCE_BRIDGE_ADDRESS"].rsplit(":", 1)
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write((json.dumps({"token": "wrong", "launch_id": "launch", "request": request().audit()}) + "\n").encode())
            await writer.drain()
            self.assertEqual(json.loads(await reader.readline())["status"], "blocked")
            writer.close()
            await writer.wait_closed()
            self.assertEqual(len(provider.requests), 1)
            self.assertNotIn("15500001111", json.dumps(broker.redactor.data(broker.events)))


class EvidencePrivacyTests(unittest.TestCase):
    def test_process_ownership_excludes_older_child_of_reused_parent_pid(self):
        from scripts.verify_workflow_compat import descendants, windows_process_tree
        tree = windows_process_tree([
            {"ProcessId": 100, "ParentProcessId": 1, "Created": 1000},
            {"ProcessId": 101, "ParentProcessId": 100, "Created": 1001},
            {"ProcessId": 50, "ParentProcessId": 100, "Created": 10},
            {"ProcessId": 51, "ParentProcessId": 50, "Created": 11}])
        self.assertEqual(descendants(tree, 100), {100, 101})
        self.assertEqual(descendants(tree, 50), {50, 51})

    def test_user_cancellation_remains_cancelled_with_blocked_dependents(self):
        self.assertEqual(aggregate(["cancelled", "blocked", "blocked"]), "cancelled")

    def test_redaction_covers_nested_keys_values_urls_and_private_delivery(self):
        secret = "Secret P@ss/哨兵"
        redactor = Redactor()
        redactor.register(secret)
        data = redactor.data({secret: {"url": "https://test/?v=" + quote_plus(secret), "message": secret}})
        self.assertNotIn(secret, json.dumps(data, ensure_ascii=False))
        self.assertNotIn(quote_plus(secret), json.dumps(data))
        delivery = request(delivery_hint="987654")
        self.assertNotIn("987654", repr(delivery))
        self.assertNotIn("987654", json.dumps(delivery.audit()))

    def test_blocked_junit_cannot_pass_and_is_not_a_product_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            path.write_text('<testsuite><testcase name="test_x"><properties><property name="scenario_id" value="x"/><property name="e2e_run_id" value="run"/><property name="execution_status" value="blocked"/></properties><failure/></testcase></testsuite>')
            result = read_result(path, 1, expected_ids={"test_x": "x"}, expected_run_id="run")
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocked, 1)
