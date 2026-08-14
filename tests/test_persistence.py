from pathlib import Path

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.graphs import CodeOpsGraph
from ops_autoagent.persistence import CheckpointerManager
from ops_autoagent.schemas import CodeOpsTaskRequest
from ops_autoagent.store import Store


class PatchLlm:
    available = True

    async def complete(self, prompt, **_kwargs):
        if "agent loop planner" in prompt:
            return '{"thoughtSummary":"localized","toolCalls":[],"finalAnswer":{"summary":"found app",' \
                   '"fixStrategy":"CODE_FIX","scopeDecision":"FULL_FILE","targetFiles":["app.py"],' \
                   '"targetMethods":["run"],"shouldEnterCodeRepair":true,"localizationConfidence":"HIGH"}}'
        return '{"summary":"fix","patches":[{"path":"app.py","old":"return 1","new":"return 2"}]}'


@pytest.mark.asyncio
async def test_sqlite_checkpointer_persists_completed_graph_state_after_restart(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    settings = Settings(langgraph_checkpoint_backend="sqlite",
                        langgraph_checkpoint_path=tmp_path / "checkpoints.db")
    first_manager = CheckpointerManager(settings)
    first_graph = CodeOpsGraph(PatchLlm(), checkpointer=await first_manager.start())
    completed = await first_graph.invoke(CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX", goal="app.py return", repository=str(repository),
        context={"allowPatchApply": True}, maxRounds=8, maxToolCalls=20))
    task_id = completed["task"]["taskId"]
    await first_manager.close()

    second_manager = CheckpointerManager(settings)
    second_graph = CodeOpsGraph(PatchLlm(), checkpointer=await second_manager.start())
    snapshot = await second_graph.graph.aget_state({"configurable": {"thread_id": task_id}})
    await second_manager.close()
    assert snapshot.values["task"]["taskId"] == task_id
    # Approval never mutates the original repository in the Java implementation.
    assert "return 1" in source.read_text(encoding="utf-8")


def test_ops_evaluation_legacy_sql_mapping_keeps_run_case_and_metric_tables_separate():
    run_table, run = Store._legacy_write_spec("eval_runs", {
        "runId": "run-1", "caseId": "case-1", "diagnosisId": "diag-1", "status": "SUCCESS",
        "top1RootCauseHit": 1, "top3RootCauseHit": 1, "requiredEvidenceCoverage": 0.75,
        "unsupportedConclusionCount": 0, "toolCallCount": 3, "diagnosisLatencyMs": 42,
        "finalStatus": "SUCCESS", "summaryJson": "{}", "createTime": "2026-01-01T00:00:00",
        "updateTime": "2026-01-01T00:00:01",
    })
    case_table, case = Store._legacy_write_spec("eval_cases", {
        "caseId": "case-1", "caseName": "case", "serviceName": "orders", "enabled": 1,
        "expectedEvidenceTypesJson": ["metrics"], "expectedToolsJson": ["query_prometheus"],
        "createTime": "2026-01-01T00:00:00", "updateTime": "2026-01-01T00:00:01",
    })
    metric_table, metric = Store._legacy_write_spec("eval_metrics", {
        "runId": "run-1", "caseId": "case-1", "metricName": "toolCallCount", "metricValue": 3,
        "metricDetailJson": "{}", "createTime": "2026-01-01T00:00:01",
    })
    assert run_table == "ops_eval_run" and run["diagnosis_latency_ms"] == 42
    assert case_table == "ops_eval_case" and case["expected_tools_json"] == '["query_prometheus"]'
    assert metric_table == "ops_eval_metric" and metric["metric_name"] == "toolCallCount"
