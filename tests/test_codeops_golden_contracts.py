from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_autoagent.codeops import TestVerificationService, assert_golden_contract, chain_contract
from ops_autoagent.config import Settings
from ops_autoagent.graphs import CodeOpsGraph
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.schemas import CodeOpsTaskRequest


@pytest.mark.asyncio
async def test_code_review_no_diff_chain_matches_legacy_golden_contract(tmp_path: Path):
    """Same no-diff input fixes task state, skips, hook event and notification side effects."""
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings(codeops_test_execution_enabled=False)))
    state = await graph.invoke(CodeOpsTaskRequest(
        taskType="CODE_REVIEW", goal="Review migration", repository=str(tmp_path), maxRounds=8, maxToolCalls=20,
    ))
    contract = chain_contract(state)

    assert_golden_contract(contract, {
        "taskStatus": "COMPLETED",
        "testVerification": {"phase": "PHASE_5_LLM_TEST_VERIFICATION", "status": "NO_DIFF",
                             "testsPassed": False, "failureType": "", "patchRolledBack": False,
                             "backgroundStatuses": ["SKIPPED", "SKIPPED"]},
        "events": ["AFTER_TEST"],
        "notifications": [{"type": "BACKGROUND_TASK_SKIPPED", "status": "SKIPPED", "consumed": False},
                          {"type": "BACKGROUND_TASK_SKIPPED", "status": "SKIPPED", "consumed": False}],
    })


@pytest.mark.asyncio
async def test_compilation_failure_chain_matches_golden_exception_and_rollback_contract(tmp_path: Path, monkeypatch):
    settings = Settings(codeops_test_execution_enabled=True)
    service = TestVerificationService(OpenAICompatibleClient(settings), settings)

    async def failed_maven(_repository: str, args: list[str]) -> dict:
        return {"command": ["mvn", *args], "success": False, "exitCode": 1, "costMillis": 1,
                "output": "[ERROR] COMPILATION FAILURE\nOrderServiceTest.java:3 cannot find symbol"}

    monkeypatch.setattr(service, "_maven", failed_maven)
    test_patch = """--- /dev/null
+++ b/src/test/java/example/OrderServiceTest.java
@@ -0,0 +1,3 @@
+package example;
+class OrderServiceTest {}
+
"""
    outcome = await service.execute({
        "task": {"taskId": "golden-failure", "taskType": "INCIDENT_TO_FIX", "goal": "verify test rollback",
                 "repository": str(tmp_path), "steps": []},
        "context": {"allowTestPatchApply": True, "diffContext": {"repositoryPath": str(tmp_path),
                    "changeRef": "working_tree", "changedFiles": ["src/main/java/example/OrderService.java"],
                    "relatedTestFiles": [], "diffSummary": "changedFiles=1", "diffAvailable": True}},
        "working_memory": {"codeLocalization": {"targetFiles": ["src/main/java/example/OrderService.java"]},
                           "patchGeneration": {"testUnifiedDiffPatch": test_patch}},
    })
    state = {"status": outcome["status"], "context": outcome["context"], "steps": [{
        "selectedSkill": "test_verification", "status": outcome["status"],
        "rawEvidenceJson": json.dumps(outcome["raw"], ensure_ascii=False),
    }]}

    assert_golden_contract(chain_contract(state), {
        "taskStatus": "FAILED",
        "testVerification": {"phase": "PHASE_5_LLM_TEST_VERIFICATION", "status": "FAILED",
                             "testsPassed": False, "failureType": "TEST_COMPILE_FAILED", "patchRolledBack": True,
                             "backgroundStatuses": ["FAILED"]},
        "notifications": [{"type": "BACKGROUND_TASK_STARTED", "status": "RUNNING", "consumed": False},
                          {"type": "BACKGROUND_TASK_FAILED", "status": "FAILED", "consumed": False}],
    })
    assert not (tmp_path / "src/test/java/example/OrderServiceTest.java").exists()
