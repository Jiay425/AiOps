from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now().isoformat()


class ApiResponse(BaseModel):
    code: str = "0000"
    info: str = "成功"
    data: Any = None


class IncidentAnalyzeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    service_name: str = Field(alias="serviceName", min_length=1)
    start_time: str = Field(alias="startTime", min_length=1)
    end_time: str = Field(alias="endTime", min_length=1)
    problem: str = Field(min_length=1, max_length=2000)
    trace_id: str | None = Field(default=None, alias="traceId")
    max_step: int = Field(default=6, alias="maxStep", ge=1, le=10)
    fixture_case_id: str = Field(default="", alias="fixtureCaseId")
    endpoint: str = ""

    @field_validator("service_name", "start_time", "end_time", "problem")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cannot be blank")
        return value


class Alert(BaseModel):
    status: str | None = None
    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str | None = None
    generatorURL: str | None = None


class AlertmanagerWebhook(BaseModel):
    version: str | None = None
    receiver: str | None = None
    status: str | None = None
    groupKey: str | None = None
    commonLabels: dict[str, str] | None = None
    commonAnnotations: dict[str, str] | None = None
    externalURL: str | None = None
    alerts: list[Alert] | None = None


class CodeOpsTaskRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    task_type: str = Field(alias="taskType", min_length=1)
    goal: str = Field(min_length=1, max_length=4000)
    repository: str | None = None
    change_ref: str | None = Field(default=None, alias="changeRef")
    focus_areas: list[str] | None = Field(default=None, alias="focusAreas")
    context: dict[str, Any] | None = None
    max_rounds: int | None = Field(default=None, alias="maxRounds", ge=1, le=12)
    max_tool_calls: int | None = Field(default=None, alias="maxToolCalls", ge=1, le=50)

    @field_validator("task_type", "goal")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cannot be blank")
        return value


class IncidentFixRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    service_name: str | None = Field(default=None, alias="serviceName")
    alert_rule: str | None = Field(default=None, alias="alertRule")
    severity: str | None = None
    problem: str | None = Field(default=None, max_length=4000)
    endpoint: str | None = None
    trace_id: str | None = Field(default=None, alias="traceId")
    start_time: str | None = Field(default=None, alias="startTime")
    end_time: str | None = Field(default=None, alias="endTime")
    repository: str | None = None
    change_ref: str | None = Field(default=None, alias="changeRef")
    allow_patch_apply: bool | None = Field(default=None, alias="allowPatchApply")
    allow_test_patch_apply: bool | None = Field(default=None, alias="allowTestPatchApply")
    fixture_fallback_allowed: bool | None = Field(default=None, alias="fixtureFallbackAllowed")
    focus_areas: list[str] | None = Field(default=None, alias="focusAreas")
    labels: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    max_rounds: int | None = Field(default=None, alias="maxRounds", ge=1, le=12)
    max_tool_calls: int | None = Field(default=None, alias="maxToolCalls", ge=1, le=50)

    @field_validator("problem")
    @classmethod
    def problem_non_blank(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            raise ValueError("cannot be blank")
        return value


class AgentLoopRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    goal: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    change_ref: str = Field(default="", alias="changeRef")
    focus_areas: list[str] = Field(default_factory=list, alias="focusAreas")
    context: dict[str, Any] = Field(default_factory=dict)
    max_turns: int | None = Field(default=None, alias="maxTurns")
    dry_run: bool | None = Field(default=None, alias="dryRun")
    include_steps: bool | None = Field(default=None, alias="includeSteps")

    @field_validator("goal", "repository")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cannot be blank")
        return value


class ApprovalDecision(BaseModel):
    reason: str = ""
