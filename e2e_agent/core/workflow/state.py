"""Serializable LangGraph state; live handles and resource values stay outside it."""
from typing import TypedDict


class RunState(TypedDict, total=False):
    goal: str
    target_url: str
    phase: str
    status: str
    observations: list
    observation: dict
    answers: dict
    clarifications: list
    decision: dict
    plan: dict | None
    scene_index: int
    checkpoint_index: int
    records: list
    pending: dict | None
    current_action: dict | None
    failure: dict | None
    assessments: list
    revisions: list
    model_decisions: int
    actions: int
    questions: int
    retries: int
    error: str | None
    generation_status: str
    complete_script: bool
    export_dir: str
    replay_result: dict
    inconsistent: bool
    repaired: bool
