"""R2 acceptance against the public production executor and its actual graph trace."""
import argparse
import asyncio
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_agent.config import Settings
from e2e_agent.coordinator import execute_suite
from e2e_agent.domain import Action, Suite
from e2e_agent.evidence import condition_fingerprint
from e2e_agent.resources import Redactor
from e2e_agent.suite_demo import shop_suite

INPUT_VERSION = 1
CASES = {
    "graph-business-chain": "passed",
    "graph-checkpoint-dependency-blocks": "failed",
    "graph-scene-dependency-blocks": "failed",
    "graph-action-budget-stops": "error",
}
NODES = {
    "select_checkpoint", "observe_page", "plan_actions", "execute_action", "await_resource",
    "verify_checkpoint", "diagnose_failure", "plan_repair", "validate_repair", "complete_checkpoint",
}
STATE_FIELDS = {
    "execution_protocol_version", "run_id", "suite", "effective_suite", "effective_actions",
    "requirements_fingerprint", "scene_id",
    "checkpoint_id", "scene_results", "checkpoint_results", "checkpoint_statuses",
    "checkpoint_outcome", "action_queue", "action_index", "action_count", "budget",
    "pending_resource", "next_step", "graph_event_index",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_without_actions(data):
    protected = deepcopy(data)
    for scene in protected["scenarios"]:
        scene.pop("actions", None)
    return protected


def assert_requirements_preserved(suite, result):
    assert result["requirements_fingerprint"] == suite.requirements_fingerprint(), "Frozen requirement fingerprint changed"
    redactor = Redactor()
    for value in ("stage_user", "TestPass123!", "Wrong123!"):
        redactor.register(value)
    expected = redactor.data(suite.model_dump(mode="json"))
    assert project_without_actions(result["sanitized_plan"]) == project_without_actions(expected), "Frozen plan fields changed"
    for scene in suite.scenarios:
        actual_scene = result["scenarios"][scene.id]
        by_id = {item["id"]: item for item in actual_scene["checkpoints"]}
        assert set(by_id) == {checkpoint.id for checkpoint in scene.checkpoints}, "Checkpoint was dropped or invented"
        for checkpoint in scene.checkpoints:
            actual = by_id[checkpoint.id]
            original = {condition_fingerprint(condition) for condition in checkpoint.acceptance}
            observed = {item["condition_fingerprint"] for item in actual.get("evidence", [])}
            assert observed <= original, "An executed condition differs from its original acceptance"
            if actual["status"] == "passed":
                assert observed == original, "A passing checkpoint omitted an original condition"


def read_actual_graph(directory, result):
    paths = {"state": directory / "execution-state.json", "events": directory / "execution-events.json",
             "history": directory / "execution-state-history.jsonl", "events_jsonl": directory / "execution-events.jsonl"}
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    events = json.loads(paths["events"].read_text(encoding="utf-8"))
    history = [json.loads(line) for line in paths["history"].read_text(encoding="utf-8").splitlines() if line.strip()]
    event_lines = [json.loads(line) for line in paths["events_jsonl"].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events and events == event_lines and len(events) == len(history), "Actual node/state event collection is incomplete"
    assert state == history[-1], "Latest state differs from the actual last node state"
    assert STATE_FIELDS <= state.keys(), "Execution state lacks explicit business progress"
    assert state["run_id"] == result["run_id"], "State belongs to a different production execution"
    assert state["requirements_fingerprint"] == result["requirements_fingerprint"]
    assert state["action_count"] == result["action_count"] <= state["budget"]["max_actions"]
    assert state["next_step"] == "done" and state["execution_status"] == result["status"], "Graph did not reach a completed execution result"
    assert set(state["scene_results"]) == set(result["scenarios"]), "Graph did not own the final scene result set"
    indexes = [event["graph_event_index"] for event in events]
    assert indexes == list(range(1, len(indexes) + 1)), "Graph event sequence is discontinuous or incomplete"
    assert [item["action_count"] for item in history] == sorted(item["action_count"] for item in history), "Action progress moved backwards"
    for event, actual in zip(events, history, strict=True):
        assert event["node"] in NODES, "Unknown production graph node"
        assert STATE_FIELDS <= actual.keys(), "Intermediate actual graph state has no explicit cursors"
        assert event["graph_event_index"] == actual["graph_event_index"]
        assert actual["run_id"] == result["run_id"]
        assert actual["requirements_fingerprint"] == result["requirements_fingerprint"], "A graph transition changed frozen requirements"
        assert 0 <= actual["action_count"] <= state["budget"]["max_actions"], "An intermediate node exceeded the original action budget"
        for key in ("scene_id", "checkpoint_id", "action_index", "action_count", "next_step"):
            assert event[key] == actual[key], f"Event {key} differs from actual state"
        assert {"run_id", "action_id", "context_id", "execution_status", "state_file"} <= event.keys()
        assert event["run_id"] == result["run_id"], "Node event belongs to another run"
        assert Path(event["state_file"]).name == paths["state"].name
        # Real files, not a manually assembled test state, must remain JSON serializable.
        json.dumps(actual, ensure_ascii=False, allow_nan=False)
    for value in ("stage_user", "TestPass123!", "Wrong123!"):
        assert value not in paths["state"].read_text(encoding="utf-8"), "Resource value leaked into graph state"
        assert value not in paths["history"].read_text(encoding="utf-8"), "Resource value leaked into graph history"
    return state, events, {name: {"path": str(path), "sha256": digest(path)} for name, path in paths.items()}


def verify_normal_progress(suite, result, events):
    actions = [event for event in events if event["node"] == "execute_action"]
    expected_actions = [(scene.id, action.checkpoint_id, action.id) for scene in suite.scenarios for action in scene.actions]
    assert [(event["scene_id"], event["checkpoint_id"], event["action_id"]) for event in actions] == expected_actions, "Graph nodes did not execute one original action per visit"
    assert [event["action_count"] for event in actions] == list(range(1, len(actions) + 1)), "Global action cursor did not advance once per step"
    grouped = defaultdict(list)
    for event in actions:
        grouped[(event["scene_id"], event["checkpoint_id"])].append(event)
    contexts = set()
    for scene in suite.scenarios:
        record = result["scenarios"][scene.id]
        assert record["contexts_created"] == 1
        contexts.add(record["context_id"])
        assert {checkpoint["context_id"] for checkpoint in record["checkpoints"]} == {record["context_id"]}
        for checkpoint in scene.checkpoints:
            steps = grouped[(scene.id, checkpoint.id)]
            assert [event["action_index"] for event in steps] == list(range(1, len(steps) + 1))
            assert {event["context_id"] for event in steps} == {record["context_id"]}
    assert len(contexts) == len(suite.scenarios), "Independent scenes shared a browser context"
    assert result["scenarios"]["main-chain"]["stats"] == {"login_count": 1, "send_count": 0}
    completed = [event for event in events if event["node"] == "complete_checkpoint"]
    assert len(completed) == sum(len(scene.checkpoints) for scene in suite.scenarios)
    node_order = [event["node"] for event in events]
    assert all(node_order[index + 1] == "select_checkpoint" for index, node in enumerate(node_order[:-1]) if node == "complete_checkpoint"), "Checkpoint completion did not return control to graph selection"
    assert node_order.count("select_checkpoint") > len(completed), "Scene/checkpoint progression remained hidden inside one node"
    assert node_order.count("verify_checkpoint") == len(completed)


def budget_suite():
    return Suite.model_validate({
        "id": "graph-budget", "query": "Observe two dependent anonymous cart checkpoints with a one-action budget",
        "fixture": "local_shop", "budget": {"max_actions": 1},
        "rules": [{"id": identity, "statement": "Anonymous access must show a login message",
                   "source": {"kind": "contract", "reference": "sample_shop.CONTRACT.v1"}} for identity in ("first", "second")],
        "scenarios": [{"id": "budget-scene", "namespace": "budget", "description": "Budget stops before the second navigation",
                       "start_url": "{base}/shop/{namespace}", "actions": [],
                       "checkpoints": [{"id": identity, "description": identity, "depends_on": [] if identity == "first" else ["first"],
                           "acceptance": [{"rule_id": identity, "target": "testid:message", "operator": "equals", "expected": "Log in to view the cart"}]}
                           for identity in ("first", "second")]}],
    })


def initial_report():
    return {"schema_version": 1, "input_version": INPUT_VERSION, "stage": "R2", "status": "running",
            "full_stage_specialty": True, "model_calls": 0, "native_human_resume_verified": False,
            "separated_repair_roles_verified": False, "scenarios": []}


async def verify(output, report=None):
    settings = Settings.load()
    report = initial_report() if report is None else report
    for identity, expected in CASES.items():
        report["current_case"] = identity
        directory = output / identity
        data = shop_suite().model_dump()
        if identity == "graph-scene-dependency-blocks":
            data["scenarios"][1]["depends_on"] = ["main-chain"]
        suite = budget_suite() if identity == "graph-action-budget-stops" else Suite.model_validate(data)
        frozen = suite.model_dump_json()
        kwargs = {}
        if identity in {"graph-checkpoint-dependency-blocks", "graph-scene-dependency-blocks"}:
            kwargs["faults"] = {"main-chain": ["valid_login_denied"]}
        planned = []
        if identity == "graph-action-budget-stops":
            async def plan(scene, checkpoint, observation, *, remaining, redact):
                planned.append({"checkpoint": checkpoint.id, "remaining": remaining})
                return [Action(id="navigate-" + checkpoint.id, checkpoint_id=checkpoint.id,
                               kind="navigate", value="cart", intent="Observe anonymous cart")]
            kwargs["action_planner"] = plan
        result = await execute_suite(suite, directory, settings.browsers_path, **kwargs)
        assert result["status"] == expected, f"{identity}: expected {expected}, got {result['status']}"
        assert suite.model_dump_json() == frozen, "Original frozen Suite was mutated"
        assert_requirements_preserved(suite, result)
        state, events, evidence = read_actual_graph(directory, result)
        if identity == "graph-business-chain":
            verify_normal_progress(suite, result, events)
        elif identity in {"graph-checkpoint-dependency-blocks", "graph-scene-dependency-blocks"}:
            main = result["scenarios"]["main-chain"]
            checkpoints = {item["id"]: item for item in main["checkpoints"]}
            assert checkpoints["login"]["status"] == "failed"
            assert all(checkpoints[name]["status"] == "blocked" for name in ("cart", "refresh"))
            assert not any(event["scene_id"] == "main-chain" and event["checkpoint_id"] in {"cart", "refresh"} and event["node"] == "execute_action" for event in events)
            assert sum(event["node"] == "execute_action" and event["action_id"] == "submit-login" and event["scene_id"] == "main-chain" for event in events) == 1
            if identity == "graph-scene-dependency-blocks":
                blocked = result["scenarios"]["wrong-password"]
                assert blocked["status"] == "blocked" and blocked["blocked_by"] == "main-chain"
                assert not any(event["scene_id"] == "wrong-password" and event["node"] == "execute_action" for event in events)
                assert not blocked["events"]
            else:
                assert result["scenarios"]["wrong-password"]["status"] == "passed"
            assert result["scenarios"]["unauthenticated"]["status"] == "passed"
        else:
            assert planned and planned[0] == {"checkpoint": "first", "remaining": 1}
            assert planned[1:] in ([], [{"checkpoint": "second", "remaining": 0}]), "Planning continued beyond the original action budget"
            assert result["action_count"] == 1
            actions = [event for event in events if event["node"] == "execute_action"]
            assert len(actions) == 1 and actions[0]["action_id"] == "navigate-first"
            checkpoints = {item["id"]: item for item in result["scenarios"]["budget-scene"]["checkpoints"]}
            assert checkpoints["first"]["status"] == "passed" and checkpoints["second"]["status"] == "error"
            assert "budget" in checkpoints["second"].get("error", "").lower()
        report["scenarios"].append({"id": identity, "expected": expected, "actual": result["status"], "status": "passed",
                                    "architecture_verified": True, "requirements_preserved": True,
                                    "event_count": len(events), "action_count": state["action_count"],
                                    "requirements_fingerprint": suite.requirements_fingerprint(), "evidence": evidence,
                                    "result_path": str(directory / "suite-result.json")})
        (output / "execution-graph-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report.pop("current_case", None)
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    report = initial_report()
    try:
        report = asyncio.run(verify(args.output, report))
    except Exception as error:
        report.update(status="failed", error=Settings.load().redact(f"{type(error).__name__}: {error}"))
    path = args.output / "execution-graph-validation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
