from __future__ import annotations

import hashlib
import json
import time
import uuid
from operator import add
from typing import Annotated, Any, TypedDict

from ..schemas import ApprovalDecisionContract


STATE_SCHEMA_VERSION = 2
EventList = Annotated[list[dict[str, Any]], add]
ToolTraceList = Annotated[list[dict[str, Any]], add]
EffectLogList = Annotated[list[dict[str, Any]], add]


class ApprovalState(TypedDict, total=False):
    approvalId: str
    taskId: str
    status: str
    action: str
    request: dict[str, Any]
    decision: dict[str, Any]
    patchDigest: str
    repositoryBaselineDigest: str
    submittedAt: str
    approvedAt: str | None
    rejectionReason: str | None


class DurableTaskState(TypedDict, total=False):
    state_schema_version: int
    events: EventList
    tool_trace: ToolTraceList
    effect_log: EffectLogList
    approval: ApprovalState
    approval_request: dict[str, Any]
    approval_decision: dict[str, Any]
    patch_digest: str
    repository_baseline_digest: str


def digest_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def task_event(task_id: str, stage: str, kind: str, summary: str, *, attempt: int = 1,
               status: str = "EMITTED", artifact_refs: list[str] | None = None,
               event_id: str | None = None, run_id: str = "", subgraph: str = "",
               node: str = "", tool_call_id: str = "", blocked_reason: str = "") -> dict[str, Any]:
    timestamp = int(time.time() * 1000)
    return {
        "eventId": event_id or f"event-{uuid.uuid4()}",
        "taskId": task_id,
        "stage": stage,
        "kind": kind,
        "attempt": attempt,
        "timestamp": timestamp,
        "status": status,
        "summary": str(summary or "")[:1200],
        "artifactRefs": list(artifact_refs or [])[:50],
        "runId": run_id,
        "subgraph": subgraph,
        "node": node or stage,
        "toolCallId": tool_call_id,
        "blockedReason": blocked_reason,
    }


def tool_trace(task_id: str, stage: str, tool_call_id: str, summary: str, *, attempt: int = 1,
               status: str = "SUCCESS", run_id: str = "", subgraph: str = "",
               node: str = "") -> dict[str, Any]:
    return {
        "toolCallId": tool_call_id,
        "taskId": task_id,
        "stage": stage,
        "attempt": attempt,
        "timestamp": int(time.time() * 1000),
        "status": status,
        "summary": str(summary or "")[:1200],
        "runId": run_id,
        "subgraph": subgraph,
        "node": node or stage,
    }


def approval_contract(value: ApprovalDecisionContract | dict[str, Any] | bool, *, reason: str = "") -> ApprovalDecisionContract:
    if isinstance(value, ApprovalDecisionContract):
        return value
    if isinstance(value, bool):
        return ApprovalDecisionContract(approved=value,
                                        action="APPROVE_DELIVERY" if value else "REJECT", reason=reason)
    return ApprovalDecisionContract.model_validate(value)
