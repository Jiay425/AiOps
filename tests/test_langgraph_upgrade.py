import hashlib
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ops_autoagent import api
from ops_autoagent.codeops import AgentLoopService, EngineeringToolGateway, PatchProposal, RepositoryToolkit, ToolBudget
from ops_autoagent.codeops.runtime import FilePatch
from ops_autoagent.config import Settings
from ops_autoagent.graphs import CodeOpsGraph, OpsDiagnosisGraph
from ops_autoagent.graphs.state_models import digest_json
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.schemas import ApprovalAction, ApprovalDecisionContract, CodeOpsTaskRequest, IncidentAnalyzeRequest
from ops_autoagent.tools import ObservabilityTools


class InterruptProbeGraph(CodeOpsGraph):
    """Small deterministic graph fixture that reaches the production HITL nodes."""

    async def _plan(self, state):
        return {"plan": []}

    async def _orchestrate(self, state):
        raw = {
            "llmGenerated": True,
            "testExecutionResults": ["BUILD SUCCESS"],
            "riskLevel": "HIGH",
            "patchQuality": {"staticSafetyPassed": True},
            "patchSandbox": {"isolated": True},
        }
        return {
            "decision": {"decision": "STOP", "selectedSkill": "", "reason": "probe complete"},
            "stop_reason": "probe complete",
            "steps": [{"stepNo": 1, "selectedSkill": "test_verification", "status": "SUCCESS",
                        "rawEvidenceJson": json.dumps(raw)}],
            "patch_proposal": PatchProposal("probe", []).to_dict(),
        }


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        ops_database_path=tmp_path / "ops.db",
        ops_runbook_path=Path("docs/dev-ops/runbook"),
        openai_api_key="",
        prometheus_base_url="",
        ops_fixture_fallback=True,
        ops_agent_chat_enabled=False,
        ops_agent_planner_enabled=False,
        ops_agent_reviewer_enabled=False,
        **overrides,
    )


@pytest.mark.asyncio
async def test_interrupt_resume_uses_same_thread_and_default_delivery_only(tmp_path: Path):
    settings = _settings(tmp_path, codeops_apply_mode="delivery_only")
    graph = InterruptProbeGraph(OpenAICompatibleClient(settings))
    result = await graph.invoke(CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX", goal="probe approval", repository=str(tmp_path), maxRounds=1, maxToolCalls=1,
    ))

    assert result["status"] == "WAITING_APPROVAL"
    assert result["approval_request"]["allowedActions"] == ["APPROVE_DELIVERY", "REJECT"]
    assert result["__interrupt__"]
    checkpoint = await graph.checkpoint_summary(result["task"]["taskId"])
    assert checkpoint["threadId"] == result["task"]["taskId"]
    assert checkpoint["interruptPending"] is True

    decision = ApprovalDecisionContract(
        approved=True, action=ApprovalAction.APPROVE_DELIVERY, operatorId="operator-1",
        decisionId="decision-1", approvalId=result["approval"]["approvalId"], reason="deliver validated artifact",
    )
    resumed = await graph.resume(result["task"]["taskId"], decision)
    assert resumed["status"] == "COMPLETED"
    assert resumed["approval"]["status"] == "APPROVED"
    assert resumed["approval"]["decisionId"] == "decision-1"
    assert not resumed.get("__interrupt__")


@pytest.mark.asyncio
async def test_approval_api_resumes_graph_and_replays_same_decision_idempotently(tmp_path: Path, monkeypatch):
    old_path = api.store.path
    api.store.path = tmp_path / "api.db"
    await api.store.initialize()
    settings = _settings(tmp_path, codeops_apply_mode="delivery_only")
    monkeypatch.setattr(api, "codeops_graph", InterruptProbeGraph(OpenAICompatibleClient(settings), api.store))
    try:
        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
            created = await client.post("/api/v1/codeops/task/submit", json={
                "taskType": "INCIDENT_TO_FIX", "goal": "probe approval", "repository": str(tmp_path),
                "maxRounds": 1, "maxToolCalls": 1,
            })
            task_id = created.json()["data"]["taskId"]
            pending = await client.get(f"/api/v1/codeops/task/{task_id}/approval")
            approval_id = pending.json()["data"]["approvalId"]
            payload = {"approved": True, "action": "APPROVE_DELIVERY", "operatorId": "operator-1",
                       "decisionId": "decision-api-1", "approvalId": approval_id}
            first = await client.post(f"/api/v1/codeops/task/{task_id}/approval/approve", json=payload)
            replay = await client.post(f"/api/v1/codeops/task/{task_id}/approval/approve", json=payload)

        assert first.json()["data"]["status"] == "APPROVED"
        assert replay.json()["data"]["status"] == "APPROVED"
        assert replay.json()["info"] == "Approval decision already processed"
        assert len(await api.store.find("audit_logs", lambda item: item.get("decisionId") == "decision-api-1")) == 1
    finally:
        api.store.path = old_path


@pytest.mark.asyncio
async def test_generic_agent_loop_cannot_write_and_apply_effect_is_gated(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    service = AgentLoopService(EngineeringToolGateway())

    async def malicious_model(request, steps):
        return {"thoughtSummary": "try mutation", "toolCalls": [{
            "toolCallId": "mutation-1", "toolName": "repo.exact_replace",
            "arguments": {"repository": str(tmp_path), "filePath": "app.py", "oldText": "return 1", "newText": "return 2"},
        }]}

    result = await service.run({"repository": str(tmp_path), "goal": "inspect", "maxTurns": 2}, malicious_model)
    assert result["status"] == "DENIED"
    assert source.read_text(encoding="utf-8") == "def run():\n    return 1\n"
    assert "repo.exact_replace" not in {item["toolName"] for item in service.gateway.list_registered_tools(read_only=True)}

    settings = _settings(tmp_path, codeops_apply_mode="apply_to_worktree")
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    proposal = PatchProposal("validated", [FilePatch("app.py", "return 1", "return 2")])
    baseline = RepositoryToolkit(tmp_path, ToolBudget(100)).create_snapshot()
    updated = "def run():\n    return 2\n"
    state = {
        "task": {"taskId": "effect-1", "taskType": "INCIDENT_TO_FIX", "repository": str(tmp_path)},
        "context": {"repoBaselineSnapshot": baseline, "repairScope": {"scopeType": "FULL_FILE"}},
        "patch_proposal": proposal.to_dict(), "patch_digest": digest_json(proposal.to_dict()),
        "repository_baseline_digest": digest_json(baseline),
        "approval": {"approvalId": "approval-1", "patchDigest": digest_json(proposal.to_dict()),
                      "repositoryBaselineDigest": digest_json(baseline), "status": "PENDING"},
        "approval_decision": {"action": "APPROVE_APPLY_TO_WORKTREE", "decisionId": "decision-1", "operatorId": "op"},
        "sandbox_result": {"checksums": {"app.py": hashlib.sha256(updated.encode()).hexdigest()}},
        "steps": [{"selectedSkill": "test_verification", "status": "SUCCESS",
                    "rawEvidenceJson": json.dumps({"testExecutionResults": ["BUILD SUCCESS"]})}],
    }
    applied = await graph._apply_approved_patch(state)
    assert applied["status"] == "COMPLETED"
    assert source.read_text(encoding="utf-8") == updated
    assert applied["effect_log"][0]["status"] == "APPLIED"

    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    stale = {**state, "repository_baseline_digest": digest_json({"stale": True})}
    blocked = await graph._apply_approved_patch(stale)
    assert blocked["status"] == "STALE_APPROVAL"
    assert source.read_text(encoding="utf-8") == "def run():\n    return 1\n"


@pytest.mark.asyncio
async def test_ops_parallel_evidence_reserves_budget_and_reduces_events(tmp_path: Path):
    settings = _settings(tmp_path, ops_parallel_evidence_enabled=True, ops_agent_plan_driven_enabled=False)
    graph = OpsDiagnosisGraph(ObservabilityTools(settings), OpenAICompatibleClient(settings))
    result = await graph.invoke(IncidentAnalyzeRequest(
        serviceName="parallel-service", startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T00:10:00Z",
        problem="HTTP 500 and timeout", maxStep=4,
    ))
    assert result["status"] == "SUCCESS"
    assert {item["subType"] for item in result["events"]} >= {
        "evidence_budget_reserved", "evidence_barrier", "prometheus", "elk", "skywalking",
    }
    assert len(result["tool_trace"]) >= 3


@pytest.mark.asyncio
async def test_codeops_event_sse_replays_from_last_event_id(tmp_path: Path):
    old_path = api.store.path
    api.store.path = tmp_path / "events.db"
    await api.store.initialize()
    try:
        events = [
            {"eventId": "event-1", "taskId": "task-1", "stage": "plan", "kind": "started", "attempt": 1,
             "timestamp": 1, "status": "EMITTED", "summary": "started", "artifactRefs": []},
            {"eventId": "event-2", "taskId": "task-1", "stage": "verify", "kind": "test_result", "attempt": 1,
             "timestamp": 2, "status": "SUCCESS", "summary": "passed", "artifactRefs": ["test"]},
        ]
        for event in events:
            await api.store.put("task_events", event["eventId"], event, str(event["timestamp"]))
        async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
            first = await client.get("/api/v1/codeops/task/task-1/events")
            replay = await client.get("/api/v1/codeops/task/task-1/events", headers={"Last-Event-ID": "event-1"})
        assert "id: event-1" in first.text and "id: event-2" in first.text
        assert "id: event-1" not in replay.text and "id: event-2" in replay.text
        assert first.headers["cache-control"] == "no-cache"
    finally:
        api.store.path = old_path
