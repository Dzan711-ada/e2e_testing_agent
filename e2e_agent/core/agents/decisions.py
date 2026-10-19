"""Structured decisions exchanged by the three Agents."""
from typing import Literal
from pydantic import Field, model_validator
from ..contracts import Action, Plan, Question, Record, ResourceSpec


class DesignDecision(Record):
    status: Literal['inspect', 'clarify', 'ready', 'blocked']
    reason: str = Field(min_length=1)
    inspect_url: str | None = None
    questions: tuple[Question, ...] = ()
    plan: Plan | None = None

    @model_validator(mode='after')
    def complete(self):
        if self.status == 'inspect' and not self.inspect_url:
            raise ValueError('Inspection needs a URL')
        if self.status == 'clarify' and not self.questions:
            raise ValueError('Clarification needs questions')
        if self.status == 'ready' and self.plan is None:
            raise ValueError('A ready decision needs a plan')
        if self.status != 'ready' and self.plan is not None:
            raise ValueError('An unresolved decision must not contain an executable plan')
        return self


class Binding(Record):
    assertion_id: str
    target: str | None = None


class ExecutionDecision(Record):
    status: Literal['action', 'resource', 'verify', 'replan', 'blocked']
    reason: str = Field(min_length=1)
    action: Action | None = None
    resource: ResourceSpec | None = None
    bindings: tuple[Binding, ...] = ()

    @model_validator(mode='after')
    def complete(self):
        if self.status == 'action' and self.action is None:
            raise ValueError('Action decision needs an action')
        if self.status == 'resource' and self.resource is None:
            raise ValueError('Resource decision needs a description')
        if self.status == 'verify' and not self.bindings:
            raise ValueError('Verification needs all frozen assertion bindings')
        return self


class Assessment(Record):
    attribution: Literal['agent_execution', 'suspected_product', 'environment', 'requirements', 'unknown']
    reason: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    recommendation: Literal['retry', 'stop']
