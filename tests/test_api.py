from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ops_autoagent.api import (_bounded_evaluation_text, _evaluation_terminal_success, _fixture_payloads, _previous_successful_eval_run,
                               api_guard_counters, app, settings, store)
from ops_autoagent.config import get_settings
from ops_autoagent.graphs.codeops import CodeOpsGraph
from ops_autoagent.codeops.eval_cases import (BUSINESS_EVAL_LEVEL, BASELINE_CASE_SOURCE,
                                              EXPANDED_BUSINESS_CASE_IDS, EXPANSION_CASE_SOURCE,
                                              LEGACY_BASELINE_CASE_IDS, builtin_codeops_eval_cases,
                                              runtime_reliability_cases)
from ops_autoagent.codeops.evaluation import _evaluation_outcome, _fix_strategy, _normalized_missing, collect_raw_outputs
from ops_autoagent.codeops.evaluation import build_report


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
    cases = builtin_codeops_eval_cases()
    assert [case["caseId"] for case in cases[:16]] == list(LEGACY_BASELINE_CASE_IDS)
    assert len(cases) == 52
    assert [case["caseId"] for case in cases[16:]] == list(EXPANDED_BUSINESS_CASE_IDS)
    assert len({case["caseId"] for case in cases}) == 52
    assert all(case["caseLifecycle"] == "COMPLETED" for case in cases)
    assert all(case["caseSource"] == BASELINE_CASE_SOURCE for case in cases[:16])
    assert all(case["evaluationLevel"] == BUSINESS_EVAL_LEVEL for case in cases)


def test_expanded_cases_have_traceable_fixture_and_repository_references():
    cases = builtin_codeops_eval_cases()
    expanded = cases[16:]
    assert all(case["caseSource"] == EXPANSION_CASE_SOURCE for case in expanded)
    assert all(case["fixtureReference"] and Path(case["fixtureReference"]).exists() for case in expanded)
    assert all(case["repositoryFixtureReference"] and Path(case["repositoryFixtureReference"]).exists()
               for case in expanded)
    assert all(case["context"].get("fixtureDataClass") == "TEST_SIMULATED_DATA" for case in expanded)
    assert all(len(_fixture_payloads(case)) >= 2 for case in expanded)


def test_shared_scenario_fixture_is_selected_by_case_id_not_by_a_reused_incident():
    cases = {case["caseId"]: case for case in builtin_codeops_eval_cases()}
    payload = _fixture_payloads(cases["incident-db-deadlock"])
    text = str(payload).lower()
    assert {"prometheus", "logs", "trace"} == set(payload)
    assert "deadlock" in text and "rate-limit" not in text
    assert cases["incident-db-deadlock"]["evaluationCaseRevision"] == "8"
    assert cases["incident-db-deadlock"]["expectedEvidenceKeywords"] == ["database", "deadlock", "lock", "transaction"]


def test_corrected_code_fix_cases_use_dedicated_evidence_and_vulnerable_snapshot():
    ids = {
        "incident-order-idempotency-race", "incident-coupon-double-deduction", "incident-order-state-transition-race",
        "issue-to-patch-pagination-boundary", "issue-to-patch-precision-money", "issue-to-patch-timezone-date",
        "issue-to-patch-input-validation", "issue-to-patch-retry-idempotency", "scope-cross-module-patch-blocked",
        "test-flaky-reflection-repair",
    }
    cases = {case["caseId"]: case for case in builtin_codeops_eval_cases() if case["caseId"] in ids}
    assert len(cases) == 10
    for case in cases.values():
        fixture = Path(case["fixtureReference"])
        assert case["evaluationCaseRevision"] == "2"
        assert case["repository"] == "samples/codeops-eval"
        assert case["context"]["fixtureCaseId"] == case["caseId"]
        assert all((fixture.parent / name).is_file() for name in ("eval-case.json", "prometheus.json", "es-logs.json", "skywalking-trace.json"))
        for relative in case["expectedTargetFiles"]:
            assert (Path(case["repository"]) / relative).is_file()


def test_fixture_path_resolves_to_its_case_id():
    assert CodeOpsGraph._fixture_case_id({"fixtureCase": "fixtures/incident/issue-to-patch-pagination-boundary/eval-case.json"}) == \
        "issue-to-patch-pagination-boundary"


def test_runtime_cases_are_not_business_eval_cases():
    business_ids = {case["caseId"] for case in builtin_codeops_eval_cases()}
    runtime_ids = {case["caseId"] for case in runtime_reliability_cases()}
    assert len(runtime_ids) == 10 and business_ids.isdisjoint(runtime_ids)


def test_expected_no_code_fix_is_a_valid_evaluation_terminal_state():
    assert _evaluation_terminal_success({"expectedFixStrategy": "NO_CODE_FIX"}, "NO_CODE_FIX")
    assert not _evaluation_terminal_success({"expectedFixStrategy": "CODE_FIX"}, "NO_CODE_FIX")
    assert _evaluation_terminal_success({"expectedOutcome": {"requiredStoppingState": "SCOPE_GUARD_REJECTED_OR_HUMAN_TAKEOVER"}}, "REVIEW_REJECTED")


def test_explicit_optional_env_file_loads_without_hardcoding_a_secret_path(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "local.env"
    env_file.write_text("OPENAI_BASE_URL=https://example.invalid\nOPENAI_API_KEY=test-only-key\n", encoding="utf-8")
    monkeypatch.setenv("OPS_AUTOAGENT_ENV_FILE", str(env_file))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    loaded = get_settings()
    try:
        assert loaded.openai_base_url == "https://example.invalid"
        assert loaded.openai_api_key == "test-only-key"
    finally:
        import os
        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("OPENAI_API_KEY", None)
        get_settings.cache_clear()


def test_evaluation_metric_projection_is_bounded_and_omits_repository_snapshot():
    text = _bounded_evaluation_text(
        {"taskType": "ISSUE_TO_PATCH", "goal": "fix", "status": "COMPLETED",
         "steps": [{"selectedSkill": "bug_fix", "status": "SUCCESS",
                     "resultSummary": "ok", "rawEvidenceJson": "x" * 100000}]},
        {"fixtureEvidence": {"logs": "fixture"}},
        {"repoBaselineSnapshot": {"secret.java": "secret" * 100000},
         "patchGeneration": {"summary": "patch", "patchDraft": "large" * 100000}},
    )
    assert len(text) <= 60000
    assert "secret.java" not in text


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
async def test_successful_same_revision_case_is_not_rerun():
    case = next(item for item in builtin_codeops_eval_cases() if item["caseId"] == "incident-db-deadlock")
    await store.put("eval_runs", "existing-success", {"runId": "existing-success", "caseId": case["caseId"],
                    "taskId": "prior-task", "status": "SUCCESS", "detail": {"expected": case}}, "2026-08-15T00:00:00")
    prior = await _previous_successful_eval_run(case)
    assert prior and prior["taskId"] == "prior-task"


@pytest.mark.asyncio
async def test_report_rebuild_uses_current_scoring_without_invoking_a_graph():
    case = next(item for item in builtin_codeops_eval_cases() if item["caseId"] == "incident-db-deadlock")
    task = {"taskId": "rebuild-task", "taskType": case["taskType"], "status": "NO_CODE_FIX",
            "context": {}, "steps": [{"stepNo": 1, "selectedSkill": "ops_diagnosis", "status": "SUCCESS",
                                          "resultSummary": "deadlock evidence", "rawEvidenceJson": "{}"}],
            "usedToolCalls": 3, "finalSummary": "deadlock isolated", "repository": case["repository"]}
    run = {"runId": "rebuild-run", "caseId": case["caseId"], "taskId": task["taskId"],
           "taskType": case["taskType"], "status": "SUCCESS", "evidenceKeywordCoverage": 1.0,
           "artifactCoverage": 1.0, "stepCount": 1, "usedToolCalls": 3, "latencyMs": 10,
           "detail": {"expected": case, "codeLocalizationCoverage": 1.0,
                      "localizationDecisionCoverage": 1.0, "patchCoverage": 1.0, "testCoverage": 1.0}}
    await store.put("tasks", task["taskId"], task, "2026-08-15T00:00:00")
    await store.put("eval_runs", run["runId"], run, "2026-08-15T00:00:00")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/evaluation/report/rebuild")
    data = response.json()["data"]
    assert response.json()["code"] == "0000"
    assert data["evaluationScoringSchemaVersion"] == "2"
    assert data["catalogBusinessE2ETotal"] == 52
    assert data["evaluatedBusinessE2ETotal"] == 1
    assert data["cases"][0]["rootCauseHit"] is True


def test_expanded_code_fix_cases_preserve_fixture_test_oracles():
    cases = [case for case in builtin_codeops_eval_cases()
             if case.get("caseSource") == "EVAL_EXPANSION" and case.get("expectedOutcome", {}).get("requiresPatchSandbox")]
    assert cases
    assert all(case["context"].get("allowPatchApply") is True for case in cases)
    assert all("allowTestPatchApply" not in case["context"] for case in cases)


def test_evaluation_metrics_keep_requested_fixture_and_structured_fix_strategy():
    text = _bounded_evaluation_text(
        {"taskType": "ISSUE_TO_PATCH", "goal": "money precision", "steps": []}, {}, {},
        {"logs": {"observations": ["amount mismatch 0.30000000000000004"]}},
    )
    assert "amount mismatch" in text
    assert _fix_strategy({"fixStrategy": {"strategyType": "CODE_FIX"}}) == "CODE_FIX"


def test_case_report_uses_runtime_evidence_keyword_coverage():
    outcome = _evaluation_outcome(
        {"expectedFixStrategy": "CODE_FIX"}, {"status": "SUCCESS", "evidenceKeywordCoverage": 1.0},
        {"status": "COMPLETED", "context": {}}, {"testExecutionResults": ["success=true"], "reviewVerdict": "ACCEPT"},
        {"score": 1.0}, {}, {}, {"success": True},
    )
    assert outcome["rootCauseHit"] is True
    assert outcome["evidenceCoverage"]["keywordCoverage"] == 1.0


def test_case_report_normalizes_method_signatures_for_localization():
    assert not _normalized_missing(["submitHttp", "validate"], [
        "OrderController.submitHttp(OrderSubmitRequest)", "OrderSubmitRequest.validate()",
    ])


def test_case_report_uses_structured_localization_when_later_steps_overwrite_fields():
    task = {"steps": [
        {"rawEvidenceJson": '{"targetMethods":["wrongMethod"]}'},
        {"rawEvidenceJson": '{"targetMethods":["reviewerSummary"]}'},
    ], "context": {"incidentFixWorkingMemory": {"codeLocalization": {
        "targetFiles": ["src/main/java/OrderService.java"], "targetMethods": ["OrderService.submit(Request)"],
        "strategyType": "CODE_FIX", "scopeDecisionType": "STRICT_SINGLE_METHOD", "shouldEnterCodeRepair": True,
    }}}}
    raw = collect_raw_outputs(task)
    assert raw["targetMethods"] == ["OrderService.submit(Request)"]
    assert raw["targetFiles"] == ["src/main/java/OrderService.java"]


def test_delivery_only_report_separates_sandbox_verification_from_production_apply():
    report = build_report("batch", [{
        "status": "SUCCESS", "localizationEval": {"expectedTargetFiles": [], "expectedTargetMethods": [], "expectedFixStrategy": "", "expectedScopeDecision": "", "targetFileMatched": None, "targetMethodMatched": None, "fixStrategyMatched": None, "scopeDecisionMatched": None, "score": 1.0},
        "scopeType": "STRICT_SINGLE_METHOD", "patchApplied": False, "compilePassed": True, "testsPassed": True,
            "patchGenerated": True, "verificationStatus": "PASSED", "rootCauseHit": True,
            "evidenceCoverage": {"keywordCoverage": 1.0}, "repairAttempts": 0, "reflectionRounds": 0, "latencyMs": 10,
            "expectedOutcome": {"classification": "CODE_FIX"}, "targetMethods": ["submit"], "reflectionRecovered": False,
            "realEvidenceCoverage": 1.0, "patchQuality": {}, "patchSandbox": {}, "steps": [],
    }, {
        "status": "SUCCESS", "localizationEval": {"expectedTargetFiles": [], "expectedTargetMethods": [], "expectedFixStrategy": "", "expectedScopeDecision": "", "targetFileMatched": None, "targetMethodMatched": None, "fixStrategyMatched": None, "scopeDecisionMatched": None, "score": 1.0},
        "scopeType": "STRICT_SINGLE_METHOD", "patchApplied": False, "compilePassed": True, "testsPassed": False,
            "patchGenerated": True, "verificationStatus": "FAILED_OR_NOT_EXECUTED", "rootCauseHit": True,
            "evidenceCoverage": {"keywordCoverage": 1.0}, "repairAttempts": 0, "reflectionRounds": 0, "latencyMs": 10,
            "expectedOutcome": {"classification": "CODE_FIX", "requiredStoppingState": "SCOPE_GUARD_REJECTED_OR_HUMAN_TAKEOVER"},
            "targetMethods": ["submit"], "reflectionRecovered": False, "realEvidenceCoverage": 1.0,
            "patchQuality": {}, "patchSandbox": {}, "steps": [],
    }])
    assert report["summaryMetrics"]["patchGeneratedRate"] == 1.0
    assert report["summaryMetrics"]["verificationPassRate"] == 1.0
    assert report["summaryMetrics"]["patchApplyRate"] == 0.0


@pytest.mark.asyncio
async def test_expanded_business_catalog_and_representative_cases_use_real_graph_path():
    representative = [
        "incident-order-idempotency-race",  # incident / distributed consistency
        "incident-db-deadlock",  # database / infrastructure
        "issue-to-patch-precision-money",  # issue to patch
        "release-risk-canary-rollback",  # release risk
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = (await client.get("/api/v1/codeops/evaluation/cases", params={"caseSource": "EVAL_EXPANSION"})).json()
        summary = (await client.get("/api/v1/codeops/evaluation/summary")).json()
        runs = [(case_id, (await client.post(f"/api/v1/codeops/evaluation/run/{case_id}")).json())
                for case_id in representative]
    assert catalog["code"] == "0000" and len(catalog["data"]) == 36
    assert summary["data"]["businessE2ETotal"] == 52
    assert summary["data"]["baselineCompleted"] == 16
    assert summary["data"]["newlyAddedCompleted"] == 36
    assert summary["data"]["runtimeSafetyReliabilityCases"] == 10
    for case_id, response in runs:
        assert response["code"] == "0000"
        run = response["data"]["runs"][0]
        assert run["caseId"] == case_id and run["taskId"]
        assert run["detail"]["expected"]["evaluationLevel"] == BUSINESS_EVAL_LEVEL


@pytest.mark.asyncio
async def test_full_business_evaluation_report_keeps_runtime_cases_separate():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/evaluation/run")
        report_response = await client.get("/api/v1/codeops/evaluation/report")
    assert response.json()["code"] == "0000"
    result = response.json()["data"]
    report = report_response.json()["data"]
    assert (result["totalCases"], result["businessE2ETotal"], result["baselineCompleted"],
            result["newlyAddedCompleted"], result["runtimeSafetyReliabilityCases"]) == (52, 52, 16, 36, 10)
    assert (report["businessE2ETotal"], report["baselineCompleted"], report["newlyAddedCompleted"],
            report["runtimeSafetyReliabilityCases"]) == (52, 16, 36, 10)
    required = {"caseId", "caseLifecycle", "caseSource", "evaluationLevel", "fixtureReference",
                "expectedOutcome", "actualOutcome", "rootCauseHit", "evidenceCoverage", "localizationCoverage",
                "patchGenerated", "verificationStatus", "reviewDecision", "scopeGuardStatus", "toolCallCount",
                "repairAttempts", "latencyMs", "failureReason"}
    assert len(report["cases"]) == 52 and all(required <= set(case) for case in report["cases"])


@pytest.mark.asyncio
async def test_scope_expansion_eval_case_keeps_effect_boundary_zero():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/codeops/evaluation/run/scope-cross-module-patch-blocked")
    assert response.json()["code"] == "0000"
    run = response.json()["data"]["runs"][0]
    metrics = await store.find("runtime_metrics", lambda item: item.get("taskId") == run["taskId"])
    writes = [item for item in metrics if item.get("metricName") == "unauthorized_target_repository_writes"]
    assert writes and all(item.get("value") == 0 for item in writes)


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
