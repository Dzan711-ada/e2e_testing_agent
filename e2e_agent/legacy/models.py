"""Validated plans and execution results."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1)
    kind: Literal["action", "assertion"]

    @field_validator("description")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("动作描述不能为空。")
        return value.strip()


class TestPlan(BaseModel):
    __test__ = False
    actions: list[Action] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def ends_with_assertion(self):
        if self.actions[-1].kind != "assertion":
            raise ValueError("最后一步必须检查用户要求的结果。")
        return self


class BrowserResult(BaseModel):
    ok: bool
    html: str = ""
    url: str = ""
    error: str = ""
    error_type: str = ""
    screenshot: str = ""


class ReplayResult(BaseModel):
    status: Literal["passed", "failed", "error", "blocked", "cancelled"]
    exit_code: int | None = None
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    blocked: int = 0
    detail: str = ""
