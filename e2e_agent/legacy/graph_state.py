"""JSON-only execution progress; live objects and resource values stay outside it."""
from graphlib import TopologicalSorter
from copy import deepcopy
from typing import TypedDict

from .action_policy import (public_action_ids, public_action_locators,
                            redact_action_record, validate_graph_actions, redact_role_payload)


class ExecutionState(TypedDict, total=False):
    execution_protocol_version: int
    run_id: str
    suite: dict
    effective_suite: dict
    effective_actions: dict[str, list[dict]]
    public_ids: list[str]
    public_locators: list[str]
    requirements_fingerprint: str
    scene_order: list[str]
    checkpoint_order: dict[str, list[str]]
    scene_id: str | None
    checkpoint_id: str | None
    context_id: str | None
    scene_results: dict
    checkpoint_results: list
    checkpoint_statuses: dict
    checkpoint_outcome: dict
    action_queue: list[dict]
    action_index: int
    current_action: dict | None
    last_action_id: str | None
    planned_checkpoints: list[list[str]]
    action_count: int
    budget: dict
    pending_resource: dict | None
    observation: dict
    failure: dict | None
    failure_execution: dict | None
    repair_queue: list[dict]
    repair_index: int
    repair_active: bool
    repair_original: dict | None
    repair_bundle: dict | None
    repair_assessment: dict | None
    repair_proposal: dict | None
    repair_attempt: int
    scene_repair_attempt: int
    repair_history: list
    repair_attempts: dict
    scene_repair_attempts: dict
    repaired: bool
    initial_failures: list
    next_step: str
    execution_status: str
    execution: dict | None
    graph_event_index: int


def dependency_order(items):
    """Compute static stable dependency order, without performing execution."""
    return list(TopologicalSorter({item.id: item.depends_on for item in items}).static_order())


_PUBLIC_RECORD_FIELDS = frozenset({
    "id", "ref", "kind", "status", "execution_status", "decision", "operator", "category", "type",
    "run_id", "suite_id", "scene_id", "scenario_id", "checkpoint_id", "action_id",
    "context_id", "namespace", "resource_id", "rule_id", "field_role", "target",
    "binding_id", "challenge_id", "phone_ref", "blocked_by", "attribution",
    "condition_fingerprint", "requirements_fingerprint", "protected_data_fingerprint",
    "evidence_ids", "counter_evidence_ids", "validation_conditions", "planned_scenarios",
    "restart_checkpoint", "account_ref", "password_ref", "method", "resource_type",
})
_ACTION_RECORD_FIELDS = frozenset({"action", "original_action", "proposed_actions"})
_DISPLAY_RECORD_FIELDS = frozenset({"observation", "source_catalog", "sanitized_plan", "assessment"})


def redact_execution_record(value, redact, *, public_locators=(), public_ids=()):
    """Display/model views retain trusted protocol and mask free text."""
    return redact_role_payload(value, redact, public_ids=public_ids, public_locators=public_locators)


def redact_execution_state(state, redact):
    display = {"suite", "effective_suite", "observation"}
    records = {"scene_results", "checkpoint_results", "checkpoint_outcome", "failure",
               "failure_execution", "repair_history", "initial_failures", "execution",
               "repair_bundle", "repair_assessment", "repair_proposal"}
    return {key: (redact_role_payload(value, redact, public_ids=state.get("public_ids", ()),
                                       public_locators=state.get("public_locators", ())) if key in display else
                  redact_execution_record(value, redact, public_locators=state.get("public_locators", ()),
                                          public_ids=state.get("public_ids", ())) if key in records else deepcopy(value))
            for key, value in state.items()}


def initial_execution_state(suite, run_id, *, redact=None):
    """Initial logical state. Apply the runtime redactor BEFORE checkpointing.

    suite/effective_suite are display views and action carriers after redaction.
    Executable requirements are always recovered from immutable runtime.suite.
    """
    check = redact or (lambda value: value)
    # Reject private literal arguments before even the initial input can enter
    # a native MemorySaver checkpoint. No rejected candidate is returned/logged.
    for scene in suite.scenarios:
        validate_graph_actions(suite, scene.actions, redact=check)
    state = {
        "execution_protocol_version": 1, "run_id": run_id,
        "suite": suite.model_dump(), "effective_suite": suite.model_dump(),
        "effective_actions": {scene.id: [action.model_dump() for action in scene.actions] for scene in suite.scenarios},
        "public_ids": sorted(public_action_ids(suite)), "public_locators": sorted(public_action_locators(suite)),
        "requirements_fingerprint": suite.requirements_fingerprint(),
        "scene_order": dependency_order(suite.scenarios),
        "checkpoint_order": {scene.id: dependency_order(scene.checkpoints) for scene in suite.scenarios},
        "scene_id": None, "checkpoint_id": None, "context_id": None,
        "scene_results": {}, "checkpoint_results": [], "checkpoint_statuses": {},
        "checkpoint_outcome": {}, "action_queue": [], "action_index": 0,
        "current_action": None, "last_action_id": None, "planned_checkpoints": [],
        "action_count": 0, "budget": suite.budget.model_dump(),
        "pending_resource": None, "observation": {}, "failure": None,
        "failure_execution": None, "repair_queue": [], "repair_index": 0,
        "repair_active": False, "repair_original": None, "repair_history": [],
        "repair_bundle": None, "repair_assessment": None, "repair_proposal": None,
        "repair_attempt": 0, "scene_repair_attempt": 0,
        "repair_attempts": {}, "scene_repair_attempts": {}, "repaired": False,
        "initial_failures": [], "next_step": "select_checkpoint",
        "execution_status": "running", "execution": None, "graph_event_index": 0,
    }
    # A caller-supplied structural redactor must retain public IDs and action
    # enums. Never apply Redactor.data indiscriminately to control state.
    return redact_execution_state(state, redact) if redact is not None else state
