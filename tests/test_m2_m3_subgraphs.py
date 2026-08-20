from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ops_autoagent import api
from ops_autoagent.config import Settings
from ops_autoagent.graphs import (CodeOpsGraph, IndependentReviewSubgraph, OpsEvidenceSubgraph,
                                  RepairProposalSubgraph, RepositoryInvestigationSubgraph,
                                  VerificationSubgraph)
from ops_autoagent.codeops.orchestrator import IncidentFixOrchestratorPolicy
from ops_autoagent.codeops.runtime import PatchProposal
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.observability import RuntimeObservability
from ops_autoagent.store import Store


def test_adversarial_eval_fixture_patch_is_opt_in_and_first_attempt_only():
    original = PatchProposal(summary="model", patches=[])
    state = {"repair_attempt": 0, "context": {"evaluationCaseId": "scope-cross-module-patch-blocked",
             "evaluationFixturePatchProposal": {"summary": "fixture", "patches": [
                 {"path": "src/main/java/com/example/order/PaymentClient.java", "old": "return true;", "new": "return false;"}
             ]}}}
    proposal, source = CodeOpsGraph._evaluation_fixture_proposal(state, original)
    assert source == "EVALUATION_ADVERSARIAL_FIXTURE"
    assert proposal.patches[0].path.endswith("PaymentClient.java")
    proposal, source = CodeOpsGraph._evaluation_fixture_proposal({**state, "repair_attempt": 1}, original)
    assert proposal is original and source == ""
    reflected = {**state, "context": {**state["context"], "incidentFixReflectionFailures": [{"round": 1}]}}
    proposal, source = CodeOpsGraph._evaluation_fixture_proposal(reflected, original)
    assert proposal is original and source == ""
    consumed = {**state, "context": {**state["context"], "evaluationFixturePatchConsumed": True}}
    proposal, source = CodeOpsGraph._evaluation_fixture_proposal(consumed, original)
    assert proposal is original and source == ""


@pytest.mark.asyncio
async def test_subgraphs_have_explicit_contracts_effect_boundaries_and_checkpoint(tmp_path: Path):
    input_state = {"taskId": "subgraph-contract", "targetFiles": ["src/app.py"],
                   "patchProposal": {"patches": []}, "verification": {"status": "SKIPPED"},
                   "review": {"reviewVerdict": "REVIEW_UNAVAILABLE", "patchDecision": "HUMAN_REVIEW"}}
    cases = [(OpsEvidenceSubgraph, "READ_ONLY"), (RepositoryInvestigationSubgraph, "READ_ONLY"),
             (RepairProposalSubgraph, "MANAGED_PATCH_SANDBOX_ONLY"), (VerificationSubgraph, "READ_ONLY"),
             (IndependentReviewSubgraph, "READ_ONLY")]
    for cls, boundary in cases:
        subgraph = cls()
        result = await subgraph.ainvoke(input_state, thread_id=f"{cls.__name__}-thread")
        output = result["output"]
        assert output["effectBoundary"] == boundary
        assert result["artifact_refs"] and output["artifactRefs"]
        checkpoint = await subgraph.checkpoint(f"{cls.__name__}-thread")
        assert checkpoint.values["output"]["subgraph"] == output["subgraph"]


@pytest.mark.asyncio
async def test_independent_reviewer_cannot_override_deterministic_patch_facts():
    reviewer = IndependentReviewSubgraph()
    result = await reviewer.ainvoke({"review": {"reviewVerdict": "ACCEPT", "patchDecision": "ACCEPT",
                                                 "riskLevel": "LOW"},
                                     "patchFacts": {"scopeGuardPassed": True, "compilePassed": False,
                                                    "testsPassed": True}},
                                    thread_id="review-facts")
    review = result["output"]["review"]
    assert review["reviewVerdict"] == "ACCEPT_WITH_HUMAN_REVIEW"
    assert review["patchDecision"] == "HUMAN_REVIEW"
    assert "Deterministic" in " ".join(review["mustReview"])


def test_codeops_agent_loop_compacts_fixture_evidence_and_uses_model_aliases():
    settings = Settings(CODEOPS_MODEL_FLASH="deepseek-v4-flash", CODEOPS_MODEL_PRO="deepseek-v4-pro")
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    prompt = graph._loop_prompt({"goal": "inspect order timeout", "repository": "samples/order-service",
                                 "maxTurns": 5, "context": {
                                     "fixtureEvidence": {"logs": {"raw": "sensitive-looking-data " * 20000},
                                                          "metrics": {"raw": "metric " * 20000}},
                                     "evaluationCaseId": "prompt-compaction"}}, [])
    assert settings.codeops_llm_flash_model == "deepseek-v4-flash"
    assert settings.codeops_llm_pro_model == "deepseek-v4-pro"
    assert len(prompt) <= 33000
    assert "fixtureEvidenceSummary" in prompt
    assert prompt.count("sensitive-looking-data") < 100


def test_repair_and_review_prompts_are_bounded():
    settings = Settings(CODEOPS_LLM_PROMPT_MAX_CHARS=32000)
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    oversized = "visible repository evidence " * 12000
    state = {
        "task": {"taskId": "prompt-bound", "taskType": "INCIDENT_TO_FIX", "goal": "fix race",
                  "repository": "samples/order-service", "changeRef": "working_tree"},
        "context": {"codeSearchMatches": [{"file": "src/main/java/App.java", "snippet": oversized}],
                     "repoBaselineSnapshot": {"src/main/java/App.java": "class App { int run(){ return 1; } }"},
                     "codeContextPack": {"raw": oversized}},
        "working_memory": {
            "opsEvidence": {"summary": oversized, "evidenceDetails": oversized},
            "fixStrategy": {"shouldEnterCodeRepair": True, "strategyType": "CODE_FIX"},
            "codeLocalization": {"targetFiles": ["src/main/java/App.java"],
                                  "localizationDecision": {"candidates": [{"snippet": oversized}]}},
            "engineeringKnowledge": {"matches": [{"title": "rule", "snippet": oversized}]},
        },
    }
    bugfix_prompt = graph._bugfix_prompt(state, 1)
    assert len(bugfix_prompt) <= 32000
    assert "class App" in bugfix_prompt
    assert len(graph._release_risk_prompt({"opsEvidence": {"summary": oversized},
                                           "patchGeneration": {"summary": oversized},
                                           "testVerification": {"summary": oversized},
                                           "codeLocalization": {"summary": oversized},
                                           "fixStrategy": {"strategyType": "CODE_FIX"},
                                           "knowledgeMatches": [{"snippet": oversized}],
                                           "patchFacts": {"patchGenerated": False},
                                           "changedFiles": [], "relatedTestFiles": [],
                                           "reflectionFailures": [], "baselineReport": {}})) <= 32000


@pytest.mark.asyncio
async def test_repair_and_review_use_router_output_budget(monkeypatch):
    settings = Settings(CODEOPS_LLM_PRO_ESCALATION_ENABLED=False,
                        openai_api_key="test-key", openai_base_url="https://example.invalid")
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    calls = []

    async def complete(prompt, **kwargs):
        calls.append(kwargs)
        return '{"rootCause":"visible cause","confidence":"HIGH","targetFiles":[],"reasoning":[],' \
               '"fileRewrites":[],"exactReplaceBlocks":[],"testSuggestions":[],"mavenCommands":[]}'

    monkeypatch.setattr(graph.llm, "complete", complete)
    await graph._execute_skill({
        "task": {"taskId": "budget-repair", "taskType": "INCIDENT_TO_FIX", "goal": "fix race",
                  "repository": "samples/order-service"},
        "context": {}, "working_memory": {"fixStrategy": {"strategyType": "CODE_FIX"},
                                             "codeLocalization": {"targetFiles": []}},
        "current_skill": "bug_fix", "round": 0, "tool_calls": 0, "steps": [],
    })
    assert calls[0]["max_tokens"] == 8192

    calls.clear()
    review = await graph._release_risk_agent(
        {"task": {"taskId": "budget-review", "taskType": "INCIDENT_TO_FIX", "goal": "review"},
         "context": {}, "repair_attempt": 0}, {}, {}, [])
    assert review["success"] is True
    assert calls[0]["max_tokens"] == 8192


def test_agent_loop_accepts_provider_tool_parameter_aliases():
    result = CodeOpsGraph._parse_loop_decision(
        '{"toolCalls":[{"toolName":"repo.read_file_snippet","params":'
        '{"path":"src/main/java/App.java","startLine":10,"endLine":30}}]}'
    )
    assert result["toolCalls"][0]["arguments"] == {
        "path": "src/main/java/App.java", "startLine": 10, "endLine": 30,
        "filePath": "src/main/java/App.java", "centerLine": 10, "radius": 20,
    }
    snake = CodeOpsGraph._parse_loop_decision(
        '{"toolCalls":[{"toolName":"repo.read_file_snippet","arguments":'
        '{"file_path":"src/main/java/App.java","start_line":10,"end_line":30}}]}'
    )
    assert snake["toolCalls"][0]["arguments"]["filePath"] == "src/main/java/App.java"


@pytest.mark.asyncio
async def test_llm_empty_provider_content_retries_once(monkeypatch):
    responses = iter(["", "OK"])
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": next(responses)}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *args, **kwargs):
            calls.append(kwargs["json"]["model"])
            return FakeResponse()

    monkeypatch.setattr("ops_autoagent.llm.httpx.AsyncClient", FakeClient)
    settings = Settings(openai_api_key="test-key", openai_base_url="https://example.invalid",
                        codeops_llm_empty_content_retries=1)
    assert await OpenAICompatibleClient(settings).complete("return JSON", model="deepseek-v4-flash") == "OK"
    assert calls == ["deepseek-v4-flash", "deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_llm_retries_transient_provider_error(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    class FakeClient:
        attempts = 0

        def __init__(self, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *args, **kwargs):
            calls.append(kwargs["json"]["model"])
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise httpx.ConnectError("temporary provider failure")
            return FakeResponse()

    monkeypatch.setattr("ops_autoagent.llm.httpx.AsyncClient", FakeClient)
    settings = Settings(openai_api_key="test-key", openai_base_url="https://example.invalid",
                        codeops_llm_empty_content_retries=1)
    assert await OpenAICompatibleClient(settings).complete("return JSON", model="deepseek-v4-flash") == "OK"
    assert calls == ["deepseek-v4-flash", "deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_deepseek_v4_uses_non_thinking_mode_for_structured_output(monkeypatch):
    bodies = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *args, **kwargs):
            bodies.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr("ops_autoagent.llm.httpx.AsyncClient", FakeClient)
    settings = Settings(openai_api_key="test-key", openai_base_url="https://example.invalid")
    assert await OpenAICompatibleClient(settings).complete("return JSON", model="deepseek-v4-flash") == "{}"
    assert bodies[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_llm_empty_provider_content_includes_finish_metadata(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {"content": ""}}],
                    "usage": {"completion_tokens": 2048,
                              "completion_tokens_details": {"reasoning_tokens": 2048}}}

    class FakeClient:
        def __init__(self, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("ops_autoagent.llm.httpx.AsyncClient", FakeClient)
    settings = Settings(openai_api_key="test-key", openai_base_url="https://example.invalid",
                        codeops_llm_empty_content_retries=0)
    with pytest.raises(RuntimeError, match="finishReason.*length"):
        await OpenAICompatibleClient(settings).complete("return JSON", model="deepseek-v4-flash")


@pytest.mark.asyncio
async def test_agent_loop_retries_invalid_structured_json(monkeypatch):
    settings = Settings(openai_api_key="test-key", codeops_llm_structured_output_retries=1)
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    responses = iter([
        '{"thoughtSummary":"truncated", "finalAnswer": {',
        '{"thoughtSummary":"ok", "finalAnswer":{"summary":"localized","targetFiles":["App.java"],'
        '"targetMethods":["App.run"],"shouldEnterCodeRepair":true,"localizationConfidence":"HIGH",'
        '"missingEvidence":[]}}',
    ])
    calls = []

    async def complete(prompt, **kwargs):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(graph.llm, "complete", complete)
    result = await graph._skill_agent_loop({
        "task": {"taskId": "structured-retry", "goal": "inspect App", "repository": "samples/order-service",
                  "maxToolCalls": 4, "focusAreas": []},
        "context": {}, "steps": [], "tool_calls": 0, "round": 0, "working_memory": {},
    })
    assert len(calls) == 2
    assert result["working_memory"]["agentLoopInvestigation"]["targetFiles"] == ["App.java"]
    assert result["working_memory"]["agentLoopInvestigation"]["strategyType"] == "CODE_FIX"
    assert result["working_memory"]["agentLoopInvestigation"]["scopeDecisionType"] == "STRICT_SINGLE_METHOD"


@pytest.mark.asyncio
async def test_repository_investigation_reuses_agent_targets_as_read_only_search_terms():
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    result = await graph._repo_understanding({
        "task": {"taskId": "repo-target-terms", "taskType": "INCIDENT_TO_FIX",
                  "goal": "investigate duplicate request race", "repository": "samples/order-service",
                  "maxToolCalls": 20, "changeRef": ""},
        "context": {},
        "working_memory": {"agentLoopInvestigation": {
            "targetFiles": ["src/main/java/com/example/order/OrderSubmitService.java"],
            "targetMethods": ["OrderSubmitService.submitFlashSale"],
        }},
        "tool_calls": 0, "steps": [],
    })
    matches = result["context"]["codeSearchMatches"]
    assert any(item.get("file") == "src/main/java/com/example/order/OrderSubmitService.java" for item in matches)


@pytest.mark.asyncio
async def test_repository_investigation_uses_ops_code_hints_after_agent_tool_denial():
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    result = await graph._repo_understanding({
        "task": {"taskId": "repo-ops-hints", "taskType": "INCIDENT_TO_FIX",
                  "goal": "investigate incident", "repository": "samples/order-service",
                  "maxToolCalls": 20, "changeRef": ""},
        "context": {},
        "working_memory": {"opsEvidence": {"codeHints": [
            "src/main/java/com/example/order/OrderSubmitService.java"]}},
        "tool_calls": 0, "steps": [],
    })
    assert any(item.get("file") == "src/main/java/com/example/order/OrderSubmitService.java"
               for item in result["context"]["codeSearchMatches"])


@pytest.mark.asyncio
async def test_repair_node_uses_task_focus_and_parent_decision_when_state_fields_are_reduced(monkeypatch,
                                                                                              tmp_path: Path):
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    called = False

    async def fake_execute(state):
        nonlocal called
        called = True
        return {"round": 1, "tool_calls": state.get("tool_calls", 0),
                "patch_proposal": {"summary": "no model patch in deterministic test", "patches": [], "tests": []},
                "bugfixAgent": {}, "context": state["context"], "steps": state["steps"]}

    monkeypatch.setattr(graph, "_execute_skill", fake_execute)
    result = await graph._skill_bug_fix({
        "task": {"taskId": "reduced-repair-state", "taskType": "INCIDENT_TO_FIX", "repository": str(tmp_path),
                  "focusAreas": ["incident", "bug_fix"]},
        "context": {"allowPatchApply": True, "allowTestPatchApply": True},
        "working_memory": {"fixStrategy": {"shouldEnterCodeRepair": False, "scopeDecisionType": "NO_CODE_FIX"},
                           "codeLocalization": {"targetFiles": ["src/main/java/App.java"],
                                                  "targetMethods": [], "localizationBlocking": False}},
        "steps": [{"selectedSkill": "repo_understanding"}], "executed_skills": [],
        "focus_areas": [], "decision": {"selectedSkill": "bug_fix"}, "current_skill": "",
        "round": 1, "tool_calls": 0,
    })
    assert called
    assert "BUG_FIX_SKIPPED_NO_CODE_FIX" not in result["steps"][-1]["rawEvidenceJson"]


@pytest.mark.asyncio
async def test_retry_feedback_is_structured_and_duplicate_digest_stops(tmp_path: Path):
    settings = Settings(codeops_reviewer_retry_enabled=True, codeops_max_repair_attempts=3,
                        ops_database_path=tmp_path / "ops.db")
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    state = {"task": {"taskId": "retry-task", "taskType": "INCIDENT_TO_FIX", "maxToolCalls": 20,
                       "maxRounds": 12},
             "context": {"releaseRiskRaw": {"reviewVerdict": "RETRY_REPAIR", "review": {
                 "reviewVerdict": "RETRY_REPAIR", "patchDecision": "RETRY_REPAIR",
                 "retryInstructions": {"failureType": "TEST_ASSERTION_FAILED", "mustFix": ["atomicity"],
                                        "mustAvoid": ["same patch"], "nextAttemptConstraints": ["new test"],
                                        "previousPatchDigest": "patch-1"}}}},
             "patch_digest": "patch-1", "repair_attempt": 0, "round": 1, "tool_calls": 1,
             "steps": [], "working_memory": {}}
    assert graph._route_finish(state) == "retry_repair"
    feedback = await graph._repair_feedback(state)
    assert feedback["repair_feedback"]["failureType"] == "TEST_ASSERTION_FAILED"
    assert feedback["repair_feedback"]["mustFix"] == ["atomicity"]
    assert feedback["repair_feedback"]["previousPatchDigest"] == "patch-1"
    blocked = {**state, "repair_attempt": 1}
    assert graph._retry_allowed(blocked) is False


@pytest.mark.asyncio
async def test_localization_blocked_never_enters_patch_sandbox(tmp_path: Path):
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    state = {"task": {"taskId": "blocked-localization", "taskType": "INCIDENT_TO_FIX",
                       "repository": str(tmp_path)},
             "context": {"allowPatchApply": True}, "working_memory": {
                 "fixStrategy": {"shouldEnterCodeRepair": True},
                 "codeLocalization": {"localizationBlocking": True, "missingEvidence": ["method"]}},
             "steps": [], "round": 0, "tool_calls": 0, "executed_skills": []}
    result = await graph._skill_bug_fix(state)
    raw = result["steps"][-1]["rawEvidenceJson"]
    assert result["status"] == "REQUIRES_REVIEW"
    assert "LOCALIZATION_BLOCKED" in raw
    assert not result.get("sandbox_result")


@pytest.mark.asyncio
async def test_no_code_fix_and_review_unavailable_are_stopping_routes(tmp_path: Path):
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    no_fix_state = {"task": {"taskId": "no-fix", "taskType": "INCIDENT_TO_FIX",
                              "repository": str(tmp_path)}, "context": {}, "working_memory": {
        "fixStrategy": {"shouldEnterCodeRepair": False, "strategyType": "NO_CODE_FIX"},
        "codeLocalization": {"targetFiles": []}}, "steps": [], "round": 0, "tool_calls": 0,
        "executed_skills": []}
    no_fix = await graph._skill_bug_fix(no_fix_state)
    assert "NO_CODE_FIX" in no_fix["steps"][-1]["rawEvidenceJson"]
    assert not no_fix.get("sandbox_result")

    unavailable = {"task": {"taskId": "unavailable", "taskType": "INCIDENT_TO_FIX"},
                   "context": {"releaseRiskRaw": {"reviewVerdict": "REVIEW_UNAVAILABLE"}},
                   "steps": [], "repair_attempt": 0, "round": 1, "tool_calls": 0}
    assert graph._route_finish(unavailable) == "summarize"
    finished = await graph._finish(unavailable)
    assert finished["status"] == "REVIEW_UNAVAILABLE"


@pytest.mark.asyncio
async def test_release_risk_contract_reaches_independent_reviewer_subgraph():
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    contract = {"reviewVerdict": "NO_CODE_FIX", "patchDecision": "NO_CODE_FIX",
                "riskLevel": "MEDIUM", "rootCauseAddressed": True, "scopeSafe": True,
                "testSufficient": True, "humanApprovalPoints": ["Observe runtime metrics"]}

    async def fake_release_risk(state):
        context = {**state["context"], "releaseRiskRaw": {"reviewContract": contract,
                                                            "reviewVerdict": "NO_CODE_FIX",
                                                            "patchDecision": "NO_CODE_FIX"}}
        return {"context": context, "steps": state["steps"]}

    graph._skill_release_risk = fake_release_risk
    state = {"task": {"taskId": "review-contract-plumbing", "taskType": "INCIDENT_TO_FIX",
                       "goal": "runtime-only incident"}, "context": {}, "working_memory": {},
             "steps": [], "repair_attempt": 0, "run_id": "run-review-contract"}
    result = await graph._skill_release_risk_with_subgraph(state)
    raw = result["context"]["releaseRiskRaw"]
    assert raw["review"]["reviewVerdict"] == "NO_CODE_FIX"
    assert raw["reviewVerdict"] == "NO_CODE_FIX"


def test_trace_redacts_prompts_and_sensitive_values():
    trace = api._build_task_trace({"taskId": "trace-task", "status": "FAILED", "steps": [{
        "stepNo": 1, "selectedSkill": "independent_review", "status": "FAILED",
        "resultSummary": "review failed", "rawEvidenceJson":
        '{"prompt":"token=secret-value","patchDraft":"Bearer abc","reviewVerdict":"REVIEW_UNAVAILABLE"}'
    }]})
    payload = str(trace)
    assert "secret-value" not in payload and "Bearer abc" not in payload
    assert trace["timeline"][0]["rawEvidence"]["prompt"]["redacted"] is True


def test_incident_policy_continues_read_only_localization_before_repair():
    policy = IncidentFixOrchestratorPolicy()
    memory = {
        "opsEvidence": {"evidenceCoverage": {"realEvidenceCoverage": 1.0}},
        "codeLocalization": {
            "targetFiles": ["src/main/java/com/example/order/OrderSubmitService.java"],
            "targetMethods": ["OrderSubmitService.submitFlashSale"],
            "localizationConfidence": "MEDIUM",
            "missingEvidence": ["current method body and regression assertion"],
            "localizationBlocking": False,
        },
        # This is the conservative answer observed from the first model pass.
        "fixStrategy": {"strategyType": "NO_CODE_FIX", "shouldEnterCodeRepair": False},
    }
    before_repo = policy.decide(
        "INCIDENT_TO_FIX", memory, [policy.OPS, policy.AGENT_LOOP],
        {"allowPatchApply": True, "allowTestPatchApply": True}, ["incident", "bug_fix"],
    )
    assert before_repo.selected_skill == policy.REPO

    after_repo = policy.decide(
        "INCIDENT_TO_FIX", memory, [policy.OPS, policy.AGENT_LOOP, policy.REPO],
        {"allowPatchApply": True, "allowTestPatchApply": True}, ["incident", "bug_fix"],
    )
    assert after_repo.selected_skill == policy.KNOWLEDGE

    no_code = {**memory, "fixStrategy": {"strategyType": "NO_CODE_FIX", "shouldEnterCodeRepair": False}}
    no_code_decision = policy.decide(
        "INCIDENT_TO_FIX", no_code, [policy.OPS, policy.AGENT_LOOP, policy.REPO],
        {}, ["incident", "runtime", "release_risk"],
    )
    assert no_code_decision.selected_skill == policy.RELEASE


def test_repair_node_carries_parent_code_fix_decision_into_effect_safe_node():
    graph = CodeOpsGraph(OpenAICompatibleClient(Settings()))
    state = {"context": {"allowPatchApply": True, "allowTestPatchApply": True},
             "executed_skills": ["ops_diagnosis", "agent_loop_investigation", "repo_understanding"],
             "focus_areas": ["incident", "bug_fix"], "working_memory": {}}
    localization = {"targetFiles": ["src/main/java/App.java"], "targetMethods": ["App.run"],
                    "localizationBlocking": False}
    assert graph._repair_override_allowed(state, localization, {"incident", "bug_fix"})
    assert graph._repair_override_allowed({**state, "current_skill": "bug_fix"},
                                          {"targetFiles": ["src/main/java/App.java"],
                                           "localizationBlocking": False}, set())
    assert not graph._repair_override_allowed({**state, "focus_areas": ["incident", "runtime"]}, localization,
                                              {"incident", "runtime"})


@pytest.mark.asyncio
async def test_runtime_metrics_are_durable_and_keep_zero_write_counter(tmp_path: Path):
    store = Store(tmp_path / "metrics.db")
    await store.initialize()
    recorder = RuntimeObservability(store)
    await recorder.metric("task-1", "unauthorized_target_repository_writes", 0, node="effect_boundary")
    await recorder.metric("task-1", "repair_attempt", 2, subgraph="repair_proposal")
    rows = await store.find("runtime_metrics", lambda item: item.get("taskId") == "task-1")
    assert {item["metricName"] for item in rows} == {"unauthorized_target_repository_writes", "repair_attempt"}
    assert next(item for item in rows if item["metricName"] == "unauthorized_target_repository_writes")["value"] == 0
