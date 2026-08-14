from ops_autoagent.codeops import IncidentFixOrchestratorPolicy


def test_incident_orchestrator_preserves_java_stage_order():
    policy = IncidentFixOrchestratorPolicy()
    memory, done, context = {}, [], {"allowPatchApply": True}
    assert policy.decide("INCIDENT_TO_FIX", memory, done, context).selected_skill == "ops_diagnosis"
    memory["opsEvidence"] = {"signals": ["500"]}
    done.append("ops_diagnosis")
    assert policy.decide("INCIDENT_TO_FIX", memory, done, context).selected_skill == "agent_loop_investigation"
    done.append("agent_loop_investigation")
    assert policy.decide("INCIDENT_TO_FIX", memory, done, context).selected_skill == "repo_understanding"
    memory["codeLocalization"] = {"targetFiles": ["App.java"], "localizationSuccess": True}
    done.append("repo_understanding")
    assert policy.decide("INCIDENT_TO_FIX", memory, done, context).selected_skill == "engineering_knowledge_rag"
    memory["engineeringKnowledge"] = {"matches": []}
    done.append("engineering_knowledge_rag")
    assert policy.decide("INCIDENT_TO_FIX", memory, done, context).selected_skill == "bug_fix"


def test_localization_gate_stops_unsafe_repair():
    policy = IncidentFixOrchestratorPolicy()
    memory = {"opsEvidence": {"signals": []}, "codeLocalization": {
        "localizationSuccess": False, "missingEvidence": ["root cause method"], "localizationBlocking": True,
    }, "releaseRisk": {"riskLevel": "HIGH"}}
    decision = policy.decide("INCIDENT_TO_FIX", memory,
                             ["ops_diagnosis", "agent_loop_investigation", "repo_understanding", "release_risk_analysis"],
                             {"allowPatchApply": True})
    assert decision.should_stop


def test_agent_loop_low_confidence_requires_repo_followup():
    policy = IncidentFixOrchestratorPolicy()
    memory = {"opsEvidence": {"signals": ["500"]}, "codeLocalization": {
        "localizationSuccess": True, "localizationConfidence": "LOW", "targetFiles": [],
    }}
    decision = policy.decide("INCIDENT_TO_FIX", memory, ["ops_diagnosis", "agent_loop_investigation"],
                             {"allowPatchApply": True})
    assert decision.selected_skill == "repo_understanding"


def test_reflection_reexecutes_test_and_release_even_when_previously_executed():
    policy = IncidentFixOrchestratorPolicy()
    memory = {"opsEvidence": {"signals": ["500"]}, "codeLocalization": {"targetFiles": ["App.java"]},
              "engineeringKnowledge": {"matches": ["runbook"]}, "patchGeneration": {"patch": "new"}}
    done = ["ops_diagnosis", "agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag",
            "bug_fix", "test_verification", "release_risk_analysis"]
    decision = policy.decide("INCIDENT_TO_FIX", memory, done,
                             {"allowPatchApply": True, "incidentFixReflectionRound": 1})
    assert decision.selected_skill == "test_verification"


def test_orchestrator_terminal_and_failure_routes_match_java_reasons():
    policy = IncidentFixOrchestratorPolicy()
    completed = {"opsEvidence": {"ok": True}, "codeLocalization": {"targetFiles": ["App.java"]},
                 "engineeringKnowledge": {"ok": True}, "patchGeneration": {"llmGenerated": True},
                 "testVerification": {"testsPassed": True}, "releaseRisk": {"riskLevel": "LOW"}}
    done = ["ops_diagnosis", "agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag",
            "bug_fix", "test_verification", "release_risk_analysis"]
    decision = policy.decide("INCIDENT_TO_FIX", completed, done, {})
    assert decision.reason == "Incident-to-Fix 所需的运维证据、代码定位、知识补充、修复、测试和发布风险阶段均已完成或已尝试。"

    exhausted = policy.decide("INCIDENT_TO_FIX", {**completed, "releaseRisk": {}}, done[:-1],
                              {"incidentFixReflectionExhausted": True})
    assert exhausted.selected_skill == "release_risk_analysis"
    assert exhausted.reason.startswith("测试验证连续失败已达到 3 轮反思上限")

    blocked_memory = {"opsEvidence": {"ok": True}, "codeLocalization": {
        "localizationBlocking": True, "missingEvidence": ["method"]}}
    blocked = policy.decide("INCIDENT_TO_FIX", blocked_memory,
                            ["ops_diagnosis", "agent_loop_investigation", "repo_understanding"], {})
    assert blocked.selected_skill == "release_risk_analysis"
    blocked_memory["releaseRisk"] = {"riskLevel": "HIGH"}
    stopped = policy.decide("INCIDENT_TO_FIX", blocked_memory,
                            ["ops_diagnosis", "agent_loop_investigation", "repo_understanding",
                             "release_risk_analysis"], {})
    assert stopped.reason == "代码定位质量门阻断自动修复：根因文件/方法或支撑证据不足，任务停止等待人工补证。"


def test_non_incident_orchestrator_terminal_contracts():
    policy = IncidentFixOrchestratorPolicy()
    localization = {"codeLocalization": {"targetFiles": ["App.java"], "localizationConfidence": "HIGH"},
                    "engineeringKnowledge": {"hits": []}, "testVerification": {"plan": True}}
    review = policy.decide("CODE_REVIEW", localization, ["agent_loop_investigation", "repo_understanding",
                           "engineering_knowledge_rag", "pr_review", "test_verification"], {})
    assert review.reason == "Code-Review 的代码理解、知识补充、审查和测试验证阶段均已完成或已尝试。"
    release = policy.decide("RELEASE_RISK", {**localization, "releaseRisk": {"riskLevel": "LOW"}},
                            ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag",
                             "release_risk_analysis", "test_verification"], {})
    assert release.reason == "Release-Risk 的代码理解、知识补充、风险评估和测试验证阶段均已完成或已尝试。"
    issue_memory = {**localization, "patchGeneration": {"llmGenerated": True},
                    "releaseRisk": {"riskLevel": "LOW"}}
    issue = policy.decide("ISSUE_TO_PATCH", issue_memory,
                          ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "bug_fix",
                           "test_verification", "release_risk_analysis"], {})
    assert issue.reason == "Issue-to-Patch 的代码定位、知识补充、修复、测试和发布风险阶段均已完成或已尝试。"


def test_orchestrator_helper_branch_contracts():
    policy = IncidentFixOrchestratorPolicy()
    assert policy._needs_agent_loop({}, [], {}, incident=False)
    assert not policy._needs_agent_loop({}, [policy.AGENT_LOOP], {}, incident=False)
    assert not policy._needs_agent_loop({}, [], {"agentLoopInvestigationEnabled": False}, incident=False)
    assert policy._needs_agent_loop({"opsEvidence": {"signal": 1}}, [], {}, incident=True)
    assert not policy._needs_agent_loop({}, [], {}, incident=True)
    assert policy._needs_repo_after_loop({"codeLocalization": {"localizationConfidence": "LOW", "targetFiles": []}},
                                         [policy.AGENT_LOOP])
    assert not policy._needs_repo_after_loop({"codeLocalization": {"targetFiles": ["App.java"]}},
                                             [policy.AGENT_LOOP, policy.REPO])
    assert policy._localization_blocking({"fixStrategy": {"strategyType": "NEED_MORE_EVIDENCE"}})
    assert policy._localization_blocking({"codeLocalization": {
        "localizationSuccess": False, "missingEvidence": ["method"]}})
    assert policy._localization_blocking({"codeLocalization": {
        "localizationReflection": {"blocking": True}}})
    assert not policy._localization_blocking({"codeLocalization": {"localizationSuccess": True}})
    assert policy._should_repair({}, {"allowPatchApply": True}, [], [])
    assert policy._should_repair({}, {}, ["bug_fix"], [])
    assert not policy._should_repair({"fixStrategy": {"shouldEnterCodeRepair": False}}, {}, [], [])
    assert not policy._should_repair({"codeLocalization": {
        "localizationConfidence": "LOW", "targetFiles": []}}, {}, [], [policy.AGENT_LOOP])
    assert policy._no_code_fix({"phase": "BUG_FIX_SKIPPED_NO_CODE_FIX"})
    assert policy._no_code_fix({"repairScope": {"scopeType": "NO_CODE_FIX"}})
    assert not policy._no_code_fix({"repairScope": {"scopeType": "FULL_FILE"}})
