from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.graphs.codeops import CodeOpsGraph


class _ReleaseRiskLlm:
    available = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **_kwargs) -> str:
        self.prompts.append(prompt)
        return json.dumps({
            "reviewVerdict": "RETRY_REPAIR", "qualityScore": 42, "deterministicScore": 25,
            "semanticScore": 17, "patchDecision": "RETRY_REPAIR", "rootCauseAddressed": False,
            "workaround": True, "minimalChange": True, "scopeSafe": True, "testSufficient": False,
            "businessRisks": ["failed verification"], "concurrencyRisks": [],
            "reviewFindings": ["tests failed"], "mustReview": ["rerun maven"], "riskLevel": "HIGH",
            "impactScopes": ["service"], "riskPoints": ["compile failed"],
            "regressionFocus": ["targeted test"], "onlineObservationMetrics": ["5xx"],
            "rollbackFocus": ["rollback artifact"], "knowledgeReferences": ["[runbook][0.9] release -> docs/release.md"],
            "reasoning": ["deterministic facts prohibit release"], "humanApprovalPoints": ["review failed test"],
        })


def _state(tmp_path: Path) -> dict:
    return {
        "task": {"taskId": "risk-task", "taskType": "INCIDENT_TO_FIX", "goal": "fix order submit",
                 "repository": str(tmp_path), "changeRef": "working_tree"},
        "context": {"diffContext": {"repositoryPath": str(tmp_path), "changeRef": "working_tree",
                                        "changedFiles": ["src/main/java/example/OrderService.java"],
                                        "relatedTestFiles": ["src/test/java/example/OrderServiceTest.java"],
                                        "diffSummary": "changedFiles=1", "diffAvailable": True,
                                        "diff": "@@ -1 +1 @@\n- return 1;\n+ return 2;"},
                    "incidentFixReflectionFailures": [{"round": 1, "failedSkill": "test_verification"}]},
        "working_memory": {
            "fixStrategy": {"shouldEnterCodeRepair": True, "strategyType": "CODE_FIX"},
            "codeLocalization": {"targetFiles": ["src/main/java/example/OrderService.java"]},
            "engineeringKnowledge": {"hits": [{"category": "runbook", "score": "0.9", "title": "release", "path": "docs/release.md"}]},
            "patchGeneration": {"llmGenerated": True, "patchDraft": "--- a/x\n+++ b/x\n@@\n-old\n+new",
                                "changedFiles": ["src/main/java/example/OrderService.java"],
                                "patchScopeGuard": {"passed": True, "changedMethods": ["OrderService.submit"]},
                                "patchApply": {"applied": True}, "patchValidation": {"valid": True},
                                "compileGate": {"success": False},
                                "patchQuality": {"testsChanged": False, "staticSafetyPassed": True,
                                                 "minimalChangeScore": 95, "requiresHumanApproval": False},
                                "patchDiffAnalysis": {"touchedFiles": ["src/main/java/example/OrderService.java"],
                                                      "changedMethods": ["OrderService.submit"], "sensitiveFiles": [],
                                                      "configFileCount": 0, "productionFileCount": 1, "testFileCount": 0}},
            "testVerification": {"testsPassed": False, "testFailureType": "TEST_ASSERTION_FAILED",
                                   "recommendedTests": ["OrderServiceTest"], "mavenCommands": ["mvn test"],
                                   "testExecutionResults": ["[ERROR] test failed"]},
        },
        "steps": [], "round": 0,
    }


@pytest.mark.asyncio
async def test_release_risk_uses_patch_facts_and_independent_llm_review(tmp_path: Path):
    settings = Settings(codeops_agent_release_risk_llm_enabled=True)
    llm = _ReleaseRiskLlm(settings)
    result = await CodeOpsGraph(llm)._release_risk(_state(tmp_path))
    raw = result["context"]["releaseRiskRaw"]

    assert "patchFacts" in llm.prompts[0]
    assert raw["llmReleaseRiskSuccess"] is True
    assert raw["reviewVerdict"] == "RETRY_REPAIR"
    assert raw["patchFacts"]["redLines"] == ["COMPILE_NOT_PASSED", "TESTS_NOT_PASSED"]
    assert raw["manualTakeoverRequired"] is True
    assert raw["verificationBlockedReason"] == "Verification failed with type=TEST_ASSERTION_FAILED"
    assert raw["releaseRiskReport"]["riskLevel"] == "HIGH"
    assert raw["knowledgeMatches"][0]["path"] == "docs/release.md"


@pytest.mark.asyncio
async def test_release_risk_disabled_uses_java_compatible_unavailable_review(tmp_path: Path):
    settings = Settings(codeops_agent_release_risk_llm_enabled=False)
    llm = _ReleaseRiskLlm(settings)
    raw = (await CodeOpsGraph(llm)._release_risk(_state(tmp_path)))["context"]["releaseRiskRaw"]

    assert raw["llmReleaseRiskFallback"] is True
    assert raw["reviewVerdict"] == "REVIEW_UNAVAILABLE"
    assert raw["humanApprovalPoints"] == ["Release risk LLM agent is disabled."]
    assert raw["codeReview"]["patchDecision"] == "HUMAN_REVIEW"
