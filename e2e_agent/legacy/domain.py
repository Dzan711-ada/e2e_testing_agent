"""Versioned domain records for the new suite path; legacy models stay unchanged."""
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal["pending", "running", "waiting_user", "passed", "failed", "error", "blocked", "cancelled"]
Attribution = Literal["suspected_product", "agent_execution", "environment", "requirements", "unknown"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Record):
    kind: Literal["user", "clarification", "contract"]
    reference: str = Field(min_length=1)

    @field_validator("reference")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("A rule needs an explicit source")
        return value.strip()


class Rule(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    statement: str = Field(min_length=1)
    source: Source
    required: bool = True


class ResourceRef(Record):
    id: str = Field(min_length=1)
    kind: Literal["account", "password", "phone", "sms_code"]
    sensitive: bool = True
    reusable: bool = False
    run_id: str = Field(min_length=1)
    challenge_id: str | None = None
    acquisition: Literal["fixture", "runtime"] = "fixture"


class Acceptance(Record):
    rule_id: str
    target: str = Field(min_length=1)
    operator: Literal["visible", "hidden", "equals", "count", "url"]
    expected: str | int | bool

    @model_validator(mode="after")
    def consistent_expected_type(self):
        if self.operator in {"visible", "hidden"} and self.expected is not True:
            raise ValueError("Visibility checks require expected=true; use hidden for absence")
        if self.operator == "count" and (type(self.expected) is not int or self.expected < 0):
            raise ValueError("Count requires a non-negative integer")
        if self.operator == "url" and (not isinstance(self.expected, str) or not self.expected.strip()):
            raise ValueError("URL acceptance requires an explicit expected URL")
        return self


class Checkpoint(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    acceptance: tuple[Acceptance, ...] = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class Action(Record):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9 _-]*$")
    kind: Literal["observe", "navigate", "fill", "click", "select", "check", "wait", "request_resource"]
    checkpoint_id: str
    intent: str = Field(min_length=1)
    target: str | None = None
    value: str | int | bool | None = None
    resource_id: str | None = None
    preconditions: tuple[str, ...] = ()
    postcondition: str | None = None
    field_role: Literal["username", "password", "email", "phone", "sms_code"] | None = None

    @model_validator(mode="after")
    def typed_arguments(self):
        if self.kind == "navigate" and (not isinstance(self.value, str) or not self.value.strip() or self.resource_id):
            raise ValueError("Navigation needs an explicit URL and cannot use a secret resource")
        if self.kind in {"fill", "select"}:
            if not self.target or (self.value is None) == (self.resource_id is None):
                raise ValueError("Fill/select needs a target and exactly one value or resource reference")
        if self.kind in {"click", "wait", "check"} and not self.target:
            raise ValueError("Action needs an observed target")
        if self.kind == "request_resource" and not self.resource_id:
            raise ValueError("Resource request needs a reference")
        return self


class Scenario(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    start_url: str
    namespace: str = Field(min_length=1)
    initial_state: Literal["empty", "registered_account"] = "empty"
    checkpoints: tuple[Checkpoint, ...] = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    actions: tuple[Action, ...] = ()

    @model_validator(mode="after")
    def valid_checkpoints(self):
        ids = [item.id for item in self.checkpoints]
        validate_dependencies({item.id: item.depends_on for item in self.checkpoints}, ids)
        if len({a.id for a in self.actions}) != len(self.actions):
            raise ValueError("Duplicate action identity")
        for action in self.actions:
            if action.checkpoint_id not in ids or set(action.preconditions) - set(ids):
                raise ValueError("Action has a dangling checkpoint reference")
            checkpoint = next(c for c in self.checkpoints if c.id == action.checkpoint_id)
            if set(action.preconditions) - set(checkpoint.depends_on):
                raise ValueError("Action prerequisites must be declared checkpoint dependencies")
        return self


def validate_dependencies(edges, identities):
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate identity")
    visiting, done = set(), set()

    def visit(identity):
        if identity in visiting:
            raise ValueError("Cyclic dependency")
        if identity not in edges:
            raise ValueError("Dangling dependency")
        if identity in done:
            return
        visiting.add(identity)
        for dependency in edges[identity]:
            visit(dependency)
        visiting.remove(identity)
        done.add(identity)
    for identity in identities:
        visit(identity)


class Budget(Record):
    max_actions: int = Field(default=40, ge=1, le=200)
    max_model_calls: int = Field(default=20, ge=1, le=100)
    max_seconds: int = Field(default=180, ge=1, le=1800)
    max_repairs_per_failure: int = Field(default=2, ge=0, le=2)
    max_repairs_per_scenario: int = Field(default=4, ge=0, le=8)


class Suite(Record):
    schema_version: Literal[2] = 2
    id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    query: str = Field(min_length=1)
    rules: tuple[Rule, ...] = Field(min_length=1)
    scenarios: tuple[Scenario, ...] = Field(min_length=1)
    resources: tuple[ResourceRef, ...] = ()
    fixture: Literal["local_shop"] | None = None
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def consistent_plan(self):
        rule_ids = {r.id for r in self.rules}
        if len(rule_ids) != len(self.rules):
            raise ValueError("Duplicate rule identity")
        identities = [s.id for s in self.scenarios]
        validate_dependencies({s.id: s.depends_on for s in self.scenarios}, identities)
        resource_ids = {r.id for r in self.resources}
        if len(resource_ids) != len(self.resources):
            raise ValueError("Duplicate resource identity")
        covered = set()
        for scenario in self.scenarios:
            for checkpoint in scenario.checkpoints:
                for condition in checkpoint.acceptance:
                    if condition.rule_id not in rule_ids:
                        raise ValueError("Acceptance has no confirmed rule")
                    covered.add(condition.rule_id)
            for action in scenario.actions:
                if action.resource_id and action.resource_id not in resource_ids:
                    raise ValueError("Unknown resource reference")
        uncovered = {r.id for r in self.rules if r.required} - covered
        if uncovered:
            raise ValueError("Required rule has no acceptance check: " + ", ".join(sorted(uncovered)))
        if sum(len(s.actions) for s in self.scenarios) > self.budget.max_actions:
            raise ValueError("Plan exceeds action budget")
        return self

    def requirements_fingerprint(self):
        protected = {"version": self.version, "rules": [r.model_dump() for r in self.rules],
                     "fixture": self.fixture,
                     "scenarios": [{"id": s.id, "namespace": s.namespace, "initial_state": s.initial_state,
                                    "checkpoints": [c.model_dump() for c in s.checkpoints]} for s in self.scenarios]}
        return hashlib.sha256(json.dumps(protected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Evidence(Record):
    id: str
    scenario_id: str
    checkpoint_id: str
    kind: Literal["assertion", "observation", "error", "action", "screenshot"]
    expected: Any = None
    actual: Any = None
    path: str | None = None


class Diagnosis(Record):
    attribution: Attribution = "unknown"
    evidence_ids: tuple[str, ...]
    counter_evidence_ids: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    explanation: str


class RepairPlan(Record):
    scenario_id: str
    action_id: str
    requirements_fingerprint: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    proposed_actions: tuple[Action, ...] = Field(min_length=1)
    restart_checkpoint: str
    attempt: int = Field(ge=1, le=2)
    protected_data_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_conditions: tuple[str, ...] = Field(min_length=1, description="Copy the exact program-supplied required_validation_conditions check IDs; no prose or omissions")


class Outcome(Record):
    scenario_id: str
    checkpoint_id: str | None = None
    status: Status
    attribution: Attribution = "unknown"
    stability: Literal["consistent", "inconsistent", "unverified"] = "unverified"
    assisted: bool = False
    repaired: bool = False
    repair_count: int = Field(default=0, ge=0)
    evidence_ids: tuple[str, ...] = ()
    blocked_by: str | None = None


def load_record(data: dict):
    """Versionless historical run records stay readable; never reinterpret them as suites."""
    if "schema_version" not in data:
        if not {"actions", "events", "status"} <= data.keys():
            raise ValueError("Unrecognized legacy record")
        return dict(data)
    if data["schema_version"] == 2:
        return Suite.model_validate(data)
    raise ValueError("Unsupported schema_version")
