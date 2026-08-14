from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ops_autoagent.api import api_guard_counters, app, settings, store
from ops_autoagent.codeops.eval_cases import builtin_codeops_eval_cases


@pytest.fixture(autouse=True)
async def isolated_database(tmp_path: Path):
    store.path = tmp_path / "api.db"
    await store.initialize()


@pytest.mark.asyncio
async def test_health_and_skills():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/actuator/health")
        skills = await client.get("/api/v1/codeops/task/skills")
    assert health.json()["status"] == "UP"
    assert skills.json()["code"] == "0000"
    assert [item["skillId"] for item in skills.json()["data"]] == [
        "agent_loop_investigation", "bug_fix", "engineering_knowledge_rag", "fix_strategy_router",
        "ops_diagnosis", "pr_review", "release_risk_analysis", "repo_understanding", "test_verification",
    ]


def test_all_legacy_codeops_builtin_cases_are_present():
    assert [case["caseId"] for case in builtin_codeops_eval_cases()] == [
        "code-review-basic", "issue-to-patch-basic", "release-risk-basic", "incident-to-fix-basic",
        "incident-inventory-oversell-concurrency", "incident-db-pool-runtime-pressure",
        "incident-order-create-npe", "incident-gc-latency-spike", "incident-rpc-timeout-dependency",
        "incident-redis-timeout-cache", "incident-slow-sql-db-span", "incident-thread-pool-saturation",
        "incident-gateway-5xx-upstream", "scope-violation-reflection", "test-assertion-reflection",
        "scope-expansion-cross-file-idempotency",
    ]


@pytest.mark.asyncio
async def test_submit_and_query_task(tmp_path: Path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/codeops/task/submit", json={
            "taskType": "CODE_REVIEW", "goal": "Review the repository", "repository": str(tmp_path),
            "maxRounds": 1, "maxToolCalls": 5,
        })
        task_id = created.json()["data"]["taskId"]
        queried = await client.get(f"/api/v1/codeops/task/{task_id}")
    assert queried.json()["data"]["taskId"] == task_id
    assert set(queried.json()["data"]) == {
        "taskId", "taskType", "goal", "repository", "changeRef", "status", "maxRounds",
        "maxToolCalls", "finalSummary", "steps", "createTime", "updateTime",
    }


@pytest.mark.asyncio
async def test_agent_loop_dry_run_matches_legacy_defaults_and_shape(tmp_path: Path):
    (tmp_path / "OrderService.java").write_text("class OrderService {}", encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/agent-loop/run", json={
            "goal": "inspect OrderService", "repository": str(tmp_path), "dryRun": True,
        })
    payload = response.json()
    assert payload["code"] == "0000"
    assert set(payload["data"]) == {"status", "finalAnswer", "stopReason", "turns", "trace", "steps"}
    assert payload["data"]["status"] == "COMPLETED" and payload["data"]["turns"] == 2
    assert payload["data"]["steps"] == []  # Java includes steps only when includeSteps is explicitly true.


@pytest.mark.asyncio
async def test_approval_approve_accepts_empty_body_like_legacy():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/task/missing/approval/approve")
    assert response.status_code == 200
    assert response.json()["code"] == "0001"


@pytest.mark.asyncio
async def test_missing_approval_returns_legacy_empty_map_not_null():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/codeops/task/missing/approval")
        evaluation = await client.get("/api/v1/codeops/evaluation/approval/missing")
    assert response.json() == {"code": "0000", "info": "No pending approval for this task", "data": {}}
    assert evaluation.json() == {"code": "0000", "info": "No pending approval for this task", "data": None}


@pytest.mark.asyncio
async def test_approval_decisions_match_legacy_record_and_task_transitions():
    approval = {"taskId": "approve-1", "caseName": "case", "status": "PENDING", "rootCause": "cause",
                "patchSummary": "diff", "changedFiles": ["app.py"], "riskLevel": "HIGH",
                "testResults": "BUILD SUCCESS", "approvalReasons": ["release risk is HIGH"],
                "evidenceSummary": {}, "patchQuality": {}, "patchSandbox": {}, "submittedAt": "2026-01-01T00:00:00",
                "approvedAt": None, "rejectionReason": None}
    task = {"taskId": "approve-1", "status": "WAITING_APPROVAL", "finalSummary": "ready",
            "approval": approval, "updateTime": "2026-01-01T00:00:00"}
    await store.put("tasks", "approve-1", task, task["updateTime"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/task/approve-1/approval/approve")
        saved = await client.get("/api/v1/codeops/task/approve-1/approval")
    assert response.json()["data"]["status"] == "APPROVED"
    assert response.json()["data"]["approvedAt"]
    assert saved.json()["data"]["status"] == "APPROVED"
    persisted = await store.get("tasks", "approve-1")
    assert persisted["status"] == "COMPLETED"
    assert persisted["finalSummary"] == "ready\n人工审批已通过，任务完成。"


@pytest.mark.asyncio
async def test_real_evaluation_endpoints_execute_graphs():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        codeops = (await client.post("/api/v1/codeops/evaluation/run/incident-db-pool-runtime-pressure")).json()
        ops = (await client.post("/api/v1/ops/evaluation/run/incident-order-create-npe")).json()
    assert codeops["code"] == "0000" and codeops["data"]["runs"][0]["taskId"]
    assert "expectedSkillCoverage" in codeops["data"]["runs"][0]
    assert ops["code"] == "0000" and ops["data"]["runs"][0]["diagnosisId"]
    assert "averageEvidenceCoverage" in ops["data"]
    assert set(ops["data"]) == {
        "batchId", "totalCases", "successCases", "failedCases", "top1RootCauseHitRate",
        "top3RootCauseHitRate", "averageEvidenceCoverage", "averageExpectedToolCoverage",
        "averageToolCallCount", "averageLatencyMs", "runs",
    }
    assert len(await store.find("eval_metrics", lambda row: row["runId"] == ops["data"]["runs"][0]["runId"])) == 12


@pytest.mark.asyncio
async def test_ops_guard_token_rate_limit_and_audit(monkeypatch):
    monkeypatch.setattr(settings, "ops_security_enabled", True)
    monkeypatch.setattr(settings, "ops_api_token", "expected-token")
    monkeypatch.setattr(settings, "ops_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "ops_rate_limit_max_requests", 1)
    api_guard_counters.clear()
    payload = {"serviceName": "guard-service", "problem": "timeout",
               "startTime": "2026-01-01T00:00:00+00:00", "endTime": "2026-01-01T00:05:00+00:00"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post("/api/v1/ops/incident/analyze", json=payload)
        allowed = await client.post("/api/v1/ops/incident/analyze", json=payload,
                                    headers={"X-Ops-Token": "expected-token", "X-Forwarded-For": "10.0.0.8"})
        limited = await client.post("/api/v1/ops/incident/analyze", json=payload,
                                    headers={"X-Ops-Token": "expected-token", "X-Forwarded-For": "10.0.0.8"})
    assert "invalid ops api token" in denied.text and denied.headers["content-type"].startswith("text/event-stream")
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-cache"
    assert "rate limit exceeded, max 1 requests per 60 seconds" in limited.text
    audits = await store.recent("audit_logs", 100)
    assert {item.get("result") for item in audits} >= {"ALLOW", "DENY"}


@pytest.mark.asyncio
async def test_incident_validation_keeps_legacy_sse_error_contract():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/ops/incident/analyze", json={"serviceName": ""})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"error"' in response.text and '"subType":"diagnosis_error"' in response.text
    assert '"completed":true' in response.text and "serviceName cannot be blank" in response.text


@pytest.mark.asyncio
async def test_alertmanager_empty_payload_and_message_match_legacy_contract():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.post("/api/v1/ops/alert/webhook/alertmanager", json={"alerts": []})
        accepted = await client.post("/api/v1/ops/alert/webhook/alertmanager", json={"alerts": [{
            "status": "resolved", "fingerprint": "resolved-1",
            "labels": {"alertname": "Resolved", "service": "orders"},
        }]})
    assert empty.json() == {"code": "0002", "info": "alert webhook payload cannot be empty", "data": None}
    assert accepted.json()["data"] == {"totalAlerts": 1, "acceptedCount": 0, "skippedCount": 1,
                                        "message": "alert webhook accepted"}
