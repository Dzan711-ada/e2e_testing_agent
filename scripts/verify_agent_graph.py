"""R4 production parent/child architecture and evidence reuse; fixed roles only."""
import argparse
import asyncio
import hashlib
import inspect
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent import agent_workflow
from e2e_agent.agent_workflow import AgentGraphHost, ScriptedAnswers, build_agent_graph, initial_agent_state, run_agent_suite
from e2e_agent.config import Settings
from e2e_agent.domain import Suite
from e2e_agent.execution_graph import recursion_limit
from e2e_agent.graph_checkpoint import AsyncGraphSession, AsyncNodeBridge, MemorySaver
from e2e_agent.replay import run_replay
from e2e_agent.sample_shop import CONTRACT, shop_server
from e2e_agent.sms_demo import sms_suite
from e2e_agent.suite_demo import shop_suite
from scripts.verify_diagnostics import FixedAssessment
from scripts.verify_graph_resources import private_scan_saver
from scripts.verify_resources import CapturingProvider, PHONE, scan

INPUT_VERSION = 1
CASES = (
    "parent-child-native-namespace", "parent-main-stage-paths", "repair-role-handoff",
    "repair-rejected-before-dispatch", "repair-budget-stops-in-graph",
    "repaired-export-independent", "replay-only-failure-diagnoses-current-run",
)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def reference(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def assert_subsequence(nodes, expected):
    position = iter(nodes)
    assert all(any(node == wanted for node in position) for wanted in expected), "Actual node path omitted or reordered a required stage"


def parent_events(directory):
    events = load(directory / "parent-events.json")
    lines = [json.loads(line) for line in (directory / "parent-events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events and events == lines, "Actual parent node trace is incomplete"
    assert all({"node", "run_id", "phase", "status"} <= event.keys() for event in events)
    assert len({event["run_id"] for event in events}) == 1, "Parent event sequence mixed separate runs"
    return events


def child_events(directory):
    events = load(directory / "execution-events.json")
    lines = [json.loads(line) for line in (directory / "execution-events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events and events == lines, "Actual child node trace is incomplete"
    assert [event["graph_event_index"] for event in events] == list(range(1, len(events) + 1))
    return events


def pending_child(snapshot):
    """Only inspect actual native StateSnapshot tasks; never synthesize child state."""
    for task in snapshot.tasks:
        child = task.state
        if hasattr(child, "values") and child.values.get("pending_resource"):
            return child
        if hasattr(child, "tasks"):
            found = pending_child(child)
            if found is not None:
                return found
    return None


def confirmed_sources(query):
    return {"user:request": {"kind": "user", "text": query},
            "contract:shop-v1": {"kind": "contract", "text": CONTRACT},
            "sample_shop.CONTRACT.v1": {"kind": "contract", "text": CONTRACT}}


def frozen_agent_fixture(data, max_actions):
    """Use the same confirmed sources/field semantics as existing Agent gates."""
    data["budget"]["max_actions"] = min(data["budget"]["max_actions"], max_actions)
    for rule in data["rules"]:
        rule["source"]["reference"] = "contract:shop-v1"
    for scene in data["scenarios"]:
        for action in scene["actions"]:
            if action["kind"] == "fill" and not action.get("field_role"):
                action["field_role"] = "username" if action["resource_id"] == "username" else "password"
    return Suite.model_validate(data)


class FrozenRoles:
    """Frozen complete plans need no planning service; diagnosis is an interface fixture."""
    def __init__(self):
        self.calls, self.payloads, self.limit = [], [], 20

    async def call(self, role, schema, system, payload):
        if role != "diagnose":
            raise AssertionError("A frozen complete plan unexpectedly requested a planning service")
        self.calls.append({"role": role, "agent_id": "diagnosis", "status": "passed", "real": False})
        self.payloads.append(payload)
        return await FixedAssessment("agent_execution").call(role, schema, system, payload)


async def native_parent_case(settings, directory, secrets):
    directory.mkdir(parents=True, exist_ok=False)
    suite = frozen_agent_fixture(sms_suite().model_dump(), settings.max_actions)
    sources = confirmed_sources(suite.query)
    provider = CapturingProvider(secrets, use_test_delivery=True)
    model = FrozenRoles()
    bridge = AsyncNodeBridge()
    with shop_server() as fixture:
        host = AgentGraphHost(settings, model, fixture, directory, provider, frozen_suite=suite, confirmed_sources=sources)
        mirror = initial_agent_state(suite.query, host=host, frozen_suite=suite, confirmed_sources=sources)
        graph = build_agent_graph(settings, model, ScriptedAnswers([]), fixture, directory, provider,
                                  bridge=bridge, host=host, interrupt_resources=True)
        saver = graph.checkpointer
        assert isinstance(saver, MemorySaver), "Parent did not use the production native saver"
        session = AsyncGraphSession(graph, bridge)
        config = {"configurable": {"thread_id": host.run_id}, "recursion_limit": recursion_limit(suite) + 24}
        async def drive(value):
            async for update in session.astream(value, config, stream_mode="updates"):
                for node, values in update.items():
                    mirror.update(values)
                    saved = host.record_parent_update(node, mirror)
                    if inspect.isawaitable(saved):
                        await saved
            snapshot = await session.aget_state(config, subgraphs=True)
            saved = host.record_checkpoint(snapshot)
            if inspect.isawaitable(saved):
                await saved
            return snapshot
        try:
            phone_root = await drive(mirror)
            phone = pending_child(phone_root)
            assert phone is not None and phone.next == ("await_resource",)
            assert phone.values["pending_resource"]["kind"] == "phone" and not provider.requests
            namespace = phone.config["configurable"]["checkpoint_ns"]
            assert namespace and "execute" in namespace, "Execution was not a native namespaced child"
            assert namespace in saver.storage[config["configurable"]["thread_id"]]
            context = phone.values["context_id"]
            generation_id = phone.values["run_id"]
            json.dumps(phone.values, ensure_ascii=False, allow_nan=False)
            otp_root = await drive(None)
            otp = pending_child(otp_root)
            assert otp is not None and otp.values["pending_resource"]["kind"] == "sms_code"
            assert otp.values["context_id"] == context and otp.values["run_id"] == generation_id
            challenge = otp.values["pending_resource"]["challenge_id"]
            secrets.append(host.runtime.store.test_code(challenge))
            assert host.runtime.store.stats(host.runtime.session.namespace) == {"send_count": 1, "login_count": 0}
            assert [request["kind"] for request in provider.requests] == ["phone"]
            private_scan_saver(saver, secrets)
            final = await drive(None)
            assert not final.next and pending_child(final) is None
            assert mirror["status"] == "passed" and mirror["complete_script"] and mirror["replay_result"]["status"] == "passed"
            assert [request["kind"] for request in provider.requests] == ["phone", "sms_code", "phone", "sms_code"]
            assert provider.requests[1]["challenge_id"] != provider.requests[3]["challenge_id"]
            first = mirror["execution"]["scenarios"]["sms-chain"]
            second = load(directory / "replay-suite/suite-result.json")
            assert first["context_id"] == context and first["contexts_created"] == 1
            assert first["stats"] == {"send_count": 1, "login_count": 1}
            assert second["run_id"] != generation_id and second["scenarios"]["sms-chain"]["stats"] == {"send_count": 1, "login_count": 1}
            assert not model.calls, "Frozen native/replay flow unexpectedly invoked a model fixture"
            assert_subsequence([event["node"] for event in parent_events(directory)], ["export", "replay", "analyze"])
            saved = private_scan_saver(saver, secrets)
            evidence = {"parent_run_id": host.run_id, "execution_run_id": generation_id, "checkpoint_namespace": namespace,
                        "context_id": context, "parent_checkpoint": reference(directory / "agent-checkpoint.json"),
                        "saver_privacy": saved, "replay_run_id": second["run_id"]}
        finally:
            await host.close(clear_resources=False)
            host.clear_resources()
    return evidence


async def replay_failure_case(settings, directory, secrets):
    suite = frozen_agent_fixture(shop_suite().model_dump(), settings.max_actions)
    model = FrozenRoles()
    original_replay = run_replay
    stale = "testid:replay-only-stale-login"
    async def fail_actual_replay(configured, run_dir, *, resource_provider=None):
        path = run_dir / "suite.json"
        exported = load(path)
        frozen = Suite.model_validate(exported).requirements_fingerprint()
        action = next(action for action in exported["scenarios"][0]["actions"] if action["id"] == "submit-login")
        action["target"] = stale
        assert Suite.model_validate(exported).requirements_fingerprint() == frozen == suite.requirements_fingerprint()
        (run_dir / "replay-fault-before.json").write_bytes(path.read_bytes())
        path.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
        return await original_replay(configured, run_dir, resource_provider=resource_provider)
    with patch.object(agent_workflow, "run_replay", fail_actual_replay):
        result = await run_agent_suite(settings, query=suite.query, run_dir=directory, transport=model,
            answers_provider=ScriptedAnswers([]), frozen_suite=suite, confirmed_sources=confirmed_sources(suite.query))
    original = result["execution"]
    replay = load(directory / "replay-suite/suite-result.json")
    assert original["status"] == "passed" and replay["status"] == "error"
    assert original["run_id"] != replay["run_id"]
    assert result["status"] == "error" and result["stability"] == "inconsistent"
    assert result["diagnostic_run_id"] == replay["run_id"], "Final diagnosis used the original successful run"
    diagnosis = result["diagnostics"]
    assert diagnosis["status"] == "completed" and diagnosis["execution_status"] == "error"
    actual_snapshot = load(Path(diagnosis["directory"]) / "execution-snapshot.json")
    assert actual_snapshot["run_id"] == replay["run_id"]
    assert model.payloads and all(payload["execution_status"] == "error" for payload in model.payloads)
    assert any(event.get("target") == stale for payload in model.payloads for event in payload["evidence"] if event["kind"] == "action")
    assert_subsequence([event["node"] for event in parent_events(directory)], ["export", "replay", "analyze", "final_diagnose"])
    scan(directory, secrets)
    return {"first_run_id": original["run_id"], "failed_replay_run_id": replay["run_id"],
            "diagnostic_run_id": result["diagnostic_run_id"], "parent_events": reference(directory / "parent-events.json")}


def initial_report():
    return {"schema_version": 1, "stage": "R4", "input_version": INPUT_VERSION, "status": "running",
            "full_stage_specialty": True, "real_model": False, "actual_console_input": False,
            "parent_child_native_verified": False, "separated_repair_roles_verified": False, "scenarios": []}


async def verify(args, report, secrets):
    settings = Settings.load()
    secrets.extend([PHONE, settings.deepseek_api_key, settings.qwen_api_key])
    def passed(identity, evidence):
        report["scenarios"].append({"id": identity, "expected": "passed", "actual": "passed", "status": "passed", "evidence": evidence})
        (args.output / "agent-graph-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["current_case"] = CASES[0]
    passed(CASES[0], await native_parent_case(settings, args.output / "native-parent", secrets))
    reports = []
    report["current_case"] = CASES[1]
    for folder in (args.agents_fixed, args.agents_real):
        report_path = folder / "agent-validation.json"
        source = load(report_path)
        assert source["status"] == "passed" and source["full_stage_specialty"]
        for identity in ("clear-goal", "required-clarification"):
            directory = folder / identity
            result = load(directory / "agent-result.json")
            assert result["status"] == "passed" and result["complete_script"]
            nodes = [event["node"] for event in parent_events(directory)]
            assert_subsequence(nodes, ["observe", "design", "export", "replay", "analyze"])
            if identity == "required-clarification":
                assert "clarify" in nodes
            assert child_events(directory / "generation")
        reports.append(reference(report_path))
    passed(CASES[1], {"reused_reports": reports})
    fixed_report = load(args.repairs_fixed / "repair-validation.json")
    real_report = load(args.repairs_real / "repair-validation.json")
    assert fixed_report["status"] == real_report["status"] == "passed"
    report["current_case"] = CASES[2]
    for folder in (args.repairs_fixed, args.repairs_real):
        events = child_events(folder / "stale-target/generation")
        assert_subsequence([event["node"] for event in events], ["diagnose_failure", "plan_repair", "validate_repair", "execute_action"])
        assert any(event["node"] == "validate_repair" and event["next_step"] == "execute_action" for event in events)
    real_stale = next(item for item in real_report["scenarios"] if item["id"] == "stale-target")
    repairs = [call for call in real_stale["model_calls"] if call["role"] == "repair"]
    assert repairs and all(call.get("agent_id") == "execution" for call in repairs), "Repair was not attributed to the Execution Agent"
    passed(CASES[2], {"fixed_trace": reference(args.repairs_fixed / "stale-target/generation/execution-events.json"),
                     "real_report": reference(args.repairs_real / "repair-validation.json")})
    report["current_case"] = CASES[3]
    rejected = child_events(args.repairs_fixed / "malicious-plan-rejected/generation")
    validators = [event for event in rejected if event["node"] == "validate_repair"]
    assert validators and all(event["next_step"] != "execute_action" for event in validators)
    rejected_result = load(args.repairs_fixed / "malicious-plan-rejected/generation/suite-result.json")
    assert rejected_result["status"] == "error" and not rejected_result["repaired"]
    passed(CASES[3], {"trace": reference(args.repairs_fixed / "malicious-plan-rejected/generation/execution-events.json")})
    report["current_case"] = CASES[4]
    budget = load(args.repairs_fixed / "repair-budget-stop/generation/suite-result.json")
    events = child_events(args.repairs_fixed / "repair-budget-stop/generation")
    assert budget["status"] == "error" and budget["repair_count"] == 2 and not budget["repaired"]
    assert "budget exhausted" in budget["repair_history"][-1]["reason"]
    assert any(event["node"] == "diagnose_failure" for event in events)
    assert events[-1]["next_step"] == "done"
    passed(CASES[4], {"trace": reference(args.repairs_fixed / "repair-budget-stop/generation/execution-events.json")})
    report["current_case"] = CASES[5]
    directory = args.repairs_fixed / "graph-repaired-export/run"
    result = load(directory / "agent-result.json")
    original = Suite.model_validate_json((directory / "original-plan.json").read_text(encoding="utf-8"))
    effective = Suite.model_validate_json((directory / "suite.json").read_text(encoding="utf-8"))
    replay = load(directory / "replay-suite/suite-result.json")
    assert original.requirements_fingerprint() == effective.requirements_fingerprint() == result["execution"]["requirements_fingerprint"]
    assert result["repaired"] and result["execution"]["initial_failures"] and result["stability"] == "inconsistent"
    assert replay["status"] == "passed" and replay["model_calls"] == 0
    assert_subsequence([event["node"] for event in parent_events(directory)], ["export", "replay", "analyze"])
    assert any(action.target == "testid:login-old" for action in original.scenarios[0].actions)
    assert all(action.target != "testid:login-old" for action in effective.scenarios[0].actions)
    passed(CASES[5], {"result": reference(directory / "agent-result.json"), "independent_replay": reference(directory / "replay-suite/suite-result.json")})
    report["current_case"] = CASES[6]
    passed(CASES[6], await replay_failure_case(settings, args.output / "replay-only-failure", secrets))
    scan(args.output, secrets)
    report.pop("current_case", None)
    report.update(status="passed", parent_child_native_verified=True, separated_repair_roles_verified=True, plaintext_resource_leaks=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "agents-fixed", "agents-real", "repairs-fixed", "repairs-real"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report, secrets = initial_report(), []
    try:
        asyncio.run(verify(args, report, secrets))
    except Exception as error:
        from e2e_agent.resources import Redactor
        redactor = Redactor()
        for value in secrets:
            redactor.register(value)
        report.update(status="failed", error=redactor.text(f"{type(error).__name__}: {error}"))
    (args.output / "agent-graph-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
