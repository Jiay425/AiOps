from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

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
    """Backward-compatible approval request accepted by the legacy endpoints."""

    model_config = ConfigDict(populate_by_name=True)

    approved: bool = True
    action: "ApprovalAction" = Field(default="APPROVE_DELIVERY")
    reason: str = ""
    operator_id: str = Field(default="anonymous", alias="operatorId")
    decision_id: str = Field(default_factory=lambda: f"decision-{uuid4()}", alias="decisionId")
    approval_id: str | None = Field(default=None, alias="approvalId")


class ApprovalAction(str, Enum):
    APPROVE_DELIVERY = "APPROVE_DELIVERY"
    APPROVE_APPLY_TO_WORKTREE = "APPROVE_APPLY_TO_WORKTREE"
    REJECT = "REJECT"


class ApprovalDecisionContract(BaseModel):
    """The JSON-only contract consumed by the LangGraph human approval node."""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    approved: bool = True
    action: ApprovalAction = ApprovalAction.APPROVE_DELIVERY
    reason: str = ""
    operator_id: str = Field(default="anonymous", alias="operatorId")
    decision_id: str = Field(default_factory=lambda: f"decision-{uuid4()}", alias="decisionId")
    approval_id: str | None = Field(default=None, alias="approvalId")

    @field_validator("reason", "operator_id", "decision_id")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("decision_id")
    @classmethod
    def require_decision_id(cls, value: str) -> str:
        if not value:
            raise ValueError("decisionId cannot be blank")
        return value


class RetryInstructionsContract(BaseModel):
    """Machine-readable feedback passed from the independent reviewer to Repair."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    failure_type: str = Field(default="REVIEW_RETRY", alias="failureType")
    must_fix: list[str] = Field(default_factory=list, alias="mustFix")
    must_avoid: list[str] = Field(default_factory=list, alias="mustAvoid")
    next_attempt_constraints: list[str] = Field(default_factory=list, alias="nextAttemptConstraints")
    previous_patch_digest: str = Field(default="", alias="previousPatchDigest")
    additional_evidence_required: list[str] = Field(default_factory=list, alias="additionalEvidenceRequired")

    @field_validator("failure_type", "previous_patch_digest")
    @classmethod
    def normalize_retry_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("must_fix", "must_avoid", "next_attempt_constraints", "additional_evidence_required")
    @classmethod
    def normalize_retry_lists(cls, value: list[Any]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


class ReleaseReviewContract(BaseModel):
    """Independent reviewer output; deterministic facts are checked by the subgraph."""

    model_config = ConfigDict(populate_by_name=True, use_enum_values=True, extra="ignore")

    review_verdict: Literal[
        "RELEASE_READY", "ACCEPT", "ACCEPT_WITH_HUMAN_REVIEW", "HUMAN_REVIEW",
        "RETRY_REPAIR", "REJECT", "NO_CODE_FIX", "REVIEW_UNAVAILABLE"
    ] = Field(default="REVIEW_UNAVAILABLE", alias="reviewVerdict")
    patch_decision: Literal[
        "RELEASE_READY", "ACCEPT", "HUMAN_REVIEW", "RETRY_REPAIR", "REJECT", "NO_CODE_FIX"
    ] = Field(default="HUMAN_REVIEW", alias="patchDecision")
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = Field(default="UNKNOWN", alias="riskLevel")
    root_cause_addressed: bool | None = Field(default=None, alias="rootCauseAddressed")
    scope_safe: bool | None = Field(default=None, alias="scopeSafe")
    test_sufficient: bool | None = Field(default=None, alias="testSufficient")
    retry_instructions: RetryInstructionsContract = Field(default_factory=RetryInstructionsContract,
                                                           alias="retryInstructions")
    must_review: list[str] = Field(default_factory=list, alias="mustReview")
    human_approval_points: list[str] = Field(default_factory=list, alias="humanApprovalPoints")
    review_findings: list[str] = Field(default_factory=list, alias="reviewFindings")
    business_risks: list[str] = Field(default_factory=list, alias="businessRisks")
    concurrency_risks: list[str] = Field(default_factory=list, alias="concurrencyRisks")
    reasoning: list[str] = Field(default_factory=list)
    quality_score: int = Field(default=0, ge=0, le=100, alias="qualityScore")

    @field_validator("must_review", "human_approval_points", "review_findings", "business_risks",
                     "concurrency_risks", "reasoning")
    @classmethod
    def normalize_review_lists(cls, value: list[Any]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


class EvidenceReviewContract(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: Literal["SUFFICIENT", "NEED_MORE_EVIDENCE", "LOCALIZATION_BLOCKED"] = "NEED_MORE_EVIDENCE"
    sufficient: bool = False
    confidence_score: int = Field(default=0, alias="confidenceScore", ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list, alias="missingEvidence")
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")
    root_cause: str = Field(default="", alias="rootCause")


class InvestigationDecisionContract(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: Literal["LOCALIZED", "NEED_MORE_EVIDENCE", "LOCALIZATION_BLOCKED"] = "NEED_MORE_EVIDENCE"
    target_files: list[str] = Field(default_factory=list, alias="targetFiles")
    target_methods: list[str] = Field(default_factory=list, alias="targetMethods")
    supporting_evidence: list[str] = Field(default_factory=list, alias="supportingEvidence")
    negative_evidence: list[str] = Field(default_factory=list, alias="negativeEvidence")
    missing_evidence: list[str] = Field(default_factory=list, alias="missingEvidence")
    scope_suggestion: str = Field(default="", alias="scopeSuggestion")
    should_enter_code_repair: bool = Field(default=False, alias="shouldEnterCodeRepair")


class PatchProposalContract(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    summary: str = ""
    rationale: str = ""
    root_cause: str = Field(default="", alias="rootCause")
    target_files: list[str] = Field(default_factory=list, alias="targetFiles")
    patch_digest: str = Field(default="", alias="patchDigest")
    scope_guard: dict[str, Any] = Field(default_factory=dict, alias="scopeGuard")
    patch_validation: dict[str, Any] = Field(default_factory=dict, alias="patchValidation")
    patch_diff_analysis: dict[str, Any] = Field(default_factory=dict, alias="patchDiffAnalysis")
    sandbox_result: dict[str, Any] = Field(default_factory=dict, alias="sandboxResult")
    test_proposal: list[str] = Field(default_factory=list, alias="testProposal")
