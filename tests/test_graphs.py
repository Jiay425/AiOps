import json
from pathlib import Path

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.graphs import CodeOpsGraph, OpsDiagnosisGraph
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.schemas import CodeOpsTaskRequest, IncidentAnalyzeRequest
from ops_autoagent.tools import ObservabilityTools


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ops_database_path=tmp_path / "test.db",
        ops_runbook_path=Path("docs/dev-ops/runbook"),
        ops_fixture_fallback=True,
        openai_api_key="",
        prometheus_base_url="",
    )


@pytest.mark.asyncio
async def test_ops_graph_completes_with_deterministic_fallback(settings: Settings):
    llm = OpenAICompatibleClient(settings)
    graph = OpsDiagnosisGraph(ObservabilityTools(settings), llm)
    result = await graph.invoke(IncidentAnalyzeRequest(
        serviceName="order-service", startTime="2026-01-01T00:00:00Z", endTime="2026-01-01T00:10:00Z",
        problem="HTTP 500 and timeout",
    ))
    assert result["status"] == "SUCCESS"
    assert result["report"].startswith("# Incident diagnosis")
    assert result["events"][-1]["type"] == "complete"
    assert result["events"][-1]["subType"] == "diagnosis_completed"
    assert result["evidence"]


@pytest.mark.asyncio
async def test_ops_graph_streams_each_stage_and_result(settings: Settings):
    graph = OpsDiagnosisGraph(ObservabilityTools(settings), OpenAICompatibleClient(settings))
    initial = graph.create_state(IncidentAnalyzeRequest(
        serviceName="order-service", startTime="2026-01-01T00:00:00Z",
        endTime="2026-01-01T00:10:00Z", problem="HTTP 500 and timeout",
    ))
    streamed = [item async for item in graph.stream(initial)]
    events = [event for event, _ in streamed if event is not None]
    assert events[0]["subType"] == "intent"
    assert events[-1]["subType"] == "diagnosis_completed"
    assert {"intent", "prometheus", "elk", "skywalking", "evidence_chain", "runbook",
            "diagnosis_report", "diagnosis_completed"} <= {event["subType"] for event in events}
    assert all({"type", "subType", "content", "completed", "timestamp", "sessionId"} <= event.keys()
               for event in events)
    assert streamed[-1][0] is None and streamed[-1][1]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_required_chat_failure_is_streamed_and_persistable(tmp_path: Path):
    strict = Settings(ops_database_path=tmp_path / "strict.db", ops_fixture_fallback=False,
                      ops_agent_chat_required=True, openai_api_key="", prometheus_base_url="")
    graph = OpsDiagnosisGraph(ObservabilityTools(strict), OpenAICompatibleClient(strict))
    initial = graph.create_state(IncidentAnalyzeRequest(
        serviceName="strict-service", startTime="2026-01-01T00:00:00Z",
        endTime="2026-01-01T00:10:00Z", problem="timeout",
    ))
    streamed = [item async for item in graph.stream(initial)]
    assert streamed[-1][0]["type"] == "error"
    assert streamed[-1][0]["subType"] == "diagnosis_error"
    assert streamed[-1][1]["status"] == "FAILED"
    assert "Required PLANNER Chat Agent failed" in streamed[-1][1]["error"]


@pytest.mark.asyncio
async def test_plan_driven_mode_suppresses_unplanned_tools(settings: Settings):
    graph = OpsDiagnosisGraph(ObservabilityTools(settings), OpenAICompatibleClient(settings))
    result = await graph.invoke(IncidentAnalyzeRequest(
        serviceName="budget-service", startTime="2026-01-01T00:00:00Z",
        endTime="2026-01-01T00:10:00Z", problem="bounded investigation", maxStep=1,
    ))
    assert result["metrics"]["source"] == "DENIED"
    assert result["logs"]["source"] == "DENIED"
    assert result["traces"]["source"] == "DENIED"
    assert all(not item["allowed"] for item in result["tool_trace"][:3])


@pytest.mark.asyncio
async def test_codeops_graph_has_explicit_steps(settings: Settings, tmp_path: Path):
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    result = await graph.invoke(CodeOpsTaskRequest(
        taskType="CODE_REVIEW", goal="Review migration", repository=str(tmp_path), maxRounds=8, maxToolCalls=20,
    ))
    assert result["status"] == "COMPLETED"
    # The Java service records only orchestrator decisions/skill executions; planning is not a user-visible step.
    assert result["task"]["steps"][0]["selectedSkill"] == "agent_loop_investigation"
    assert any(step["selectedSkill"] == "test_verification" for step in result["steps"])
    test_step = next(step for step in result["steps"] if step["selectedSkill"] == "test_verification")
    test_raw = json.loads(test_step["rawEvidenceJson"])
    assert test_raw["phase"] == "PHASE_5_LLM_TEST_VERIFICATION"
    assert test_raw["mavenCommands"][0] == "mvn -q -DskipTests compile"
    assert {"testsPassed", "testFailureType", "testPatchValidation", "testPatchApply"} <= set(test_raw)
    assert result["task"]["finalSummary"].startswith("CodeOps Incident-to-Fix 任务执行完成：taskType=CODE_REVIEW")
    assert set(result["task"]["context"]["guardrailSummary"]) == {
        "realEvidenceCoverage", "fixtureFallbackUsed", "patchSandboxMode", "patchSandboxIsolated",
        "patchStaticSafetyPassed", "minimalChangeScore", "testsPassed", "approvalStatus", "approvalReasons",
    }
