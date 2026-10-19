"""The actual parent embeds the actual production child; only browser I/O is stubbed."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from e2e_agent import agent_workflow as workflow
from e2e_agent.agent_workflow import AgentGraphHost, ScriptedAnswers, build_agent_graph, initial_agent_state, run_agent_suite
from e2e_agent.config import Settings
from e2e_agent.domain import Action, Suite
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from e2e_agent.models import ReplayResult
from e2e_agent.resources import ScriptedProvider
from e2e_agent.resume import read_saved_plan, restart_suite
from scripts.verify_graph_resources import private_scan_saver
from test_execution_graph import DeterministicServices, tiny_suite


class FrozenTransport:
    limit = 20
    def __init__(self):
        self.calls = []
    async def call(self, *args, **kwargs):
        raise AssertionError("An offline frozen plan unexpectedly called a model")


def fixture_suite(*, phone=False, empty=False):
    data = json.loads(tiny_suite().model_dump_json())
    scene = data["scenarios"][0]
    data["scenarios"] = [scene]
    data["query"] = "Verify sms readiness" if phone else "Verify readiness"
    for rule in data["rules"]:
        rule["source"]["reference"] = "contract:shop-v1"
    if phone:
        scene["actions"].insert(1, {"id": "request-phone", "checkpoint_id": "second", "kind": "request_resource",
                                   "resource_id": "phone", "intent": "Acquire fictional test phone"})
        data["resources"] = [{"id": "phone", "kind": "phone", "run_id": "per-execution", "reusable": True}]
    if empty:
        scene["checkpoints"].append({"id": "third", "description": "third", "depends_on": ["second"],
            "acceptance": [{"rule_id": "second", "target": "testid:ready", "operator": "equals", "expected": "ready"}]})
        scene["actions"] = []
    data["budget"]["max_actions"] = 4
    return Suite.model_validate(data)


def sources(suite):
    return {"user:request": {"kind": "user", "text": suite.query},
            "contract:shop-v1": {"kind": "contract", "text": "Explicit readiness fixture contract"}}


def child_pending(snapshot):
    for task in snapshot.tasks:
        if hasattr(task.state, "values") and task.state.values.get("pending_resource"):
            return task.state
    return None


def resource_io():
    original = workflow.RunRuntime.execute_one
    async def execute(runtime, action, check_conditions, *, pending_resource=None):
        if action.kind == "request_resource":
            await runtime.manager.request(action.resource_id, runtime.session.scene_id, action.id, action.intent)
        return await original(runtime, action, check_conditions)
    async def metadata(runtime, action):
        return {}
    return patch.object(workflow.RunRuntime, "execute_one", execute), patch.object(workflow.RunRuntime, "resource_request_metadata", metadata)


class NativeAgentGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_parent_child_namespace_resumes_without_repeating_completed_actions(self):
        self.assertTrue(asyncio.get_running_loop().get_debug())
        suite, services, model = fixture_suite(phone=True), DeterministicServices(), FrozenTransport()
        provider = ScriptedProvider({"phone": "15500007777"})
        with tempfile.TemporaryDirectory() as folder, services.patched():
            settings, directory = replace(Settings(), root=Path(folder)), Path(folder) / "native"
            host = AgentGraphHost(settings, model, (None, None), directory, provider)
            first_patch, second_patch = resource_io()
            with first_patch, second_patch, patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
                 patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="passed", exit_code=0))):
                state = initial_agent_state(suite.query, host=host, frozen_suite=suite, confirmed_sources=sources(suite))
                bridge = AsyncNodeBridge()
                graph = build_agent_graph(settings, model, ScriptedAnswers([]), (None, None), directory, provider, host=host, bridge=bridge)
                session = AsyncGraphSession(graph, bridge)
                config = {"configurable": {"thread_id": host.run_id}, "recursion_limit": 200}
                try:
                    await session.ainvoke(state, config)
                    paused = await session.aget_state(config, subgraphs=True)
                    child = child_pending(paused)
                    self.assertIs(type(graph.checkpointer), MemorySaver)
                    self.assertEqual(paused.next, ("execute",))
                    self.assertEqual(child.next, ("await_resource",))
                    self.assertIn("execute", child.config["configurable"]["checkpoint_ns"])
                    self.assertIn(child.config["configurable"]["checkpoint_ns"], graph.checkpointer.storage[host.run_id])
                    self.assertEqual(child.values["action_count"], 2)
                    self.assertEqual(child.values["pending_resource"]["ref"], "phone")
                    self.assertFalse(provider.requests)
                    context = child.values["context_id"]
                    host.record_checkpoint(paused)
                    result = await session.ainvoke(None, config)
                    self.assertEqual(result["status"], "passed", result.get("error"))
                    self.assertEqual(result["execution"]["action_count"], 3)
                    self.assertEqual(result["execution"]["scenarios"]["alpha"]["context_id"], context)
                    self.assertEqual(len(provider.requests), 1)
                    self.assertEqual([call[-1] for call in services.calls if call[0] == "action"],
                                     ["observe-first", "request-phone", "observe-second"])
                    self.assertFalse((await session.aget_state(config)).next)
                    self.assertEqual(private_scan_saver(graph.checkpointer, ["15500007777"])["plaintext_resource_leaks"], 0)
                    self.assertEqual(session.active_workers, 0)
                finally:
                    await host.close(clear_resources=False)
                    host.clear_resources()
        self.assertIsNone(host.runtime.session)
        self.assertFalse(host.runtime.manager.redactor._values)

    async def test_replay_only_failure_diagnoses_the_actual_new_replay_record_and_sources(self):
        suite, services, model = fixture_suite(), DeterministicServices(), FrozenTransport()
        captured = []
        async def replay(settings, directory, **kwargs):
            record = json.loads((directory / "generation/suite-result.json").read_text(encoding="utf-8"))
            record.update(run_id="actual-replay-failed", status="error")
            record["scenarios"]["alpha"]["status"] = "error"
            record["scenarios"]["alpha"]["events"][0]["target"] = "testid:replay-only-stale"
            replay_dir = directory / "replay-suite"
            replay_dir.mkdir()
            (replay_dir / "suite-result.json").write_text(json.dumps(record), encoding="utf-8")
            (replay_dir / "execution-state.json").write_text(json.dumps({"run_id": record["run_id"]}), encoding="utf-8")
            return ReplayResult(status="error", exit_code=1)
        async def diagnose(settings, planned, execution, directory, *, transport):
            captured.append(deepcopy(execution))
            return {"status": "completed", "execution_status": execution["status"]}
        with tempfile.TemporaryDirectory() as folder, services.patched(), \
             patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
             patch.object(workflow, "run_replay", replay), patch("e2e_agent.diagnostics.diagnose_execution", diagnose):
            directory = Path(folder) / "run"
            result = await run_agent_suite(replace(Settings(), root=Path(folder)), query=suite.query, run_dir=directory,
                frozen_suite=suite, confirmed_sources=sources(suite), transport=model)
            self.assertEqual(result["execution"]["status"], "passed")
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["stability"], "inconsistent")
            self.assertEqual(result["diagnostic_run_id"], "actual-replay-failed")
            self.assertEqual(result["diagnostics"]["origin"], "replay")
            self.assertEqual(captured[0]["run_id"], "actual-replay-failed")
            self.assertEqual(captured[0]["status"], "error")
            self.assertEqual(captured[0]["source_catalog"], sources(suite))
            self.assertEqual(captured[0]["scenarios"]["alpha"]["events"][0]["target"], "testid:replay-only-stale")
            self.assertEqual(Suite.model_validate(result["suite"]).requirements_fingerprint(), suite.requirements_fingerprint())
            nodes = [event["node"] for event in json.loads((directory / "parent-events.json").read_text(encoding="utf-8"))]
            self.assertEqual(nodes, ["observe", "execute", "export", "replay", "analyze", "final_diagnose"])
        await self._assert_parent_export_removes_successful_repair_wait_from_original_plan()

    async def _assert_parent_export_removes_successful_repair_wait_from_original_plan(self):
        data = fixture_suite().model_dump()
        data["budget"]["max_actions"] = 5
        data["scenarios"][0]["actions"] = [{"id": "retry-control", "checkpoint_id": "first", "kind": "click",
            "target": "testid:transient-control", "intent": "Retry the original transient control"},
            data["scenarios"][0]["actions"][1]]
        suite, services = Suite.model_validate(data), DeterministicServices()
        class WaitTransport(FrozenTransport):
            async def call(self, role, schema, system, payload):
                self.calls.append({"role": role, "real": False})
                if role == "diagnose":
                    decisive = next(item["id"] for item in payload["evidence"] if item["kind"] == "action")
                    return schema.model_validate({"diagnosis": {"attribution": "agent_execution", "evidence_ids": [decisive],
                        "explanation": "The recorded control failed before dispatch and may be retried after a bounded wait"}})
                original = Action.model_validate(payload["original_action"])
                wait = Action(id="repair-bounded-wait", checkpoint_id=original.checkpoint_id, kind="wait",
                              target=original.target, intent="Bounded wait for the original control")
                return schema.model_validate({"scenario_id": payload["scenario_id"], "action_id": original.id,
                    "restart_checkpoint": payload["checkpoint_id"], "attempt": payload["attempt"],
                    "requirements_fingerprint": payload["requirements_fingerprint"],
                    "protected_data_fingerprint": payload["protected_data_fingerprint"],
                    "evidence_ids": [payload["evidence"]["evidence"][0]["id"]],
                    "validation_conditions": payload["required_validation_conditions"],
                    "proposed_actions": [wait.model_dump(), original.model_dump()]})
        model = WaitTransport()
        with tempfile.TemporaryDirectory() as folder, services.patched():
            execute = workflow.RunRuntime.execute_one
            failed = False
            async def transient(runtime, action, conditions):
                nonlocal failed
                if action.id == "retry-control" and not failed:
                    failed = True
                    runtime.session.executor.events.append({"action_id": action.id, "checkpoint_id": action.checkpoint_id,
                        "kind": action.kind, "target": action.target, "status": "error", "side_effect_attempted": False})
                    raise ValueError("Transient control was unavailable before dispatch")
                return await execute(runtime, action, conditions)
            with patch.object(workflow.RunRuntime, "execute_one", transient), \
                 patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
                 patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="passed", exit_code=0))):
                directory = Path(folder) / "repaired"
                result = await run_agent_suite(replace(Settings(), root=Path(folder)), query=suite.query,
                    run_dir=directory, frozen_suite=suite, confirmed_sources=sources(suite), transport=model, repair=True)
            self.assertEqual(result["status"], "passed", result.get("error"))
            self.assertTrue(result["repaired"])
            self.assertEqual(result["repair_count"], 1)
            history = result["execution"]["repair_history"][0]
            self.assertEqual((history["decision"], history["execution_status"]), ("approved", "passed"))
            original = Suite.model_validate_json((directory / "original-plan.json").read_text(encoding="utf-8"))
            effective = Suite.model_validate_json((directory / "suite.json").read_text(encoding="utf-8"))
            self.assertEqual(original.model_dump(), suite.model_dump())
            self.assertEqual([action.id for action in effective.scenarios[0].actions],
                             ["repair-bounded-wait", "retry-control", "observe-second"])
            self.assertEqual(result["execution"]["action_count"], 4)
            self.assertEqual(original.requirements_fingerprint(), effective.requirements_fingerprint())
            self.assertEqual([call["role"] for call in model.calls], ["diagnose", "repair"])

    async def test_cancelled_later_resource_wait_preserves_generated_batches_and_restart_only_plans_missing(self):
        suite, services = fixture_suite(phone=True, empty=True), DeterministicServices()
        entered, cleaned, sessions = asyncio.Event(), asyncio.Event(), []
        class BlockingProvider:
            async def acquire(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
        planned = []
        async def planner(executor, *, suite, scene, checkpoint, **kwargs):
            planned.append(checkpoint.id)
            items = [{"id": "observe-" + checkpoint.id, "checkpoint_id": checkpoint.id,
                      "kind": "observe", "intent": "Observe readiness"}]
            if checkpoint.id == "second":
                items.insert(0, {"id": "request-phone", "checkpoint_id": "second", "kind": "request_resource",
                                 "resource_id": "phone", "intent": "Acquire fictional test phone"})
            return [Action.model_validate(item) for item in items]
        def make_session(graph, bridge):
            session = AsyncGraphSession(graph, bridge)
            sessions.append(session)
            return session
        with tempfile.TemporaryDirectory() as folder, services.patched():
            settings, directory = replace(Settings(), root=Path(folder)), Path(folder) / "cancel"
            first_patch, second_patch = resource_io()
            with first_patch, second_patch, patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
                 patch.object(workflow.ExecutionAgent, "plan_checkpoint", planner), \
                 patch.object(workflow, "AsyncGraphSession", make_session), \
                 patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="passed", exit_code=0))):
                task = asyncio.create_task(run_agent_suite(settings, query=suite.query, run_dir=directory,
                    frozen_suite=suite, confirmed_answers={"confirmed": "ready"}, confirmed_sources=sources(suite),
                    transport=FrozenTransport(), resource_provider=BlockingProvider()))
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                self.assertTrue(cleaned.is_set())
                self.assertEqual(sessions[0].active_workers, 0)
                canonical = read_saved_plan(directory)
                self.assertEqual([action.checkpoint_id for action in canonical.scenarios[0].actions],
                                 ["first", "second", "second"])
                artifact = json.loads((directory / "partial-plan.json").read_text(encoding="utf-8"))
                self.assertEqual(artifact["answers"], {"confirmed": "ready"})
                self.assertEqual(artifact["confirmed_sources"], sources(suite))
                self.assertEqual(canonical.requirements_fingerprint(), suite.requirements_fingerprint())
                checkpoint = json.loads((directory / "agent-checkpoint.json").read_text(encoding="utf-8"))
                self.assertTrue(checkpoint["tasks"][0]["state"]["values"]["pending_resource"])
                self.assertEqual(planned, ["first", "second"])
                planned.clear()
                result = await restart_suite(settings, directory, run_dir=Path(folder) / "restart",
                    resource_provider=ScriptedProvider({"phone": "15500008888"}), transport=FrozenTransport())
                self.assertEqual(result["status"], "passed", result.get("error"))
                self.assertEqual(planned, ["third"])
                self.assertEqual(result["missing_batches_at_restart"], [("alpha", "third")])
                self.assertEqual(result["execution"]["action_count"], 4)
                self.assertEqual(result["answers"], {"confirmed": "ready"})
                self.assertFalse(result["browser_state_restored"])
                self.assertFalse(result["old_resources_restored"])
                self.assertNotEqual(result["execution"]["run_id"], checkpoint["tasks"][0]["state"]["values"]["run_id"])
                self.assertTrue(all(session.active_workers == 0 for session in sessions))

    async def test_cancel_after_successful_repair_preserves_effective_plan_for_fresh_restart(self):
        data = json.loads(fixture_suite().model_dump_json())
        scene = data["scenarios"][0]
        scene["checkpoints"][0]["id"] = "login"
        scene["checkpoints"][1]["depends_on"] = ["login"]
        scene["actions"][0] = {"id": "submit-login", "checkpoint_id": "login", "kind": "click",
            "target": "testid:login-old", "intent": "submit-login"}
        data["resources"] = [{"id": "password", "kind": "password", "acquisition": "runtime",
                              "run_id": "per-execution", "reusable": True}]
        suite, services = Suite.model_validate(data), DeterministicServices()
        entered, cleaned = asyncio.Event(), asyncio.Event()
        sessions, hosts, attempted = [], [], []
        first_verified, cancel_at_next_observe = False, True

        class RepairTransport(FrozenTransport):
            async def call(self, role, schema, system, payload):
                self.calls.append({"role": role, "real": False})
                if role == "diagnose":
                    decisive = next(item["id"] for item in payload["evidence"] if item["kind"] == "action")
                    return schema.model_validate({"diagnosis": {"attribution": "agent_execution", "evidence_ids": [decisive],
                        "explanation": "The original locator failed before dispatch; its unique equivalent is observed"}})
                original = Action.model_validate(payload["original_action"])
                return schema.model_validate({"scenario_id": payload["scenario_id"], "action_id": original.id,
                    "restart_checkpoint": payload["checkpoint_id"], "attempt": payload["attempt"],
                    "requirements_fingerprint": payload["requirements_fingerprint"],
                    "protected_data_fingerprint": payload["protected_data_fingerprint"],
                    "evidence_ids": [payload["evidence"]["evidence"][0]["id"]],
                    "validation_conditions": payload["required_validation_conditions"],
                    "proposed_actions": [original.model_copy(update={"target": "testid:login-current"}).model_dump()]})

        model, old_provider = RepairTransport(), ScriptedProvider({"password": "PrivateOldRepair9Z6"})
        original_host = workflow.AgentGraphHost
        def make_host(*args, **kwargs):
            host = original_host(*args, **kwargs)
            hosts.append(host)
            return host
        def make_session(graph, bridge):
            session = AsyncGraphSession(graph, bridge)
            sessions.append(session)
            return session

        with tempfile.TemporaryDirectory() as folder, services.patched():
            execute, check = workflow.RunRuntime.execute_one, workflow.RunRuntime.check_conditions
            async def action_io(runtime, action, conditions):
                attempted.append((runtime.session.context_id, action.target))
                if action.target == "testid:login-old":
                    runtime.session.executor.events.append({"action_id": action.id, "checkpoint_id": action.checkpoint_id,
                        "kind": action.kind, "target": action.target, "status": "error", "side_effect_attempted": False})
                    raise ValueError("Original locator is absent before dispatch")
                return await execute(runtime, action, conditions)
            async def verify(runtime, checkpoint, outcome):
                nonlocal first_verified
                await check(runtime, checkpoint, outcome)
                if checkpoint.id == "login":
                    first_verified = True
            async def observe(runtime):
                if first_verified and cancel_at_next_observe:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()
                return {"url": runtime.session.page.url, "candidates": {
                    "testid:login-current": [{"tag": "button", "text": "Log in", "form_testid": "login-form"}],
                    "testid:ready": [{"tag": "div", "text": "ready"}]}}
            settings, directory = replace(Settings(), root=Path(folder)), Path(folder) / "cancel-after-repair"
            with patch.object(workflow.RunRuntime, "execute_one", action_io), \
                 patch.object(workflow.RunRuntime, "check_conditions", verify), \
                 patch.object(workflow.RunRuntime, "observe", observe), \
                 patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
                 patch.object(workflow, "AgentGraphHost", make_host), \
                 patch.object(workflow, "AsyncGraphSession", make_session), \
                 patch("e2e_agent.coordinator.AsyncGraphSession", make_session), \
                 patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="passed", exit_code=0))):
                task = asyncio.create_task(run_agent_suite(settings, query=suite.query, run_dir=directory,
                    frozen_suite=suite, confirmed_sources=sources(suite), transport=model,
                    resource_provider=old_provider, repair=True))
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                self.assertTrue(cleaned.is_set())
                self.assertEqual([item["role"] for item in model.calls], ["diagnose", "repair"])
                self.assertTrue(all(session.active_workers == 0 for session in sessions))
                self.assertIsNone(hosts[0].runtime.session)
                self.assertFalse(hosts[0].runtime.manager._cache)
                canonical = read_saved_plan(directory)
                self.assertEqual(canonical.scenarios[0].actions[0].target, "testid:login-current")
                planned = json.loads((directory / "action-plan-alpha-login.json").read_text(encoding="utf-8"))
                self.assertEqual(planned["actions"][0]["target"], "testid:login-current")
                self.assertEqual(canonical.requirements_fingerprint(), suite.requirements_fingerprint())
                old_context = attempted[-1][0]
                cancel_at_next_observe, first_verified = False, False
                attempted.clear()
                fresh_provider = ScriptedProvider({"password": "PrivateNewRepair9Z6"})
                restarted = await restart_suite(settings, directory, run_dir=Path(folder) / "fresh",
                    resource_provider=fresh_provider, transport=FrozenTransport())
                self.assertEqual(restarted["status"], "passed", restarted.get("error"))
                self.assertFalse(restarted["browser_state_restored"])
                self.assertFalse(restarted["old_resources_restored"])
                self.assertEqual([target for _, target in attempted], ["testid:login-current", None])
                self.assertTrue(all(context != old_context for context, _ in attempted))
                self.assertEqual(len(old_provider.requests), 1)
                self.assertEqual(len(fresh_provider.requests), 1)
                self.assertNotEqual(old_provider.requests[0]["run_id"], fresh_provider.requests[0]["run_id"])
                self.assertTrue(all(session.active_workers == 0 for session in sessions))

    async def test_final_diagnosis_cannot_exceed_the_original_shared_model_budget(self):
        suite, services, model = fixture_suite(), DeterministicServices(failure=("alpha", "first")), FrozenTransport()
        model.calls = [{"role": "execute"}] * suite.budget.max_model_calls
        with tempfile.TemporaryDirectory() as folder, services.patched(), \
             patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
             patch.object(workflow, "run_replay", AsyncMock(return_value=ReplayResult(status="failed", exit_code=1))), \
             patch("e2e_agent.diagnostics.diagnose_execution", new_callable=AsyncMock) as diagnose:
            result = await run_agent_suite(replace(Settings(), root=Path(folder)), query=suite.query,
                run_dir=Path(folder) / "run", frozen_suite=suite, confirmed_sources=sources(suite), transport=model)
            self.assertEqual(result["diagnostics"]["status"], "blocked", result.get("error"))
            self.assertEqual(result["diagnostics"]["attribution"], "unknown")
            self.assertEqual(len(model.calls), suite.budget.max_model_calls)
            diagnose.assert_not_called()

    async def test_replay_failure_rejects_old_or_successful_structured_records(self):
        suite = fixture_suite()
        for mode in ("old", "passed", "exception"):
            with self.subTest(replay_mode=mode), tempfile.TemporaryDirectory() as folder:
                services, captured = DeterministicServices(), []
                directory = Path(folder) / "native"
                model, settings = FrozenTransport(), replace(Settings(), root=Path(folder))
                host = AgentGraphHost(settings, model, (None, None), directory, ScriptedProvider({}))
                prior = {"run_id": "old-failed-run", "status": "error", "scenarios": {},
                         "requirements_fingerprint": suite.requirements_fingerprint()}
                replay_dir = directory / "replay-suite"
                replay_dir.mkdir()
                (replay_dir / "suite-result.json").write_text(json.dumps(prior), encoding="utf-8")
                (replay_dir / "execution-state.json").write_text(json.dumps({"run_id": prior["run_id"]}), encoding="utf-8")
                async def replay(*args, **kwargs):
                    if mode == "exception":
                        raise RuntimeError("Independent pytest transport failed")
                    if mode == "passed":
                        passed = {**prior, "run_id": "new-passed-run", "status": "passed"}
                        (replay_dir / "suite-result.json").write_text(json.dumps(passed), encoding="utf-8")
                        (replay_dir / "execution-state.json").write_text(json.dumps({"run_id": passed["run_id"]}), encoding="utf-8")
                    return ReplayResult(status="error", detail="Independent pytest transport failed")
                async def diagnose(settings, planned, execution, directory, **kwargs):
                    captured.append(deepcopy(execution))
                    return {"status": "completed", "execution_status": execution["status"], "attribution": "unknown"}
                with services.patched(), patch.object(workflow, "observe_shop", AsyncMock(return_value={})), \
                     patch.object(workflow, "run_replay", replay), patch("e2e_agent.diagnostics.diagnose_execution", diagnose):
                    initial = initial_agent_state(suite.query, host=host, frozen_suite=suite, confirmed_sources=sources(suite))
                    bridge = AsyncNodeBridge()
                    graph = build_agent_graph(settings, model, ScriptedAnswers([]), (None, None), directory,
                                              host.resource_provider, host=host, bridge=bridge)
                    try:
                        result = await AsyncGraphSession(graph, bridge).ainvoke(initial,
                            {"configurable": {"thread_id": host.run_id}, "recursion_limit": 200})
                        self.assertEqual(result["status"], "error")
                        self.assertIsNone(result["diagnostic_run_id"])
                        self.assertIsNone(captured[0]["run_id"])
                        self.assertEqual(captured[0]["scenarios"], {})
                        self.assertIn("no verified current structured failure", captured[0]["error"])
                        if mode == "exception":
                            self.assertTrue(captured[0]["replay_detail"])
                            self.assertEqual(result["phase"], "complete")
                            self.assertEqual(result["diagnostic_origin"], "replay")
                        else:
                            self.assertEqual(captured[0]["replay_detail"], "Independent pytest transport failed")
                    finally:
                        await host.close(clear_resources=False)
                        host.clear_resources()

    async def test_sensitive_frozen_action_is_rejected_before_browser_and_first_native_checkpoint(self):
        data = fixture_suite().model_dump()
        data["resources"] = [{"id": "password", "kind": "password", "run_id": "per-execution"}]
        data["scenarios"][0]["actions"] = [{"id": "unsafe", "checkpoint_id": "first", "kind": "fill",
            "field_role": "password", "target": "label:Password", "value": "PrivateLiteralSentinel!91", "intent": "Fill password"}]
        suite = Suite.model_validate(data)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(workflow.RunRuntime, "open", new_callable=AsyncMock) as opened, \
             patch.object(workflow, "MemorySaver", wraps=MemorySaver) as saver, \
             patch.object(workflow, "observe_shop", new_callable=AsyncMock) as observed:
            directory = Path(folder) / "run"
            result = await run_agent_suite(replace(Settings(), root=Path(folder)), query=suite.query,
                run_dir=directory, frozen_suite=suite, confirmed_sources=sources(suite), transport=FrozenTransport())
            self.assertEqual(result["status"], "error")
            opened.assert_not_called()
            observed.assert_not_called()
            saver.assert_not_called()
            self.assertNotIn("PrivateLiteralSentinel!91", (directory / "agent-result.json").read_text(encoding="utf-8"))
