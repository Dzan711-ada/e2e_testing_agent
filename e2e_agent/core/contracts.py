"""Open business vocabulary; fixed schemas describe tool capabilities, not sites."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ResourceSpec(Record):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_-]{0,79}$')
    label: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=600)
    sensitive: bool = True
    reusable: bool = True
    max_length: int = Field(default=4096, ge=1, le=65536)


class Requirement(Record):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source: str = Field(min_length=1, description='user or answer:<id>; never infer correctness from current output')


class ResourceExpectation(Record):
    resource_id: str = Field(min_length=1)


class Assertion(Record):
    id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    target: str = Field(min_length=1, description='Business description of the thing to check, not a guessed selector')
    operator: Literal['visible', 'hidden', 'text', 'contains', 'value', 'count', 'url', 'checked', 'enabled']
    expected: str | int | bool | ResourceExpectation = Field(description='Literal expected result, or {"resource_id":"declared-id"}. Visibility/check/enabled requires a boolean.')

    @model_validator(mode='before')
    @classmethod
    def read_initial_exports(cls, value):
        # Preserve this refactor's early debug artifacts; the public schema has one expected field.
        if isinstance(value, dict) and 'expected_resource_id' in value:
            value = dict(value)
            ref = value.pop('expected_resource_id')
            if ref:
                if value.get('expected') is not None:
                    raise ValueError('Two expected values are not allowed')
                value['expected'] = {'resource_id': ref}
        return value

    @property
    def expected_resource_id(self):
        return self.expected.resource_id if isinstance(self.expected, ResourceExpectation) else None

    @model_validator(mode='after')
    def valid_expected(self):
        if self.expected_resource_id and self.operator not in {'value', 'text', 'contains'}:
            raise ValueError('Only string comparisons support resource expectations')
        if self.operator in {'visible', 'hidden', 'checked', 'enabled'} and type(self.expected) is not bool:
            raise ValueError('This assertion requires a boolean expected value')
        if self.operator == 'count' and (type(self.expected) is not int or self.expected < 0):
            raise ValueError('Count requires a nonnegative integer')
        return self


class Checkpoint(Record):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1, description='Stable business outcome, not a list of tool actions; stop before leaving the state its assertions need')
    assertions: tuple[Assertion, ...] = Field(min_length=1)


class Scenario(Record):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    checkpoints: tuple[Checkpoint, ...] = Field(min_length=1)


class Plan(Record):
    version: int = Field(default=1, ge=1)
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    scenarios: tuple[Scenario, ...] = Field(min_length=1)
    resources: tuple[ResourceSpec, ...] = ()

    @model_validator(mode='after')
    def coherent(self):
        def unique(items):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError('Duplicate identity')
        for items in (self.requirements, self.scenarios, self.resources):
            unique(items)
        requirements = {r.id for r in self.requirements}
        covered = set()
        resource_ids = {r.id for r in self.resources}
        for scenario in self.scenarios:
            unique(scenario.checkpoints)
            for checkpoint in scenario.checkpoints:
                unique(checkpoint.assertions)
                for assertion in checkpoint.assertions:
                    if assertion.requirement_id not in requirements:
                        raise ValueError('Assertion cites an unknown requirement')
                    covered.add(assertion.requirement_id)
                    if assertion.expected_resource_id and assertion.expected_resource_id not in resource_ids:
                        raise ValueError('Assertion cites an undeclared resource')
        if covered != requirements:
            raise ValueError('Every required result needs an assertion; unreferenced requirement IDs: ' + ', '.join(sorted(requirements - covered))[:300])
        return self


class Question(Record):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Action(Record):
    kind: Literal['navigate', 'click', 'fill', 'select', 'check', 'uncheck', 'press', 'wait']
    intent: str = Field(min_length=1)
    target: str | None = Field(default=None, description='Observed node reference for interaction; MUST be null for navigate')
    value: str | None = Field(default=None, description='For navigate: observed URL; press: key; fill/select: public literal if no resource_id. Null otherwise.')
    resource_id: str | None = Field(default=None, description='Declared resource reference for fill/select only; MUST be null for navigate/click/wait/press/check/uncheck')

    @model_validator(mode='after')
    def valid_arguments(self):
        if self.kind == 'navigate':
            if not self.value or self.target or self.resource_id:
                raise ValueError('navigate requires value=observed URL, target=null, resource_id=null')
        elif not self.target:
            raise ValueError('An observed target reference is required')
        if self.kind in {'fill', 'select'}:
            if (self.value is None) == (self.resource_id is None):
                raise ValueError('Supply exactly one literal or resource reference')
        elif self.resource_id:
            raise ValueError('Only input tools accept resource references')
        if self.kind == 'press' and not self.value:
            raise ValueError('Press needs a key')
        return self
