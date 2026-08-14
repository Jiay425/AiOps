from __future__ import annotations

import json
import os
import re
import uuid
import difflib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from ..codeops import (
    AgentLoopService, CodeOpsHookService, CodeOpsTaskDagService, ContextCompactor, EngineeringToolGateway,
    ErrorRecoveryPolicy, FailureDiagnosticParser,
    IncidentMemoryService, LlmCostControl,
    IncidentFixOrchestratorPolicy, ModelRouter, PatchDiffAnalysis, PatchProposal, PatchSandbox, PatchScopeGuard,
    PatchValidation, RepositoryToolkit, SecurityPolicy, TestRunner, TestVerificationService, ToolBudget,
)
from ..llm import OpenAICompatibleClient
from ..ops import EvidenceSignalExtractor, RunbookRagService
from ..schemas import CodeOpsTaskRequest, now_iso
from ..store import Store
from ..tools import ObservabilityTools


class CodeOpsState(TypedDict, total=False):
    task: dict[str, Any]
    status: str
    round: int
    tool_calls: int
    plan: list[str]
    current_skill: str
    steps: list[dict[str, Any]]
    context: dict[str, Any]
    approval_required: bool
    final_summary: str
    error: str
    evidence_graph: dict[str, Any]
    localization: dict[str, Any]
    patch_proposal: dict[str, Any]
    sandbox_result: dict[str, Any]
    verification: dict[str, Any]
    approval: dict[str, Any]
    working_memory: dict[str, Any]
    executed_skills: list[str]
    decision: dict[str, Any]
    focus_areas: list[str]
    stop_reason: str


class CodeOpsGraph:
    SKILLS = [
        {"skillId": "agent_loop_investigation", "name": "Agent Loop Investigation Skill", "description": "Run a model-driven read-only tool loop for repository investigation and evidence collection.", "supportedTaskTypes": ["CODE_REVIEW", "ISSUE_TO_PATCH", "INCIDENT_TO_FIX", "RELEASE_RISK", "AGENT_LOOP_DEBUG"], "requiredTools": ["repo.create_snapshot", "repo.search_text", "repo.read_file_snippet", "repo.git_diff", "repo.maven"], "riskLevel": "READ_ONLY"},
        {"skillId": "bug_fix", "name": "Bug Fix Skill", "description": "Analyze task goal and repository diff context, then produce localization clues, fix suggestions and a patch draft placeholder.", "supportedTaskTypes": ["ISSUE_TO_PATCH", "INCIDENT_TO_FIX", "BUG_FIX"], "requiredTools": ["repo.git_diff", "repo.find_tests", "repo.search_text"], "riskLevel": "READ_ONLY"},
        {"skillId": "engineering_knowledge_rag", "name": "Engineering Knowledge RAG Skill", "description": "Retrieve engineering docs, runbooks, review rules and historical postmortems.", "supportedTaskTypes": ["CODE_REVIEW", "ISSUE_TO_PATCH", "INCIDENT_TO_FIX", "RELEASE_RISK"], "requiredTools": ["knowledge.search"], "riskLevel": "READ_ONLY"},
        {"skillId": "fix_strategy_router", "name": "Fix Strategy Router", "description": "Classify an incident into code fix, config fix, capacity/runtime action, runbook action, or evidence gap before any patch is generated.", "supportedTaskTypes": ["INCIDENT_TO_FIX"], "requiredTools": [], "riskLevel": "READ_ONLY"},
        {"skillId": "ops_diagnosis", "name": "Ops Diagnosis Skill", "description": "Reuse AutoAgent diagnosis capability to collect metric, log, trace and runbook evidence.", "supportedTaskTypes": ["INCIDENT_TO_FIX"], "requiredTools": ["ops.query_prometheus", "ops.search_logs", "ops.query_trace", "knowledge.search"], "riskLevel": "READ_ONLY"},
        {"skillId": "pr_review", "name": "PR Review Skill", "description": "Review code changes for correctness, stability, performance, transaction and test risks.", "supportedTaskTypes": ["CODE_REVIEW"], "requiredTools": ["artifact.generate_review_report"], "riskLevel": "LOW_RISK_WRITE"},
        {"skillId": "release_risk_analysis", "name": "Release Risk Analysis Skill", "description": "Analyze release impact, regression focus, online observation metrics and rollback concerns from repo diff and engineering knowledge.", "supportedTaskTypes": ["RELEASE_RISK", "CODE_REVIEW", "INCIDENT_TO_FIX"], "requiredTools": ["repo.git_diff", "repo.find_tests", "knowledge.search"], "riskLevel": "READ_ONLY"},
        {"skillId": "repo_understanding", "name": "Repo Understanding Skill", "description": "Build repository, diff and code context for one engineering task.", "supportedTaskTypes": ["CODE_REVIEW", "ISSUE_TO_PATCH", "INCIDENT_TO_FIX", "RELEASE_RISK"], "requiredTools": ["repo.search_text", "repo.list_files", "repo.git_diff", "repo.find_tests"], "riskLevel": "READ_ONLY"},
        {"skillId": "test_verification", "name": "Test Verification Skill", "description": "Build a verification plan from changed files and related tests, including Maven commands and coverage gaps.", "supportedTaskTypes": ["ISSUE_TO_PATCH", "INCIDENT_TO_FIX", "CODE_REVIEW", "RELEASE_RISK", "BUG_FIX"], "requiredTools": ["repo.git_diff", "repo.find_tests"], "riskLevel": "READ_ONLY"},
    ]

    def __init__(self, llm: OpenAICompatibleClient, store: Store | None = None, checkpointer: Any | None = None):
        self.llm = llm
        self.settings = getattr(llm, "settings", None)
        default_model = getattr(self.settings, "openai_model", "default-model")
        flash_model = getattr(self.settings, "codeops_llm_flash_model", "") or default_model
        pro_model = getattr(self.settings, "codeops_llm_pro_model", "") or default_model
        self.model_router = ModelRouter(flash_model, pro_model,
                                        getattr(self.settings, "codeops_llm_pro_escalation_enabled", False))
        self.cost_control = LlmCostControl()
        self.compactor = ContextCompactor()
        self.failure_parser = FailureDiagnosticParser()
        self.recovery_policy = ErrorRecoveryPolicy()
        self.orchestrator = IncidentFixOrchestratorPolicy()
        self.dag = CodeOpsTaskDagService()
        self.hooks = CodeOpsHookService()
        self.engineering_tools = EngineeringToolGateway()
        self.test_verification_service = TestVerificationService(llm, self.settings, store)
        self.agent_loop_service = AgentLoopService(self.engineering_tools)
        self.observability = ObservabilityTools(self.settings) if self.settings else None
        self.runbook_rag = RunbookRagService(self.settings) if self.settings else None
        self.evidence_signal_extractor = EvidenceSignalExtractor()
        self.memory = IncidentMemoryService(store) if store else None
        builder = StateGraph(CodeOpsState)
        builder.add_node("plan", self._plan)
        builder.add_node("orchestrate", self._orchestrate)
        builder.add_node("ops_diagnosis", self._skill_ops_diagnosis)
        builder.add_node("agent_loop_investigation", self._skill_agent_loop)
        builder.add_node("repo_understanding", self._skill_repo_understanding)
        builder.add_node("engineering_knowledge_rag", self._skill_engineering_knowledge)
        builder.add_node("bug_fix", self._skill_bug_fix)
        builder.add_node("test_verification", self._skill_test_verification)
        builder.add_node("pr_review", self._skill_pr_review)
        builder.add_node("release_risk_analysis", self._skill_release_risk)
        builder.add_node("finish", self._finish)
        builder.add_node("mark_approval", self._mark_approval)
        builder.add_node("summarize", self._summarize)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "orchestrate")
        routes = {skill: skill for skill in (
            "ops_diagnosis", "agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag",
            "bug_fix", "test_verification", "pr_review", "release_risk_analysis",
        )}
        routes["STOP"] = "finish"
        builder.add_conditional_edges("orchestrate", self._route_decision, routes)
        for skill in routes:
            if skill != "STOP":
                builder.add_edge(skill, "orchestrate")
        builder.add_conditional_edges("finish", self._route_finish, {"approval": "mark_approval", "summarize": "summarize"})
        builder.add_edge("mark_approval", "summarize")
        builder.add_edge("summarize", END)
        self.graph = builder.compile(checkpointer=checkpointer or InMemorySaver())

    async def invoke(self, request: CodeOpsTaskRequest) -> CodeOpsState:
        task_id = str(uuid.uuid4())
        initial_context = dict(request.context or {})
        repository = request.repository
        if (repository is None or not repository.strip()) and initial_context.get("repository") is not None:
            repository = str(initial_context["repository"])
        snapshot: dict[str, str] = {}
        try:
            root = Path(repository or "").resolve()
            if root.exists():
                snapshot = RepositoryToolkit(root, ToolBudget(1)).create_snapshot()
        except (OSError, PermissionError, RuntimeError):
            snapshot = {}
        initial_context["repoBaselineSnapshot"] = snapshot
        requested_rounds = request.max_rounds if request.max_rounds is not None else 6
        task = {
            "taskId": task_id, "taskType": request.task_type.strip().upper(), "goal": request.goal,
            "repository": repository, "changeRef": request.change_ref,
            "focusAreas": request.focus_areas,
            "maxRounds": max(requested_rounds, 12) if request.task_type.strip().upper() == "INCIDENT_TO_FIX"
            else requested_rounds,
            "maxToolCalls": request.max_tool_calls if request.max_tool_calls is not None else 20,
            "createTime": now_iso(), "updateTime": now_iso(), "context": initial_context,
        }
        initial: CodeOpsState = {"task": task, "status": "RUNNING", "round": 0, "tool_calls": 0,
                                 "steps": [], "context": initial_context, "working_memory": {},
                                 "executed_skills": [], "focus_areas": list(request.focus_areas or [])}
        return await self.graph.ainvoke(initial, {"configurable": {"thread_id": task_id}, "recursion_limit": 100})

    async def resume(self, task_id: str, approved: bool, reason: str = "") -> CodeOpsState:
        return await self.graph.ainvoke(
            Command(resume={"approved": approved, "reason": reason, "time": now_iso()}),
            {"configurable": {"thread_id": task_id}, "recursion_limit": 100},
        )

    async def _plan(self, state: CodeOpsState) -> dict[str, Any]:
        task_type = state["task"]["taskType"]
        plans = {
            "INCIDENT_TO_FIX": ["ops_diagnosis", "agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "bug_fix", "test_verification", "release_risk_analysis"],
            "ISSUE_TO_PATCH": ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "bug_fix", "test_verification", "release_risk_analysis"],
            "RELEASE_RISK": ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "release_risk_analysis", "test_verification"],
            "CODE_REVIEW": ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "pr_review", "test_verification"],
        }
        plan = plans.get(task_type, plans["CODE_REVIEW"])
        return {"plan": plan}

    async def _orchestrate(self, state: CodeOpsState) -> dict[str, Any]:
        if state["tool_calls"] >= state["task"]["maxToolCalls"]:
            decision = {"decision": "STOP", "selectedSkill": "", "reason": "达到最大工具调用预算，任务停止。"}
        elif state["round"] >= state["task"]["maxRounds"]:
            decision = {"decision": "STOP", "selectedSkill": "", "reason": "达到最大执行轮数，任务停止。"}
        else:
            selected = self.orchestrator.decide(
                state["task"]["taskType"], state.get("working_memory", {}), state.get("executed_skills", []),
                state.get("context", {}), state.get("focus_areas", []),
            )
            decision = {"decision": selected.decision, "selectedSkill": selected.selected_skill, "reason": selected.reason}
        return {"decision": decision, "current_skill": decision["selectedSkill"],
                "stop_reason": decision["reason"] if decision["decision"] == "STOP" else ""}

    @staticmethod
    def _route_decision(state: CodeOpsState) -> str:
        return "STOP" if state["decision"]["decision"] == "STOP" else state["decision"]["selectedSkill"]

    async def _skill_ops_diagnosis(self, state: CodeOpsState) -> dict[str, Any]:
        context = state.get("context", {})
        service = str(context.get("serviceName") or context.get("service") or self._service_from_goal(
            state["task"].get("goal", "")) or "unknown-service")
        start = str(context.get("startTime") or (datetime.now() - timedelta(minutes=30)).isoformat())
        end = str(context.get("endTime") or datetime.now().isoformat())
        command = {"serviceName": service, "startTime": start, "endTime": end,
                   "problem": state["task"]["goal"], "endpoint": context.get("endpoint", ""),
                   "traceId": context.get("traceId", context.get("trace", "")), "maxStep": 6,
                   "sessionId": f"codeops-{uuid.uuid4()}",
                   "diagnosisId": context.get("opsDiagnosisId") or f"codeops-diagnosis-{uuid.uuid4()}"}
        fixture = str(context.get("fixtureCase") or "")
        if self.observability:
            metrics = await self.observability.prometheus(
                service, start, end, fixture, command["endpoint"], command["problem"])
            logs = await self.observability.elk(service, start, end, command["problem"], fixture)
            traces = await self.observability.skywalking(
                service, command["traceId"], start, end, fixture, command["endpoint"], command["problem"])
        else:
            metrics = {"source": "Prometheus", "available": False, "summary": "Prometheus unavailable"}
            logs = {"source": "Elasticsearch", "available": False, "summary": "Elasticsearch unavailable"}
            traces = {"source": "SkyWalking", "available": False, "summary": "SkyWalking unavailable"}
        signals = self.evidence_signal_extractor.extract(metrics, logs, traces, command)
        runbooks = await self.runbook_rag.search(" ".join(str(item.get("summary", "")) for item in signals), 5) \
            if self.runbook_rag else []
        available = sum(bool(item.get("available")) for item in (metrics, logs, traces))
        coverage = {"mode": "LIVE_GATEWAY", "externalSourceCount": 3, "realAvailableSources": available,
                    "realEvidenceCoverage": available / 3, "fixtureFallbackUsed": False,
                    "prometheusAvailable": bool(metrics.get("available")),
                    "elasticsearchAvailable": bool(logs.get("available")),
                    "skywalkingAvailable": bool(traces.get("available")), "runbookChunkHits": len(runbooks)}
        sources = ["Alertmanager webhook payload (live)",
                   f"Prometheus metrics (live:{'available' if metrics.get('available') else 'unavailable'})",
                   f"Elasticsearch logs (live:{'available' if logs.get('available') else 'unavailable'})",
                   f"SkyWalking traces (live:{'available' if traces.get('available') else 'unavailable'})",
                   f"Runbook RAG (matches={len(runbooks)})"]
        evidence_text = json.dumps({"metrics": metrics, "logs": logs, "traces": traces,
                                    "runbooks": runbooks}, ensure_ascii=False, default=str)
        code_hints = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_./\\-]+\.java", evidence_text)))
        diagnosis = {"diagnosisId": command["diagnosisId"], "sessionId": command["sessionId"],
                     "serviceName": service, "timeWindow": f"{start} ~ {end}", "traceId": command["traceId"],
                     "status": "LIVE_EVIDENCE_READY", "reportSummary": evidence_text[:1200],
                     "codeHints": code_hints, "evidenceSources": sources,
                     "evidenceDetails": {"prometheus": metrics, "elasticsearch": logs, "skywalking": traces,
                                         "evidenceSignals": signals, "runbookMatches": runbooks,
                                         "evidenceCoverage": coverage},
                     "evidenceCoverage": coverage, "evidenceProvenance": [
                         {"source": name, "available": bool(item.get("available")),
                          **(item.get("sourceMetadata") or {})}
                         for name, item in (("Prometheus", metrics), ("Elasticsearch", logs),
                                            ("SkyWalking", traces))]}
        raw = {"phase": "PHASE_4_OPS_DIAGNOSIS_SKILL", "command": command,
               "opsDiagnosis": diagnosis, "evidenceDetails": diagnosis["evidenceDetails"],
               "evidenceCoverage": coverage, "evidenceProvenance": diagnosis["evidenceProvenance"],
               "codeHints": code_hints}
        summary = (f"OpsDiagnosisSkill 已完成：service={service}，diagnosisId={command['diagnosisId']}，"
                   f"evidenceSources={len(sources)}，realEvidenceCoverage={coverage['realEvidenceCoverage']}，"
                   f"codeHints={len(code_hints)}")
        return self._record_skill(state, "ops_diagnosis", "opsEvidence", raw, summary,
                                  tool_calls=state["tool_calls"] + 4)

    async def _skill_agent_loop(self, state: CodeOpsState) -> dict[str, Any]:
        context = state.get("context", {})
        turns_value = context.get("agentLoopMaxTurns", 5)
        try:
            max_turns = max(1, min(int(turns_value), 12))
        except (TypeError, ValueError):
            max_turns = 5
        request = {"goal": (state["task"].get("goal") or "Investigate the repository and summarize relevant "
                            "code and tests.") + "\n\nUse read-only tools first. Summarize target files, likely "
                            "tests, and remaining uncertainty.",
                   "repository": state["task"].get("repository", ""),
                   "changeRef": state["task"].get("changeRef", ""),
                   "focusAreas": state["task"].get("focusAreas", []), "context": context,
                   "maxTurns": max_turns, "maxToolCalls": state["task"]["maxToolCalls"],
                   "task": state["task"]}

        async def model_client(loop_request: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
            if context.get("agentLoopDryRun") is True:
                return self._mock_loop_decision(loop_request, steps)
            if not self.llm.available:
                return {"thoughtSummary": "LLM client unavailable", "final": True,
                        "finalAnswer": "Agent loop model client is unavailable: "
                                       "OPENAI_API_KEY or OPENAI_BASE_URL is not configured"}
            prompt = self._loop_prompt(loop_request, steps)
            try:
                content = await self.llm.complete(prompt)
            except RuntimeError:
                content = await self.llm.complete(prompt)
            return self._parse_loop_decision(content)

        result = await self.agent_loop_service.run(request, model_client)
        structured = self._json_payload(result.get("finalAnswer", ""))
        target_files = self._string_list(structured.get("targetFiles") or structured.get("rootCauseCandidateFiles"))
        target_methods = self._string_list(structured.get("targetMethods"))
        should_repair = self._boolean(structured.get("shouldEnterCodeRepair"), bool(target_files))
        fix_strategy = str(structured.get("fixStrategy") or ("CODE_FIX" if should_repair else "NO_CODE_FIX"))
        scope = str(structured.get("scopeDecision") or (
            "NO_CODE_FIX" if not should_repair else "STRICT_SINGLE_METHOD" if len(target_methods) == 1
            else "FULL_FILE" if len(target_files) == 1 else "CROSS_FILE"))
        raw = {"phase": "PHASE_AGENT_LOOP_INVESTIGATION", "status": result["status"],
               "summary": structured.get("summary", result.get("finalAnswer", "")),
               "finalAnswer": result.get("finalAnswer", ""), "structuredFinalAnswer": structured,
               "stopReason": result.get("stopReason", ""), "turns": result.get("turns", 0),
               "trace": result.get("trace", []), "targetFiles": target_files,
               "directEvidenceFiles": self._string_list(structured.get("directEvidenceFiles")),
               "relatedFiles": self._string_list(structured.get("relatedFiles")),
               "rootCauseCandidateFiles": self._string_list(structured.get("rootCauseCandidateFiles")) or target_files,
               "doNotModifyFiles": self._string_list(structured.get("doNotModifyFiles")),
               "targetMethods": target_methods, "candidateMethods": target_methods,
               "fixStrategy": fix_strategy, "strategyType": fix_strategy, "scopeDecisionType": scope,
               "rootCauseLocationType": structured.get("rootCauseLocationType", "UNKNOWN"),
               "primarySymptomLocation": structured.get("primarySymptomLocation", ""),
               "supportingCodeEvidence": self._string_list(structured.get("supportingCodeEvidence")),
               "negativeEvidence": self._string_list(structured.get("negativeEvidence")),
               "reasoning": structured.get("reasoning", structured.get("summary", "")),
               "recommendedTests": self._string_list(structured.get("recommendedTests")),
               "shouldEnterCodeRepair": should_repair,
               "localizationConfidence": structured.get("localizationConfidence",
                                                          "MEDIUM" if result["status"] == "COMPLETED" else "LOW"),
               "missingEvidence": self._string_list(structured.get("missingEvidence"))}
        summary = ("Agent loop investigation completed: " + str(result.get("finalAnswer", "")) if
                   result["status"] == "COMPLETED" else "Agent loop investigation stopped: "
                   + str(result.get("stopReason") or result["status"]))
        loop_context = state["task"].get("context")
        update = self._record_skill(state, "agent_loop_investigation", "agentLoopInvestigation", raw, summary,
                                    status="SUCCESS" if result["status"] == "COMPLETED" else result["status"],
                                    context=loop_context if isinstance(loop_context, dict) else state["context"],
                                    tool_calls=state["tool_calls"] + len(result.get("steps", [])))
        memory = {**update["working_memory"], "codeLocalization": raw,
                  "fixStrategy": {"strategyType": fix_strategy, "fixStrategy": fix_strategy,
                                  "shouldEnterCodeRepair": should_repair, "scopeDecisionType": scope,
                                  "localizationBlocking": False}}
        update["working_memory"] = memory
        update["context"] = {**update["context"], "incidentFixWorkingMemory": memory}
        return update

    async def _skill_repo_understanding(self, state: CodeOpsState) -> dict[str, Any]:
        repo_update = await self._repo_understanding(state)
        merged = {**state, **repo_update}
        localization_update = await self._localize(merged)
        localization = localization_update["localization"]
        diff_context = localization_update["context"].get("diffContext", {})
        loop = state.get("working_memory", {}).get("agentLoopInvestigation", {})
        target_files = loop.get("targetFiles") or localization["targetFiles"]
        target_methods = loop.get("targetMethods") or []
        memory_value = {
            **localization,
            "phase": "PHASE_2_CODE_LOCALIZATION", "repositoryPath": diff_context.get("repositoryPath", ""),
            "changeRef": diff_context.get("changeRef", "working_tree"),
            "changedFiles": diff_context.get("changedFiles", []),
            "relatedTestFiles": diff_context.get("relatedTestFiles", []), "hunkCount": 0,
            "codeHints": self._search_terms(state["task"]["goal"], state["context"]),
            "codeSearchMatches": localization_update["context"].get("codeSearchMatches", []),
            "codeSnippets": [], "evidenceGraph": repo_update["evidence_graph"],
            "evidenceGraphSummary": repo_update["evidence_graph"].get("summary", ""),
            "evidenceGraphRankedCodeNodes": repo_update["evidence_graph"].get("nodes", []),
            "diffSummary": diff_context.get("diffSummary", ""),
            "diffAvailable": bool(diff_context.get("diffAvailable")),
            "localizationSuccess": not localization["blocking"], "localizationFallback": False,
            "localizationConfidence": loop.get("localizationConfidence", "LOW"),
            "strategyType": loop.get("strategyType", "NEED_MORE_EVIDENCE"),
            "shouldEnterCodeRepair": loop.get("shouldEnterCodeRepair", bool(target_files)),
            "targetFiles": target_files, "targetMethods": target_methods,
            "primarySuspectMethod": target_methods[0] if target_methods else "",
            "candidateFiles": [item.get("file") for item in localization.get("candidates", [])],
            "candidateMethods": target_methods, "scopeSuggestion": loop.get("scopeDecisionType", ""),
            "scopeConfidence": loop.get("localizationConfidence", "LOW"),
            "expandable": loop.get("scopeDecisionType") == "CROSS_FILE",
            "expansionBoundary": loop.get("relatedFiles", []),
            "suspiciousLocations": localization.get("candidates", []),
            "localizationReasoning": self._string_list(loop.get("reasoning")),
            "missingEvidence": (["code candidates"] if localization["blocking"] else loop.get("missingEvidence", [])),
            "localizationError": "", "localizationBlocking": localization["blocking"],
        }
        return self._record_skill(
            state, "repo_understanding", "codeLocalization", memory_value,
            localization_update["steps"][-1]["resultSummary"],
            status="SUCCESS" if diff_context.get("diffAvailable") else "NO_DIFF",
            context=localization_update["context"], tool_calls=repo_update["tool_calls"],
            extra={"localization": localization, "evidence_graph": repo_update["evidence_graph"]},
        )

    async def _skill_engineering_knowledge(self, state: CodeOpsState) -> dict[str, Any]:
        root = Path(state["task"].get("repository") or ".").resolve()
        terms = self._search_terms(state["task"]["goal"], state["context"])
        hits: list[dict[str, Any]] = []
        used = state["tool_calls"]
        try:
            budget = ToolBudget(state["task"]["maxToolCalls"], used)
            all_hits = RepositoryToolkit(root, budget).search(terms, limit=100)
            hits = [item for item in all_hits if Path(item["file"]).suffix.lower() in {".md", ".txt", ".yml", ".yaml"}][:20]
            used = budget.used_calls
        except Exception:
            pass
        knowledge = {"query": state["task"]["goal"], "hits": hits, "source": "REPOSITORY_KNOWLEDGE"}
        return self._record_skill(state, "engineering_knowledge_rag", "engineeringKnowledge", knowledge,
                                  f"检索到 {len(hits)} 条工程知识。", tool_calls=used)

    async def _legacy_skill_bug_fix(self, state: CodeOpsState) -> dict[str, Any]:
        strategy = state.get("working_memory", {}).get("fixStrategy", {})
        should_repair = strategy.get("shouldEnterCodeRepair", True)
        if should_repair is False:
            scope_type = strategy.get("scopeDecisionType") or strategy.get("scopeType") or "NO_CODE_FIX"
            strategy_type = strategy.get("strategyType") or strategy.get("fixStrategy") or "NO_CODE_FIX"
            reasoning = strategy.get("scopeReasoning") or strategy.get("reasoning") or ""
            raw = {"phase": "BUG_FIX_SKIPPED_NO_CODE_FIX", "repairScope": {
                "scopeType": scope_type, "strategyType": strategy_type, "scopeReasoning": reasoning},
                "verdict": "No code patch needed — this is a runtime/config/capacity incident."}
            return self._record_skill(
                state, "bug_fix", "patchGeneration", {**raw, "noCodeFix": True},
                f"Incident triage classified as {strategy_type} — no source code repair needed. Reason: {reasoning}",
                status="NO_DIFF")
        proposal_update = await self._execute_skill({**state, "current_skill": "bug_fix"})
        merged = {**state, **proposal_update}
        sandbox_update: dict[str, Any] = {}
        if proposal_update["patch_proposal"].get("patches") and state["context"].get("allowPatchApply") is True:
            sandbox_update = await self._sandbox_patch(merged)
            merged.update(sandbox_update)
        proposal = proposal_update["patch_proposal"]
        sandbox_result = sandbox_update.get("sandbox_result", {})
        changed_files = [str(item.get("path")) for item in proposal.get("patches", [])
                         if isinstance(item, dict) and item.get("path")]
        validation = sandbox_result.get("validation", {}) if isinstance(sandbox_result, dict) else {}
        patch_memory = {
            **proposal,
            "phase": "PHASE_3_LLM_BUG_FIX",
            "repositoryPath": state["task"].get("repository") or "",
            "sandboxRepositoryPath": sandbox_result.get("sandbox") if isinstance(sandbox_result, dict) else None,
            "codeLocalization": state.get("working_memory", {}).get("codeLocalization", {}),
            "localizationDecision": state.get("working_memory", {}).get("agentLoopInvestigation", {}),
            "llmGenerated": bool(proposal.get("patches")),
            "patchDraft": json.dumps(proposal, ensure_ascii=False, default=str),
            "changedFiles": changed_files,
            "patchQuality": {
                "requiresHumanApproval": False,
                "minimalChangeScore": 100 if len(changed_files) <= 1 else max(0, 100 - (len(changed_files) - 1) * 15),
                "staticSafetyPassed": bool(validation.get("valid", not changed_files)),
            },
            "patchSandbox": {
                **(sandbox_result if isinstance(sandbox_result, dict) else {}),
                "isolated": bool(sandbox_result.get("sandbox")) if isinstance(sandbox_result, dict) else False,
            },
            "patchScopeGuard": sandbox_result.get("scopeGuard", {}) if isinstance(sandbox_result, dict) else {},
            "patchValidation": sandbox_result.get("validation", {}) if isinstance(sandbox_result, dict) else {},
            "patchDiffAnalysis": sandbox_result.get("diffAnalysis", {}) if isinstance(sandbox_result, dict) else {},
            "patchApply": {"applied": bool(sandbox_result.get("success")),
                           "errorMessage": "; ".join(sandbox_result.get("errors", []))}
            if isinstance(sandbox_result, dict) else {"applied": False, "errorMessage": ""},
            "compileGate": {},
            "noCodeFix": not bool(proposal.get("patches")) and not should_repair,
            "sandbox": sandbox_result,
        }
        return self._record_skill(
            state, "bug_fix", "patchGeneration", patch_memory, proposal.get("summary", ""),
            context=merged.get("context", state["context"]), tool_calls=merged.get("tool_calls", state["tool_calls"]),
            round_no=proposal_update["round"],
            extra={"patch_proposal": proposal, "sandbox_result": sandbox_update.get("sandbox_result", {})},
        )

    async def _skill_bug_fix(self, state: CodeOpsState) -> dict[str, Any]:
        """LangGraph implementation of the Java BugFixSkill's guarded sandbox repair workflow."""
        strategy = state.get("working_memory", {}).get("fixStrategy", {})
        localization = state.get("working_memory", {}).get("codeLocalization", {})
        should_repair = strategy.get("shouldEnterCodeRepair", True)
        repair_scope = self._repair_scope(strategy, localization)
        if should_repair is False or repair_scope["scopeType"] == "NO_CODE_FIX":
            raw = {"phase": "BUG_FIX_SKIPPED_NO_CODE_FIX", "repairScope": repair_scope,
                   "verdict": "No code patch needed — this is a runtime/config/capacity incident."}
            return self._record_skill(state, "bug_fix", "patchGeneration", {**raw, "noCodeFix": True},
                                      "Incident triage classified as " + repair_scope["strategyType"] +
                                      " — no source code repair needed. Reason: " + repair_scope["scopeReasoning"],
                                      status="NO_DIFF")
        proposal_update = await self._execute_skill({**state, "current_skill": "bug_fix"})
        proposal = self._proposal(proposal_update["patch_proposal"])
        agent = self._release_mapping(proposal_update.get("bugfixAgent"))
        repository = str(state["task"].get("repository") or "")
        unified_patch = self._unified_patch(repository, proposal)
        guard = PatchScopeGuard().validate(repository, proposal, repair_scope)
        validation = PatchValidation().validate(repository, unified_patch)
        quality = PatchDiffAnalysis().analyze(unified_patch, validation, guard)
        incident = state["task"]["taskType"] == "INCIDENT_TO_FIX"
        guard_blocked = not guard["passed"]
        safety_blocked = not quality["staticSafetyPassed"] and not self._boolean(state["context"].get("allowSensitivePatch"), False)
        sandbox_result: dict[str, Any] = {}
        patch_apply = {"requested": False, "applied": False, "checkPassed": False, "repositoryPath": repository,
                       "command": [], "exitCode": -1, "output": "", "errorMessage": ""}
        source_validation = {"valid": True, "errors": [], "checkedFiles": []}
        compile_gate: dict[str, Any] = {"requested": False, "success": True,
                                        "reason": "patch not applied"}
        rolled_back = False
        hook_context = dict(proposal_update.get("context", state["context"]))
        if not guard_blocked and not safety_blocked and proposal.patches and self._boolean(state["context"].get("allowPatchApply"), False):
            merged = {**state, **proposal_update, "context": {**hook_context, "repairScope": repair_scope}}
            sandbox_update = await self._sandbox_patch(merged)
            sandbox_result = sandbox_update.get("sandbox_result", {})
            hook_context = dict(sandbox_update.get("context", hook_context))
            sandbox_path = str(sandbox_result.get("sandbox") or "")
            patch_apply = {"requested": True, "applied": bool(sandbox_result.get("success")),
                           "checkPassed": bool(sandbox_result.get("success")), "repositoryPath": sandbox_path or repository,
                           "command": sandbox_result.get("command", []), "exitCode": 0 if sandbox_result.get("success") else -1,
                           "output": sandbox_result.get("diff", ""),
                           "errorMessage": "; ".join(sandbox_result.get("errors", []))}
            if patch_apply["applied"]:
                source_validation = self._validate_java_sources(sandbox_path, validation.get("touchedFiles", []))
                compile_gate = await self._compile_gate(sandbox_path, source_validation["valid"])
                hook_context, _ = self.hooks.emit(hook_context, "AFTER_COMPILE", "BUG_FIX", "after compile gate", compile_gate)
                if not compile_gate["success"]:
                    rolled_back = self._rollback_sandbox(sandbox_path, proposal)
        elif self._boolean(state["context"].get("allowPatchApply"), False) and not proposal.patches:
            patch_apply["errorMessage"] = "No concrete LLM patch was generated."
        elif self._boolean(state["context"].get("allowPatchApply"), False):
            patch_apply["errorMessage"] = "Blocked by PatchScopeGuard" if guard_blocked else "Blocked by PatchStaticSafety"
        sandbox = {"enabled": bool(sandbox_result.get("sandbox")), "isolated": bool(sandbox_result.get("sandbox")),
                   "mode": sandbox_result.get("mode", ""), "originalRepositoryPath": repository,
                   "sandboxRepositoryPath": sandbox_result.get("sandbox", ""),
                   "branchName": sandbox_result.get("branch_name", ""), "command": sandbox_result.get("command", []),
                   "errorMessage": patch_apply["errorMessage"]}
        status = ("FAILED" if guard_blocked or safety_blocked or (patch_apply["applied"] and not compile_gate["success"]) or
                  (incident and not proposal.patches) else "SUCCESS" if proposal.patches else "NO_DIFF")
        if state["context"].get("allowPatchApply") is True and proposal.patches and not patch_apply["applied"]:
            status = "FAILED"
        changed_files = [patch.path for patch in proposal.patches]
        raw = {"phase": "PHASE_5_BUG_FIX_PATCH_PROPOSAL", "repairScope": repair_scope,
               "patchScopeGuard": guard, "repositoryPath": repository, "originalRepositoryPath": repository,
               "sandboxRepositoryPath": sandbox["sandboxRepositoryPath"], "patchSandbox": sandbox,
               "patchDiffAnalysis": quality, "patchQuality": {key: quality.get(key) for key in (
                   "minimalChangeScore", "staticSafetyPassed", "scopeAligned", "testsChanged",
                   "requiresHumanApproval", "qualityWarnings")}, "changeRef": state["task"].get("changeRef") or "working_tree",
               "changedFiles": changed_files, "suspiciousLocations": self._string_list(localization.get("targetFiles")),
               "diagnosisClues": self._string_list(localization.get("supportingCodeEvidence")),
               "fixSuggestions": self._release_string_list(agent.get("testSuggestions")) or proposal.tests or ([proposal.rationale] if proposal.rationale else []),
               "patchDraft": str(agent.get("unifiedDiffPatch") or unified_patch),
               "exactReplaceBlocks": self._release_list(agent.get("exactReplaceBlocks")),
               "exactReplaceApply": {"requested": False, "appliedFiles": [], "failedBlocks": []},
               "rootCause": str(agent.get("rootCause") or localization.get("summary") or "LLM 未确认具体根因"),
               "confidence": str(agent.get("confidence") or ("MEDIUM" if proposal.patches else "LOW")),
               "reflectionDiagnosis": self._release_mapping(agent.get("reflectionDiagnosis")) or state["context"].get("failureDiagnostic", {}),
               "scopeDecision": self._release_mapping(agent.get("scopeDecision")) or repair_scope,
               "modelRouting": self._release_mapping(agent.get("modelRouting")),
               "llmUsage": self._release_mapping(agent.get("llmUsage")) or proposal_update.get("context", {}).get("llmUsage", {}),
               "repairPlan": {"scope": repair_scope, "nextActions": ["交给 Test Verification Skill 生成验证计划"]},
               "permissionPolicy": SecurityPolicy().governance_summary(), "llmGenerated": bool(proposal.patches),
               "llmErrorMessage": str(agent.get("errorMessage") or ""),
               "testSuggestions": self._release_string_list(agent.get("testSuggestions")) or proposal.tests,
               "mavenCommands": self._release_string_list(agent.get("mavenCommands")),
               "testUnifiedDiffPatch": str(agent.get("testUnifiedDiffPatch") or ""),
               "testFileRewrites": self._release_list(agent.get("testFileRewrites")), "codeSnippets": [],
               "codeContextPack": {"primaryFiles": repair_scope["targetFiles"], "relatedTests": []},
               "codeSearchMatches": state["context"].get("codeSearchMatches", []),
               "verificationHints": ["先运行推荐的定向测试", "最后运行模块级 compile/test 作为兜底验证"],
               "patchValidation": validation, "patchApply": patch_apply, "sourceValidation": source_validation,
               "compileGate": compile_gate, "patchRolledBack": rolled_back,
               "patchRollbackReason": "补丁已应用但编译失败，已回滚沙箱源码，避免坏补丁残留在验证工作区。" if rolled_back else "",
               "sandbox": sandbox_result, "noCodeFix": False,
               "repairObservations": hook_context.get("repairObservations", [])}
        summary = (f"已生成 Bug 修复分析骨架：定位线索 {len(raw['suspiciousLocations'])} 条，修复建议 "
                   f"{len(raw['fixSuggestions'])} 条，LLM patch={str(bool(proposal.patches)).lower()}，"
                   f"patchApplied={str(patch_apply['applied']).lower()}，compileGate="
                   f"{'SKIPPED' if not compile_gate['requested'] else str(compile_gate['success']).lower()}，"
                   f"patchRolledBack={str(rolled_back).lower()}。")
        return self._record_skill(state, "bug_fix", "patchGeneration", raw, summary, status=status,
                                  context=hook_context, tool_calls=proposal_update["tool_calls"],
                                  round_no=proposal_update["round"],
                                  extra={"patch_proposal": proposal.to_dict(), "sandbox_result": sandbox_result})

    def _repair_scope(self, strategy: dict[str, Any], localization: dict[str, Any]) -> dict[str, Any]:
        scope_type = str(strategy.get("scopeDecisionType") or strategy.get("scopeType") or
                         localization.get("scopeDecisionType") or localization.get("scopeDecision") or "FULL_FILE").upper()
        strategy_type = str(strategy.get("strategyType") or strategy.get("fixStrategy") or
                            localization.get("fixStrategy") or ("NO_CODE_FIX" if scope_type == "NO_CODE_FIX" else "CODE_FIX"))
        files = self._string_list(localization.get("targetFiles") or localization.get("rootCauseCandidateFiles"))
        methods = self._string_list(localization.get("targetMethods") or localization.get("candidateMethods"))
        return {"scopeType": scope_type, "strategyType": strategy_type, "targetFiles": files,
                "targetMethods": methods, "candidateMethods": self._string_list(localization.get("candidateMethods")),
                "scopeConfidence": localization.get("localizationConfidence", "MEDIUM"),
                "scopeReasoning": str(strategy.get("scopeReasoning") or strategy.get("reasoning") or ""),
                "localizationDecision": localization}

    @staticmethod
    def _unified_patch(repository: str, proposal: PatchProposal) -> str:
        root, parts = Path(repository or ".").resolve(), []
        for patch in proposal.patches:
            file = (root / patch.path).resolve()
            try:
                file.relative_to(root)
                current = file.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            if patch.old and patch.old in current:
                changed = current.replace(patch.old, patch.new, 1)
                parts.extend(difflib.unified_diff(current.splitlines(), changed.splitlines(),
                                                  fromfile=f"a/{patch.path}", tofile=f"b/{patch.path}", lineterm=""))
        return "\n".join(parts) + ("\n" if parts else "")

    @staticmethod
    def _validate_java_sources(repository: str, touched: list[str]) -> dict[str, Any]:
        root, errors, checked = Path(repository or ".").resolve(), [], []
        for path in touched:
            if not str(path).endswith(".java"):
                continue
            file = (root / path).resolve()
            try:
                file.relative_to(root)
                content = file.read_text(encoding="utf-8")
            except OSError:
                continue
            checked.append(path)
            balance = 0
            for char in content:
                if char == "{":
                    balance += 1
                elif char == "}":
                    balance -= 1
            if balance:
                errors.append(f"{path}: Java brace balance is {balance}")
            if content.rfind("}") >= 0 and content[content.rfind("}") + 1:].strip():
                errors.append(f"{path}: non-whitespace content after final class brace")
        return {"valid": not errors, "errors": errors, "checkedFiles": checked}

    async def _compile_gate(self, repository: str, source_valid: bool) -> dict[str, Any]:
        if not source_valid:
            return {"requested": False, "success": False, "reason": "source validation failed before compile gate"}
        command = ["mvn.cmd" if os.name == "nt" else "mvn", "-q", "-DskipTests", "compile"]
        try:
            result = await TestRunner().run(repository, command,
                                            max(1, int(getattr(self.settings, "codeops_bugfix_compile_timeout_ms", 300000)) // 1000))
            return {"requested": True, "success": result.status == "PASSED", "command": result.command,
                    "exitCode": result.exit_code, "costMillis": result.duration_ms, "output": result.output[:4000]}
        except Exception as exc:
            return {"requested": True, "success": False, "command": command, "exitCode": -1,
                    "costMillis": 0, "output": str(exc)}

    @staticmethod
    def _rollback_sandbox(repository: str, proposal: PatchProposal) -> bool:
        root, rolled = Path(repository or ".").resolve(), False
        for patch in proposal.patches:
            file = (root / patch.path).resolve()
            try:
                file.relative_to(root)
                content = file.read_text(encoding="utf-8")
                if patch.new in content:
                    file.write_text(content.replace(patch.new, patch.old, 1), encoding="utf-8")
                    rolled = True
            except OSError:
                continue
        return rolled

    def _bugfix_prompt(self, state: CodeOpsState, reflection_round: int) -> str:
        context, memory = state["context"], state.get("working_memory", {})
        localization = self._release_mapping(memory.get("codeLocalization"))
        repair_scope = self._repair_scope(self._release_mapping(memory.get("fixStrategy")), localization)
        diagnostics = self._release_list(context.get("incidentFixReflectionDiagnostics") or
                                         context.get("incidentFixReflectionFailures"))
        reflection_lines = []
        for index, item in enumerate(diagnostics, 1):
            diagnostic = self._release_mapping(item)
            reflection_lines.append(f"Round {index} FAILED: {diagnostic.get('failureType', 'UNKNOWN')}")
            reflection_lines.extend("MUST FIX: " + value for value in self._release_string_list(diagnostic.get("mustFix")))
            reflection_lines.extend("MUST AVOID: " + value for value in self._release_string_list(diagnostic.get("mustAvoid")))
            reflection_lines.extend("CONSTRAINT: " + value for value in self._release_string_list(diagnostic.get("nextAttemptConstraints")))
        reflection_block = ("\n!!! REFLECTION ROUND — PREVIOUS ATTEMPT(S) FAILED !!!\n" +
                            "The following failures occurred. Your new patch MUST fix them.\n" +
                            "\n".join(reflection_lines) +
                            "\n!!! END REFLECTION — GENERATE A DIFFERENT PATCH !!!\n") if reflection_lines else ""
        snippets = self._release_list(context.get("codeSearchMatches"))[:max(1, int(
            getattr(self.settings, "codeops_agent_bugfix_max_snippets", 12) or 12))]
        knowledge = self._release_knowledge_matches(memory)[:max(1, int(
            getattr(self.settings, "codeops_agent_bugfix_max_knowledge", 5) or 5))]
        agent_input = {
            "taskId": state["task"].get("taskId", ""), "taskType": state["task"].get("taskType", ""),
            "goal": state["task"].get("goal", ""), "repositoryPath": state["task"].get("repository", ""),
            "changeRef": state["task"].get("changeRef", ""), "opsDiagnosis": memory.get("opsEvidence", {}),
            "diagnosisClues": self._release_string_list(localization.get("supportingCodeEvidence")),
            "suspiciousLocations": self._release_string_list(localization.get("targetFiles")),
            "repairScope": repair_scope, "repairPlan": {"scope": repair_scope}, "codeSearchMatches": snippets,
            "codeSnippets": snippets, "codeContextPack": context.get("codeContextPack", {}),
            "knowledgeMatches": knowledge, "reflectionFailures": self._release_list(context.get("incidentFixReflectionFailures")),
            "reflectionDiagnostics": diagnostics, "memoryHints": self._release_list(context.get("memoryHints")),
        }
        return """You are a senior Java backend incident-fix agent.

Your task is to analyze a production incident from telemetry evidence and real repository code snippets,
then propose the smallest safe production fix and minimal regression test plan in one response.

Important rules:
- Output only JSON and ground every claim in opsDiagnosis, codeContextPack, codeSearchMatches, codeSnippets, or knowledgeMatches.
- Do not invent files, methods, fields, APIs, dependencies, metrics, logs, line numbers, constructors, or test helpers.
- repairPlan and repairScope are the plan-and-execute contract. initial scope is a recommendation; candidate scope is the
  maximum legal expansion boundary. Choose KEEP_SCOPE unless visible code proves EXPAND_SCOPE is necessary.
- STRICT_SINGLE_METHOD may change only finalTargetMethods; MULTI_METHOD changes targets plus signatures/imports;
  FULL_FILE is allowed only within candidate files; NO_CODE_FIX must return empty production and test patches.
- Prefer fileRewrites with complete visible Java file content. Preserve all non-target methods byte-for-byte. Use
  exactReplaceBlocks only for exact visible oldText. Use unifiedDiffPatch only when a complete file rewrite is unsafe.
- A unified diff must use --- a/path, +++ b/path and real @@ hunks. Do not use markdown fences or prose around patches.
- For INCIDENT_TO_FIX CODE_FIX on Maven/JUnit, include a concrete JUnit 5 test rewrite when visible APIs suffice.
- Preserve public signatures and normal behavior. For state-owner check-then-act races, fix the state-owning service,
  not only its caller. Tests must use only repository dependencies and visible constructors/APIs.
- Maven commands must include compile, targeted generated/existing tests, then module tests.
- Reflection diagnostics are mandatory feedback. Fix their failureType, mustFix, mustAvoid and constraints; do not resubmit
  the previous failed patch. TEST_COMPILE_FAILED must not be solved by weakening a valid regression test.

Return JSON with this schema:
{"rootCause":"string","confidence":"LOW|MEDIUM|HIGH","targetFiles":["string"],"reasoning":["string"],
"reflectionDiagnosis":{"failureType":"string","failedFiles":["string"],"mustFix":["string"],"mustAvoid":["string"]},
"scopeDecision":{"decision":"KEEP_SCOPE|EXPAND_SCOPE","finalScopeType":"STRICT_SINGLE_METHOD|MULTI_METHOD|FULL_FILE|NO_CODE_FIX","finalTargetFiles":["string"],"finalTargetMethods":["Class.method"],"whyKeepOrExpand":["string"],"expectedBehaviorChange":"string","risk":"LOW|MEDIUM|HIGH"},
"unifiedDiffPatch":"string","fileRewrites":[{"filePath":"string","newContent":"complete file content","reasoning":"string"}],
"exactReplaceBlocks":[{"filePath":"string","oldText":"exact source","newText":"replacement","reasoning":"string"}],
"testSuggestions":["string"],"mavenCommands":["string"],"testUnifiedDiffPatch":"string",
"testFileRewrites":[{"filePath":"string","newContent":"complete file content","reasoning":"string"}],"riskNotes":["string"]}
""" + reflection_block + "\nIncident fix input:\n" + json.dumps(agent_input, ensure_ascii=False, default=str, separators=(",", ":"))

    def _parse_bugfix_agent(self, content: str, repository: str) -> dict[str, Any]:
        payload = self._json_payload(content)
        if not payload:
            try:
                proposal = PatchProposal.from_llm(content).to_dict()
            except Exception:
                proposal = {"summary": "", "rationale": "LLM output was not valid JSON", "patches": [], "tests": []}
            return {"rootCause": self._regex_json_string(content, "rootCause"),
                    "confidence": self._regex_json_string(content, "confidence") or "LOW", "targetFiles": [],
                    "reasoning": [], "reflectionDiagnosis": {}, "scopeDecision": {}, "unifiedDiffPatch": "",
                    "fileRewrites": [], "exactReplaceBlocks": [], "testSuggestions": [], "mavenCommands": [],
                    "testUnifiedDiffPatch": "", "testFileRewrites": [], "riskNotes": [], "proposal": proposal,
                    "rawContent": content, "errorMessage": ""}
        root, patches = Path(repository or ".").resolve(), []
        for rewrite in self._release_list(payload.get("fileRewrites")):
            item, path = self._release_mapping(rewrite), ""
            path = str(item.get("filePath") or "").replace("\\", "/")
            target = (root / path).resolve()
            try:
                target.relative_to(root)
                if path and target.is_file() and str(item.get("newContent") or ""):
                    patches.append({"path": path, "old": target.read_text(encoding="utf-8"), "new": str(item["newContent"])})
            except (OSError, ValueError):
                continue
        for block in self._release_list(payload.get("exactReplaceBlocks")):
            item = self._release_mapping(block)
            path, old_text = str(item.get("filePath") or "").replace("\\", "/"), str(item.get("oldText") or "")
            if path and old_text:
                patches.append({"path": path, "old": old_text, "new": str(item.get("newText") or "")})
        if not patches and str(payload.get("unifiedDiffPatch") or "").strip():
            patches = self._production_patches_from_unified_diff(repository, str(payload["unifiedDiffPatch"]))
        proposal = {"summary": str(payload.get("rootCause") or ""), "rationale": "\n".join(
            self._release_string_list(payload.get("reasoning"))), "patches": patches,
            "tests": self._release_string_list(payload.get("testSuggestions"))}
        return {"rootCause": str(payload.get("rootCause") or ""), "confidence": str(payload.get("confidence") or ""),
                "targetFiles": self._release_string_list(payload.get("targetFiles")),
                "reasoning": self._release_string_list(payload.get("reasoning")),
                "reflectionDiagnosis": self._release_mapping(payload.get("reflectionDiagnosis")),
                "scopeDecision": self._release_mapping(payload.get("scopeDecision")),
                "unifiedDiffPatch": str(payload.get("unifiedDiffPatch") or ""),
                "fileRewrites": self._release_list(payload.get("fileRewrites")),
                "exactReplaceBlocks": self._release_list(payload.get("exactReplaceBlocks")),
                "testSuggestions": self._release_string_list(payload.get("testSuggestions")),
                "mavenCommands": self._release_string_list(payload.get("mavenCommands")),
                "testUnifiedDiffPatch": str(payload.get("testUnifiedDiffPatch") or ""),
                "testFileRewrites": self._release_list(payload.get("testFileRewrites")),
                "riskNotes": self._release_string_list(payload.get("riskNotes")), "proposal": proposal,
                "rawContent": json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "errorMessage": ""}

    @staticmethod
    def _production_patches_from_unified_diff(repository: str, unified_diff: str) -> list[dict[str, str]]:
        """Convert the Java agent's unified-diff fallback into sandbox FilePatch operations without mutating source."""
        try:
            from ..codeops.test_verification import TestPatchApplier

            hunks = TestPatchApplier._parse_hunks(TestPatchApplier._normalize(unified_diff))
        except (OSError, ValueError):
            return []
        root, patches = Path(repository or ".").resolve(), []
        for path, file_hunks in hunks.items():
            normalized, target = path.replace("\\", "/"), (root / path).resolve()
            try:
                target.relative_to(root)
                if not normalized.startswith("src/main/") or not target.is_file():
                    continue
                original = target.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            lines = original.replace("\r\n", "\n").replace("\r", "\n").splitlines()
            valid = True
            for start, old_lines, new_lines, _new_file in file_hunks:
                old_lines, new_lines = list(old_lines), list(new_lines)
                while old_lines and new_lines and old_lines[-1] == new_lines[-1] == "":
                    old_lines.pop()
                    new_lines.pop()
                index = CodeOpsGraph._find_lines(lines, old_lines)
                if index < 0 and start > 0 and start - 1 + len(old_lines) <= len(lines):
                    index = start - 1
                if index < 0:
                    valid = False
                    break
                lines[index:index + len(old_lines)] = new_lines
            if valid:
                patches.append({"path": normalized, "old": original, "new": "\n".join(lines) + "\n"})
        return patches

    @staticmethod
    def _find_lines(lines: list[str], target: list[str]) -> int:
        if not target or len(target) > len(lines):
            return -1
        for index in range(len(lines) - len(target) + 1):
            if all(actual == expected or actual.strip() == expected.strip()
                   for actual, expected in zip(lines[index:index + len(target)], target)):
                return index
        return -1

    @staticmethod
    def _regex_json_string(content: str, key: str) -> str:
        match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', str(content or ""), re.DOTALL)
        return match.group(1).replace("\\n", "\n").replace('\\"', '"') if match else ""

    async def _legacy_skill_test_verification(self, state: CodeOpsState) -> dict[str, Any]:
        update = await self._verify(state)
        verification = update["verification"]
        passed = update["context"].get("verificationPassed", False)
        diff = state["context"].get("diffContext", {})
        localization = state.get("working_memory", {}).get("codeLocalization", {})
        patch = state.get("working_memory", {}).get("patchGeneration", {})
        changed = diff.get("changedFiles", []) if isinstance(diff, dict) else []
        related = list(dict.fromkeys([*(diff.get("relatedTestFiles", []) if isinstance(diff, dict) else []),
                                      *self._string_list(localization.get("relatedTestFiles"))]))
        recommended = self._string_list(localization.get("recommendedTests"))
        recommended.extend(f"{item}：直接相关回归测试" for item in related)
        if not recommended:
            sources = changed or self._string_list(localization.get("targetFiles"))
            for file in sources:
                if file.endswith(".java") and "/src/test/" not in file.replace("\\", "/"):
                    recommended.append(file + ("：未发现配套测试，建议新增同名 Test/Tests 覆盖核心分支。" if changed
                                               else "：来自 agent loop 代码定位，建议新增或运行同名 Test/Tests 覆盖核心分支。"))
        recommended = list(dict.fromkeys(recommended)) or ["当前没有 Java 变更或相关测试，建议先运行编译验证。"]
        gaps: list[str] = []
        for file in changed:
            normalized = file.replace("\\", "/")
            if file.endswith(".java") and "/src/test/" not in normalized and not related:
                gaps.append(file + " 缺少自动识别到的同名测试。")
            if "controller" in file.lower():
                gaps.append(file + " 建议覆盖 HTTP 入参、异常映射和返回码。")
            if "service" in file.lower():
                gaps.append(file + " 建议覆盖主流程、异常分支、事务/幂等边界。")
            if "repository" in file.lower() or "mapper" in file.lower():
                gaps.append(file + " 建议覆盖 SQL 条件、空结果和边界分页。")
        if not gaps and not related:
            gaps.extend(file + " 来自 agent loop 定位，但未发现相关测试文件。"
                        for file in self._string_list(localization.get("targetFiles"))
                        if file.endswith(".java") and "/src/test/" not in file.replace("\\", "/"))
        gaps = gaps or ["暂未发现明显测试覆盖缺口，仍建议结合任务目标人工确认关键路径。"]
        test_names = [Path(item).stem for item in related if item]
        maven_commands = ["mvn -q -DskipTests compile",
                          "mvn -q -Dtest=" + ",".join(test_names) + " test" if test_names else "mvn -q test"]
        test_enabled = not self.settings or self.settings.codeops_test_execution_enabled
        if not test_enabled:
            execution_results = ["真实测试执行未开启：设置 codeops.test.execution.enabled=true 后会运行推荐 Maven 命令。"]
        elif verification.get("command"):
            success_text = str(verification.get("status") == "PASSED").lower()
            execution_results = [f"command={' '.join(str(item) for item in verification['command'])}, "
                                 f"success={success_text}, exitCode={verification.get('exit_code')}, "
                                 f"costMillis={verification.get('duration_ms', 0)}, output={verification.get('output', '')[:1200]}"]
        else:
            execution_results = [str(verification.get("output") or "")]
        output_text = "\n".join(execution_results)
        lower_output = output_text.lower()
        tests_failed = "success=false" in output_text or "exitCode=1" in output_text
        tests_passed = ("真实测试执行未开启" not in output_text and not tests_failed and
                        ("success=true" in lower_output or "build success" in lower_output
                         or ("tests run:" in lower_output and "failures: 0" in lower_output
                             and "errors: 0" in lower_output)))
        failure_type = ""
        if tests_failed:
            if any(token in lower_output for token in ("compilation failure", "compilation error", "cannot find symbol", "does not exist")):
                failure_type = "TEST_COMPILE_FAILED"
            elif "timed out" in lower_output or "timeout" in lower_output:
                failure_type = "TEST_TIMEOUT"
            elif ("assertion" in lower_output or "failures:" in lower_output or "<<< failure!" in lower_output
                  or ("expected" in lower_output and ("actual" in lower_output or "but was" in lower_output))):
                failure_type = "TEST_ASSERTION_FAILED"
            elif "patch does not apply" in lower_output or "context not found" in lower_output:
                failure_type = "TEST_PATCH_APPLY_FAILED"
            else:
                failure_type = "UNKNOWN"
        repository_path = (patch.get("sandboxRepositoryPath") or patch.get("repositoryPath")
                           or (diff.get("repositoryPath") if isinstance(diff, dict) else "") or "")
        baseline_plan = {"repositoryPath": repository_path,
                         "changeRef": (diff.get("changeRef") if isinstance(diff, dict) else None) or "working_tree",
                         "changedFiles": changed, "relatedTestFiles": related, "recommendedTests": recommended,
                         "coverageGaps": gaps, "mavenCommands": maven_commands,
                         "verificationNotes": [f"验证计划基于任务目标“{state['task'].get('goal') or '未提供'}”和 diff 上下文生成。",
                                               "当前 diff 摘要：" + str(diff.get("diffSummary") or "无 diff 摘要"),
                                               "如果修复来自线上故障，还应补充可观测指标或日志断言作为上线观察项。"],
                         "testExecutionResults": []}
        verification_raw = {
            "phase": "PHASE_5_LLM_TEST_VERIFICATION", "repositoryPath": repository_path,
            "originalRepositoryPath": diff.get("repositoryPath", ""),
            "sandboxRepositoryPath": patch.get("sandboxRepositoryPath"), "testExecutionRepositoryPath": repository_path,
            "testExecutionAsync": bool(state["context"].get("asyncTestExecution")),
            "changeRef": baseline_plan["changeRef"], "changedFiles": changed, "relatedTestFiles": related,
            "recommendedTests": recommended, "coverageGaps": gaps, "mavenCommands": maven_commands,
            "skippedMavenCommands": [], "queuedBackgroundTasks": [], "backgroundVerificationPending": False,
            "backgroundVerificationStatus": "", "backgroundToolTasks": state["context"].get("backgroundToolTasks", []),
            "taskNotifications": state["context"].get("taskNotifications", []),
            "verificationNotes": baseline_plan["verificationNotes"], "testExecutionResults": execution_results,
            "baselinePlan": baseline_plan, "llmTestPlanSuccess": False, "llmTestPlanFallback": True,
            "mergedRepairAndTestAgent": state["task"]["taskType"] == "INCIDENT_TO_FIX" and bool(patch),
            "testPlanReasoning": [], "llmTestPlanError": "", "testSnippets": [], "testPatchGenerated": False,
            "testPatchScaffolded": False, "testPatchTargetFiles": [], "testPatchReasoning": [], "testPatchDraft": "",
            "testPatchError": "", "testPatchValidation": {}, "testPatchApply": {}, "verificationBlockedReason": "",
            "testPatchRolledBack": False, "testPatchRollbackReason": "", "testFailureType": failure_type,
            "failedCommands": re.findall(r"mvn\s+[^\"]+", output_text),
            "failedTestFiles": list(dict.fromkeys(re.findall(r"[\w]+\.java:\d+|[\w]+Test\.\w+", output_text)))[:10],
            "failedAssertions": list(dict.fromkeys(re.findall(r"expected:\s*<[^>]*>\s*but was:\s*<[^>]*>", output_text)))[:10],
            "rawFailureSummary": output_text if len(output_text) <= 1500 else output_text[:1500] + "...",
            "testsPassed": tests_passed, "repairObservations": state["context"].get("repairObservations", []),
        }
        memory = dict(state.get("working_memory", {}))
        context = dict(update["context"])
        executed = list(state.get("executed_skills", []))
        if passed:
            memory["testVerification"] = update["verification"]
        elif state["task"]["taskType"] == "INCIDENT_TO_FIX":
            reflection = int(context.get("incidentFixReflectionRound", 0)) + 1
            context["incidentFixReflectionRound"] = reflection
            failures = list(context.get("incidentFixReflectionFailures", []))
            failures.append({"round": reflection, "failedSkill": "test_verification",
                             "diagnostic": context.get("failureDiagnostic", {}), "summary": update["verification"].get("output", "")[:1200]})
            context["incidentFixReflectionFailures"] = failures
            if reflection >= 3:
                context["incidentFixReflectionExhausted"] = True
            else:
                for key in ("patchGeneration", "testVerification", "releaseRisk"):
                    memory.pop(key, None)
                executed = [item for item in executed if item not in {
                    "bug_fix", "test_verification", "release_risk_analysis"}]
        status = "FAILED" if tests_failed else ("SUCCESS" if tests_passed or bool(diff.get("diffAvailable")) else "NO_DIFF")
        summary = f"已生成测试验证计划：建议测试 {len(recommended)} 项，覆盖缺口 {len(gaps)} 项。"
        result = self._record_skill(state, "test_verification", "testVerification", verification_raw,
                                    summary, status=status, context=context,
                                    tool_calls=update["tool_calls"], extra={"verification": update["verification"]})
        if tests_failed:
            result["working_memory"] = memory
            result["executed_skills"] = executed + ["test_verification"]
        return result

    async def _skill_test_verification(self, state: CodeOpsState) -> dict[str, Any]:
        """Port of Java TestVerificationSkill; LangGraph owns the surrounding state transitions."""
        outcome = await self.test_verification_service.execute(state)
        raw, context = outcome["raw"], outcome["context"]
        hook_payload = {"status": outcome["status"], "mavenCommands": raw["mavenCommands"],
                        "testExecutionResults": raw["testExecutionResults"], "testsPassed": raw["testsPassed"],
                        "testFailureType": raw["testFailureType"],
                        "verificationBlockedReason": raw["verificationBlockedReason"]}
        context, _ = self.hooks.emit(context, "AFTER_TEST", "TEST_VERIFICATION", outcome["summary"], hook_payload)
        raw["repairObservations"] = context.get("repairObservations", [])
        memory = dict(state.get("working_memory", {}))
        executed = list(state.get("executed_skills", []))
        if outcome["status"] == "FAILED" and state["task"]["taskType"] == "INCIDENT_TO_FIX":
            reflection = int(context.get("incidentFixReflectionRound", 0)) + 1
            context["incidentFixReflectionRound"] = reflection
            failures = list(context.get("incidentFixReflectionFailures", []))
            failures.append({"round": reflection, "failedSkill": "test_verification",
                             "diagnostic": context.get("failureDiagnostic", {}),
                             "summary": raw["rawFailureSummary"][:1200]})
            context["incidentFixReflectionFailures"] = failures
            if reflection >= 3:
                context["incidentFixReflectionExhausted"] = True
            else:
                for key in ("patchGeneration", "testVerification", "releaseRisk"):
                    memory.pop(key, None)
                executed = [item for item in executed if item not in {
                    "bug_fix", "test_verification", "release_risk_analysis"}]
        result = self._record_skill(state, "test_verification", "testVerification", raw, outcome["summary"],
                                    status=outcome["status"], context=context,
                                    tool_calls=state["tool_calls"] + outcome["toolCalls"],
                                    extra={"verification": {"status": outcome["status"],
                                                            "output": raw["rawFailureSummary"]}})
        if outcome["status"] == "FAILED":
            result["working_memory"] = memory
            result["executed_skills"] = executed + ["test_verification"]
        return result

    async def _skill_pr_review(self, state: CodeOpsState) -> dict[str, Any]:
        diff = state["context"].get("diffContext", {}).get("diff", "")
        findings = [] if diff else [{"severity": "INFO", "message": "No git diff was available for review."}]
        review = {"findings": findings, "diffAvailable": bool(diff), "reviewedChangeRef": state["task"].get("changeRef", "")}
        return self._record_skill(state, "pr_review", "prReview", review,
                                  f"代码审查完成，发现 {len(findings)} 项。")

    async def _skill_release_risk(self, state: CodeOpsState) -> dict[str, Any]:
        update = await self._release_risk(state)
        report = update["context"]["releaseRisk"]
        raw = update["context"]["releaseRiskRaw"]
        summary = (f"代码审核与发布风险分析完成：verdict={raw['reviewVerdict']}，qualityScore={raw['qualityScore']}，"
                   f"风险等级={report['riskLevel']}，影响范围={len(report['impactScopes'])}，"
                   f"风险点={len(report['riskPoints'])}，回归重点={len(report['regressionFocus'])}")
        return self._record_skill(state, "release_risk_analysis", "releaseRisk", raw, summary,
                                  status="SUCCESS" if raw["diffAvailable"] else "NO_DIFF", context=update["context"])

    def _record_skill(self, state: CodeOpsState, skill: str, memory_key: str, value: Any, summary: str,
                      *, status: str = "SUCCESS", context: dict[str, Any] | None = None,
                      tool_calls: int | None = None, round_no: int | None = None,
                      extra: dict[str, Any] | None = None) -> dict[str, Any]:
        memory = {**state.get("working_memory", {}), memory_key: value}
        existing_outputs = (context or state["context"]).get("skillOutputs", {})
        outputs = {**(existing_outputs if isinstance(existing_outputs, dict) else {}), skill: value}
        step_no = len(state["steps"]) + 1
        next_context = {**(context or state["context"]), "skillOutputs": outputs, "incidentFixWorkingMemory": memory}
        next_context = self.dag.mark(next_context, step_no, skill, status, summary, {memory_key: value})
        decision = state.get("decision") or {}
        step = self._step(step_no, skill, status, summary)
        step["decision"] = decision.get("decision") or "EXECUTE_SKILL"
        step["reason"] = decision.get("reason") or ""
        step["rawEvidenceJson"] = json.dumps(value if isinstance(value, dict) else {memory_key: value},
                                               ensure_ascii=False, default=str, separators=(",", ":"))
        result = {
            "working_memory": memory, "context": next_context,
            "task": {**state["task"], "context": next_context, "updateTime": now_iso()},
            "executed_skills": state.get("executed_skills", []) + [skill],
            "round": round_no if round_no is not None else state["round"] + 1,
            "tool_calls": state["tool_calls"] if tool_calls is None else tool_calls,
            "steps": state["steps"] + [step],
        }
        result.update(extra or {})
        return result

    async def _finish(self, state: CodeOpsState) -> dict[str, Any]:
        reason = state.get("stop_reason") or "任务停止。"
        step = {"stepNo": len(state["steps"]) + 1, "decision": "STOP", "selectedSkill": None,
                "reason": reason, "expectedEvidence": [], "resultSummary": "任务停止",
                "rawEvidenceJson": None, "status": "STOPPED"}
        return {"steps": state["steps"] + [step]}

    @staticmethod
    def _route_finish(state: CodeOpsState) -> Literal["approval", "summarize"]:
        if CodeOpsGraph._approval_payload(state) is not None:
            return "approval"
        return "summarize"

    async def _repo_understanding(self, state: CodeOpsState) -> dict[str, Any]:
        repository = Path(state["task"]["repository"] or ".").resolve()
        if not repository.exists():
            summary = f"Repository does not exist: {repository}"
            context = {**state["context"], "repositoryError": summary}
            evidence_graph = {"nodes": [], "edges": [], "summary": summary}
        else:
            budget = ToolBudget(state["task"]["maxToolCalls"], state["tool_calls"])
            toolkit = RepositoryToolkit(repository, budget)
            files = toolkit.list_files(1000)
            terms = self._search_terms(state["task"]["goal"], state["context"])
            matches = toolkit.search(terms, limit=100)
            baseline = state["context"].get("repoBaselineSnapshot")
            if isinstance(baseline, dict) and baseline:
                current = toolkit.create_snapshot()
                changed_files = [name for name in dict.fromkeys([*baseline, *current])
                                 if str(baseline.get(name, "")) != str(current.get(name, ""))]
                parts: list[str] = []
                for name in changed_files:
                    parts.extend(difflib.unified_diff(
                        str(baseline.get(name, "")).splitlines(), str(current.get(name, "")).splitlines(),
                        fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""))
                diff = {"diff": "\n".join(parts)[:50000]}
            else:
                diff = toolkit.git_diff(state["task"].get("changeRef", "")) if (repository / ".git").exists() else {"diff": ""}
                changed_files = list(dict.fromkeys(re.findall(r"^\+\+\+ b/(.+)$", diff.get("diff", ""), re.MULTILINE)))
            related_tests = toolkit.find_tests(changed_files) if changed_files else []
            diff_context = {"repositoryPath": str(repository),
                            "changeRef": state["task"].get("changeRef") or "working_tree",
                            "changedFiles": changed_files, "relatedTestFiles": related_tests,
                            "diffSummary": (f"changedFiles={len(changed_files)}" if diff.get("diff")
                                            else "未读取到可用 diff"),
                            "diffAvailable": bool(diff.get("diff")), "diff": diff.get("diff", "")}
            summary = f"Indexed {len(files)} files from {repository}"
            memories = await self.memory.recall(state["task"]["taskType"], state["task"]["goal"]) if self.memory else []
            context = {**state["context"], "repositoryFiles": files, "repositoryRoot": str(repository),
                       "historicalIncidentMemories": memories,
                       "codeSearchMatches": matches, "diffContext": diff_context}
            nodes = ([{"id": f"file:{item['file']}:{item['line']}", "type": "CODE", "file": item["file"],
                       "line": item["line"], "signals": item["hits"]} for item in matches])
            evidence_graph = {"nodes": nodes, "edges": [], "summary": f"{len(nodes)} localized code evidence nodes"}
        return {"context": context, "evidence_graph": evidence_graph,
                "tool_calls": budget.used_calls if repository.exists() else state["tool_calls"],
                "steps": state["steps"] + [self._step(2, "repo_understanding", "COMPLETED", summary)]}

    async def _localize(self, state: CodeOpsState) -> dict[str, Any]:
        matches = state["context"].get("codeSearchMatches", [])
        ranked: dict[str, dict[str, Any]] = {}
        for match in matches:
            item = ranked.setdefault(match["file"], {"file": match["file"], "score": 0, "lines": [], "signals": []})
            item["score"] += len(match.get("hits", [])) * 10 + 1
            item["lines"].append(match.get("line"))
            item["signals"].extend(match.get("hits", []))
        candidates = sorted(ranked.values(), key=lambda item: (-item["score"], item["file"]))[:12]
        localization = {"candidates": candidates, "targetFiles": [item["file"] for item in candidates],
                        "blocking": not bool(candidates), "source": "EVIDENCE_GRAPH"}
        summary = f"Localized {len(candidates)} candidate files" if candidates else "No code candidates localized"
        return {"localization": localization, "context": {**state["context"], "localization": localization},
                "steps": state["steps"] + [self._step(len(state["steps"]) + 1, "code_localization", "COMPLETED", summary)]}

    async def _execute_skill(self, state: CodeOpsState) -> dict[str, Any]:
        round_no = state["round"] + 1
        skill = state["current_skill"]
        model_decision = self.model_router.route(skill, state["context"], round_no)
        is_bugfix = skill == "bug_fix"
        prompt = self._bugfix_prompt(state, round_no) if is_bugfix else (
            f"Perform CodeOps skill {skill}. Goal: {state['task']['goal']}\n"
            f"Repository evidence: {json.dumps(state['context'], ensure_ascii=False)[:12000]}\n"
            "Return JSON with summary, rationale, patches[{path,old,new}], tests[]. "
            "old must be an exact non-empty source substring. Use an empty patches array when no safe patch is proven. "
            "Do not claim files you have not seen.")
        proposal: PatchProposal | None = None
        bugfix_agent: dict[str, Any] = {}
        try:
            if is_bugfix and not bool(getattr(self.settings, "codeops_agent_bugfix_llm_enabled", True)):
                raise RuntimeError("CodeOps LLM bugfix agent is disabled.")
            result = await self.llm.complete(prompt, system="You are a careful senior software engineer.", model=model_decision.model)
            source = "LLM"
            try:
                if is_bugfix:
                    bugfix_agent = self._parse_bugfix_agent(result, state["task"].get("repository") or "")
                    proposal = self._proposal(bugfix_agent["proposal"])
                else:
                    proposal = PatchProposal.from_llm(result)
            except Exception:
                proposal = PatchProposal(summary=result, patches=[], rationale="Model response was not a valid patch payload")
        except Exception as exc:
            result = self._deterministic_repair_summary(state, skill, str(exc))
            source = "FALLBACK"
            proposal = PatchProposal(summary=result, patches=[], rationale=f"Deterministic evidence analysis: {exc}")
            if is_bugfix:
                bugfix_agent = {"rootCause": "", "confidence": "LOW", "targetFiles": [], "reasoning": [],
                                "reflectionDiagnosis": {}, "scopeDecision": {}, "unifiedDiffPatch": "", "fileRewrites": [],
                                "exactReplaceBlocks": [], "testSuggestions": [], "mavenCommands": [],
                                "testUnifiedDiffPatch": "", "testFileRewrites": [], "riskNotes": [],
                                "rawContent": "", "errorMessage": str(exc)}
        step = self._step(len(state["steps"]) + 1, skill, "COMPLETED", result)
        usage = self.cost_control.estimate(model_decision.tier, model_decision.model, prompt, result, skill)
        if is_bugfix:
            bugfix_agent.update({"modelRouting": {"model": model_decision.model,
                                                    "modelTier": "flash" if "flash" in model_decision.model.lower() else "pro",
                                                    "reason": model_decision.reason, "reflectionRound": round_no - 1},
                                "llmUsage": usage})
        step["rawEvidenceJson"] = json.dumps({"source": source, "round": round_no,
                                                "modelDecision": model_decision.__dict__, "llmUsage": usage}, ensure_ascii=False)
        return {"round": round_no, "tool_calls": state["tool_calls"] + 1, "patch_proposal": proposal.to_dict(),
                "bugfixAgent": bugfix_agent,
                "context": self.compactor.compact({**state["context"], "lastRecommendation": result, "llmUsage": usage}),
                "steps": state["steps"] + [step]}

    def _route_after_skill(self, state: CodeOpsState) -> Literal["sandbox", "verify"]:
        return "sandbox" if state.get("patch_proposal", {}).get("patches") else "verify"

    async def _sandbox_patch(self, state: CodeOpsState) -> dict[str, Any]:
        hook_context, hook = self.hooks.emit(state["context"], "BEFORE_PATCH_APPLY", "PATCH_SANDBOX",
                                             "validate patch before sandbox apply", {"sandbox": True})
        try:
            proposal = self._proposal(state["patch_proposal"])
            decision = SecurityPolicy().authorize("sandbox_patch")
            if not decision.allowed:
                raise PermissionError(decision.reason)
            guard = PatchScopeGuard().validate(state["task"]["repository"], proposal, state["context"].get("repairScope"))
            if not guard["passed"]:
                raise PermissionError("; ".join(guard["violations"]))
            if self.settings and not self.settings.codeops_patch_sandbox_enabled:
                raise PermissionError("CodeOps patch sandbox is disabled by configuration")
            base_dir = getattr(self.settings, "codeops_patch_sandbox_base_dir", "") or None
            if base_dir:
                Path(base_dir).mkdir(parents=True, exist_ok=True)
            result = PatchSandbox(
                state["task"]["repository"], base_dir,
                getattr(self.settings, "codeops_patch_sandbox_prefer_git_worktree", False),
                getattr(self.settings, "codeops_patch_sandbox_timeout_ms", 30000), state["task"]["taskId"],
            ).apply(proposal).to_dict()
            validation = PatchValidation().validate(state["task"]["repository"], result.get("diff", ""))
            analysis = PatchDiffAnalysis().analyze(result.get("diff", ""), validation, guard)
            result.update({"scopeGuard": guard, "validation": validation, "diffAnalysis": analysis})
            result["success"] = bool(result["success"] and validation["valid"] and guard["passed"])
            status = "COMPLETED" if result["success"] else "FAILED"
            failures = [*result["errors"], *validation["errors"], *guard["violations"]]
            summary = f"Sandbox changed {len(result['changed_files'])} file(s)" if result["success"] else "; ".join(failures)
        except Exception as exc:
            result, status, summary = {"success": False, "errors": [str(exc)]}, "FAILED", str(exc)
        hook_context, _ = self.hooks.emit(hook_context, "AFTER_PATCH_APPLY", "PATCH_SANDBOX", summary,
                                          {"sandbox": True, "success": result.get("success", False)})
        return {"sandbox_result": result, "context": {**hook_context, "sandboxResult": result, "patchHook": hook},
                "tool_calls": state["tool_calls"] + 1,
                "steps": state["steps"] + [self._step(len(state["steps"]) + 1, "patch_sandbox", status, summary)]}

    async def _verify(self, state: CodeOpsState) -> dict[str, Any]:
        root = Path(state.get("sandbox_result", {}).get("sandbox") or state["task"]["repository"] or ".").resolve()
        test_enabled = not self.settings or self.settings.codeops_test_execution_enabled
        if state["context"].get("skipTestExecution") or not test_enabled:
            planned = self._planned_tests(state)
            verification = {"command": [], "status": "SKIPPED", "exit_code": None,
                            "output": "Skipped by evaluation policy; planned regression tests: " + ", ".join(planned),
                            "plannedTests": planned, "duration_ms": 0}
        else:
            timeout = max(1, self.settings.codeops_test_execution_timeout_ms // 1000) if self.settings else 120
            verification = (await TestRunner().run(root, timeout_seconds=timeout)).to_dict()
        passed = verification["status"] in {"PASSED", "SKIPPED"}
        output = verification["output"]
        diagnostic = self.failure_parser.parse(output) if not passed else {}
        recovery = self.recovery_policy.decide(diagnostic, state["round"], state["task"]["maxRounds"],
                                               state["tool_calls"], state["task"]["maxToolCalls"]) if not passed else {"action": "NONE"}
        status = "COMPLETED" if passed else "FAILED"
        return {"verification": verification,
                "context": self.compactor.compact({**state["context"], "verificationPassed": passed,
                                                    "verificationOutput": output, "failureDiagnostic": diagnostic,
                                                    "recoveryDecision": recovery}),
                "tool_calls": state["tool_calls"] + (1 if verification["command"] else 0),
                "steps": state["steps"] + [self._step(len(state["steps"]) + 1, "test_verification", status, output[:1200])]}

    def _route_after_verify(self, state: CodeOpsState) -> Literal["retry", "approval", "finish"]:
        if state["context"].get("recoveryDecision", {}).get("action") == "RETRY":
            return "retry"
        if state.get("sandbox_result", {}).get("success") and bool(state["context"].get("allowPatchApply")):
            return "approval"
        return "finish"

    @staticmethod
    def _planned_tests(state: CodeOpsState) -> list[str]:
        text = json.dumps({"goal": state["task"].get("goal"), "context": state.get("context", {}),
                           "localization": state.get("localization", {})}, ensure_ascii=False).lower()
        tests = []
        if any(term in text for term in ("unitprice", "unit price", "ordersubmitservice")):
            tests.append("OrderSubmitServiceTest")
        if any(term in text for term in ("inventory", "oversell", "reserve")):
            tests.extend(["InventoryConcurrencyTest", "OrderSubmitServiceConcurrencyTest"])
        if any(term in text for term in ("idempot", "duplicate request", "requestid")):
            tests.extend(["OrderSubmitServiceConcurrencyTest", "IdempotencyServiceAtomicityTest"])
        return list(dict.fromkeys(tests)) or ["targeted regression test", "integration verification"]

    @staticmethod
    def _deterministic_repair_summary(state: CodeOpsState, skill: str, error: str) -> str:
        text = json.dumps({"goal": state["task"].get("goal"), "context": state.get("context", {}),
                           "localization": state.get("localization", {})}, ensure_ascii=False).lower()
        recommendations = []
        if any(term in text for term in ("unitprice", "unit price", "nullpointer")):
            recommendations.append("Validate unitPrice and quantity and throw IllegalArgumentException before calculation; verify with OrderSubmitServiceTest.")
        if any(term in text for term in ("inventory", "oversell", "race condition", "non-atomic")):
            recommendations.append("Make inventory reserve atomic using synchronized, ConcurrentHashMap compute, putIfAbsent or compareAndSet; verify with InventoryConcurrencyTest and OrderSubmitServiceConcurrencyTest.")
        if any(term in text for term in ("idempot", "duplicate request", "requestid")):
            recommendations.append("Replace the check-then-act flow with atomic tryMarkProcessed(requestId) using synchronized or ConcurrentHashMap.putIfAbsent; reject Duplicate requestId and verify with IdempotencyServiceAtomicityTest.")
        base = " ".join(recommendations) or "Inspect localized evidence, make the smallest safe change, and add a targeted regression test."
        return f"Deterministic {skill} analysis: {base} LLM unavailable: {error}"

    async def _mark_approval(self, state: CodeOpsState) -> dict[str, Any]:
        approval = self._approval_payload(state)
        return {"approval_required": approval is not None, "status": "WAITING_APPROVAL",
                "approval": approval or {}}

    @staticmethod
    def _latest_raw_outputs(state: CodeOpsState) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        for step in state.get("steps", []):
            value = step.get("rawEvidenceJson")
            if not value:
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                raw.update(parsed)
        return raw

    @staticmethod
    def _real_tests_passed(raw: dict[str, Any]) -> bool:
        value = raw.get("testExecutionResults")
        text = "\n".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
        if not text.strip() or "真实测试执行未开启" in text:
            return False
        lower = text.lower()
        failures = re.search(r"failures:\s*(\d+)", lower)
        errors = re.search(r"errors:\s*(\d+)", lower)
        if ('"success":false' in lower or '"success": false' in lower or "exitcode=1" in lower
                or "exit code: 1" in lower or "build failure" in lower or "<<< failure!" in lower
                or (failures and int(failures.group(1)) > 0) or (errors and int(errors.group(1)) > 0)):
            return False
        return ("build success" in lower or '"success":true' in lower
                or ("tests run:" in lower and "failures: 0" in lower and "errors: 0" in lower))

    @staticmethod
    def _approval_payload(state: CodeOpsState) -> dict[str, Any] | None:
        if state.get("task", {}).get("taskType") != "INCIDENT_TO_FIX" or not state.get("steps"):
            return None
        raw = CodeOpsGraph._latest_raw_outputs(state)
        patch_generated = raw.get("llmGenerated") is True
        latest_test = next((step for step in reversed(state["steps"])
                            if step.get("selectedSkill") == "test_verification"), None)
        tests_passed = bool(latest_test and latest_test.get("status") == "SUCCESS"
                            and CodeOpsGraph._real_tests_passed(raw))
        if not patch_generated or not tests_passed:
            return None
        risk_report = raw.get("releaseRiskReport")
        risk = str(risk_report.get("riskLevel") if isinstance(risk_report, dict)
                   and risk_report.get("riskLevel") is not None else raw.get("riskLevel") or "LOW")
        evidence = raw.get("evidenceCoverage") if isinstance(raw.get("evidenceCoverage"), dict) else {}
        quality = raw.get("patchQuality") if isinstance(raw.get("patchQuality"), dict) else {}
        sandbox = raw.get("patchSandbox") if isinstance(raw.get("patchSandbox"), dict) else {}
        reasons: list[str] = []
        if risk.upper() in {"HIGH", "CRITICAL"}:
            reasons.append(f"release risk is {risk}")
        if quality.get("requiresHumanApproval") is True:
            reasons.append("patch quality gate requires human approval")
        try:
            if "minimalChangeScore" in quality and int(quality["minimalChangeScore"]) < 70:
                reasons.append("minimal change score is below 70")
        except (TypeError, ValueError):
            pass
        if quality.get("staticSafetyPassed") is False:
            reasons.append("patch static safety did not pass")
        if sandbox.get("isolated") is False:
            reasons.append("patch was not verified in an isolated sandbox")
        if evidence.get("fixtureFallbackUsed") is True:
            reasons.append("diagnosis used fixture fallback evidence")
        try:
            coverage = float(evidence.get("realEvidenceCoverage") or 0)
            if 0 < coverage < 0.67:
                reasons.append("real telemetry evidence coverage is below 67%")
        except (TypeError, ValueError):
            pass
        if risk.upper() not in {"HIGH", "CRITICAL"} and not reasons:
            return None
        now = now_iso()
        patch_summary = str(raw.get("patchDraft") or "")[:1200]
        test_results = str(raw.get("testExecutionResults") or "")[:1200]
        changed = raw.get("changedFiles") if isinstance(raw.get("changedFiles"), list) else []
        return {"taskId": state["task"]["taskId"],
                "caseName": state["task"].get("goal") or state["task"]["taskId"], "status": "PENDING",
                "rootCause": str(raw.get("rootCause") or ""), "patchSummary": patch_summary,
                "changedFiles": [str(item) for item in changed], "riskLevel": risk,
                "testResults": test_results, "approvalReasons": reasons,
                "evidenceSummary": evidence, "patchQuality": quality, "patchSandbox": sandbox,
                "submittedAt": now, "approvedAt": None, "rejectionReason": None}

    async def _release_risk(self, state: CodeOpsState) -> dict[str, Any]:
        memory = state.get("working_memory", {})
        patch = memory.get("patchGeneration", {}) if isinstance(memory.get("patchGeneration"), dict) else {}
        tests = memory.get("testVerification", {}) if isinstance(memory.get("testVerification"), dict) else {}
        diff = state["context"].get("diffContext", {})
        changed = diff.get("changedFiles", []) if isinstance(diff, dict) else []
        diff_text = str(diff.get("diff") or "") if isinstance(diff, dict) else ""
        added = "\n".join(line[1:] for line in diff_text.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        lower = added.lower()
        risks: list[str] = []
        if not diff.get("diffAvailable"):
            risks.append("未读取到 diff，发布风险只能给出骨架判断，无法定位具体变更。")
        if len(changed) >= 12:
            risks.append("变更文件数量较多，建议拆分发布或提高回归范围，避免影响面过大。")
        related_tests = diff.get("relatedTestFiles", [])
        if not related_tests and any(item.endswith(".java") and "/src/test/" not in item for item in changed):
            risks.append("Java 业务代码有变更但未识别到相关测试文件，存在回归覆盖不足风险。")
        risk_tokens = [
            (("transaction", "@transactional", "rollbackfor"), "事务相关代码发生变化，需要关注事务边界、异常回滚和跨服务调用混在事务内的问题。"),
            (("redis", "cache", "caffeine"), "缓存相关代码发生变化，需要关注缓存一致性、TTL、穿透/击穿以及发布后的脏缓存处理。"),
            (("threadpool", "executor", "async", "completablefuture"), "并发或异步执行逻辑发生变化，需要关注线程池容量、上下文传递、异常吞掉和任务堆积。"),
            (("resttemplate", "webclient", "feign", "dubbo", "httpclient"), "外部依赖调用发生变化，需要关注超时、重试、熔断降级和下游错误码兼容。"),
            (("insert", "update", "delete", "select", "mapper"), "数据访问逻辑发生变化，需要关注慢 SQL、索引命中、分页边界和数据一致性。"),
            (("catch (exception", "catch (throwable", "return null"), "异常处理逻辑发生变化，需要关注异常被吞、错误码不准确和问题不可观测。"),
            (("todo", "fixme"), "新增代码仍包含 TODO/FIXME，发布前需要确认是否为未闭环逻辑。"),
        ]
        risks.extend(message for tokens, message in risk_tokens if any(token in lower for token in tokens))
        if any(item.lower().endswith((".yml", ".yaml", ".properties")) for item in changed):
            risks.append("配置文件发生变化，需要确认多环境配置、默认值、密钥脱敏和回滚方式。")
        if any(any(token in item.lower() for token in ("migration", "schema", ".sql")) for item in changed):
            risks.append("数据库脚本或 schema 发生变化，需要确认向前/向后兼容、执行顺序和回滚脚本。")
        risks = list(dict.fromkeys(risks))[:12]
        scopes: list[str] = []
        scope_rules = [
            (("/controller/", "\\controller\\"), "接口入口层：变更涉及 Controller，需要关注请求参数、鉴权、响应码和兼容性。"),
            (("/service/", "\\service\\"), "业务服务层：变更涉及 Service，需要关注核心业务分支、事务边界和异常处理。"),
            (("/repository/", "/mapper/", "\\repository\\", "\\mapper\\"), "数据访问层：变更涉及 Repository/Mapper，需要关注 SQL、索引、分页和数据兼容性。"),
        ]
        for file in changed:
            file_lower = file.lower()
            scopes.extend(message for tokens, message in scope_rules if any(token in file_lower for token in tokens))
            if any(token in file_lower for token in ("/config/", ".yml", ".yaml", ".properties")):
                scopes.append("配置层：变更涉及配置项，需要关注环境差异、默认值和灰度回退。")
            if "pom.xml" in file_lower or "build.gradle" in file_lower:
                scopes.append("构建依赖层：变更涉及依赖或构建配置，需要关注依赖冲突、镜像构建和启动兼容性。")
            if any(token in file_lower for token in ("mq", "kafka", "rocketmq")):
                scopes.append("异步消息链路：变更涉及消息生产/消费，需要关注幂等、重复消费和积压。")
            if "cache" in file_lower or "redis" in file_lower:
                scopes.append("缓存链路：变更涉及缓存，需要关注缓存一致性、过期时间和击穿风险。")
        if any(token in lower for token in ("http", "feign", "resttemplate", "webclient", "dubbo")):
            scopes.append("外部依赖调用：新增或修改远程调用，需要关注超时、降级和重试边界。")
        scopes = list(dict.fromkeys(scopes)) or ["未读取到变更文件，暂无法判断影响范围。" if not changed
                                                else "通用代码变更：需要结合变更文件和业务入口确认实际影响面。"]
        regression = ["覆盖本次变更直接影响的接口、核心分支和失败分支。"]
        if related_tests:
            regression.append("优先执行相关测试：" + ", ".join(str(item) for item in related_tests))
        for marker, message in (("事务", "补充事务回滚、部分失败和重复请求场景。"),
                                ("缓存", "补充缓存命中、缓存未命中、过期和脏数据刷新场景。"),
                                ("外部依赖", "补充下游超时、下游 5xx、返回字段缺失和降级兜底场景。"),
                                ("数据访问", "补充空结果、大分页、边界条件和慢查询 explain 验证。")):
            if any(marker in item for item in risks):
                regression.append(message)
        if any("controller" in item.lower() for item in changed):
            regression.append("补充接口兼容性、参数校验、鉴权和错误码回归。")
        observations = ["接口 5xx 数量、错误率和核心接口 P95/P99 延迟。", "服务实例 CPU、内存、GC、线程池队列和重启次数。"]
        if any(token in lower for token in ("select", "insert", "update", "delete", "mapper")):
            observations.append("数据库慢 SQL、连接池 active/pending/timeout、事务耗时。")
        if "redis" in lower or "cache" in lower:
            observations.append("缓存命中率、Redis 慢命令、热点 key、缓存错误数。")
        if any(token in lower for token in ("feign", "resttemplate", "webclient", "dubbo", "httpclient")):
            observations.append("下游调用成功率、超时数、重试次数、熔断/降级次数。")
        if any(any(token in item.lower() for token in ("mq", "kafka", "rocketmq")) for item in changed):
            observations.append("消息积压、消费失败率、重复消费和死信队列数量。")
        rollback = ["确认应用版本可快速回退，且回退后配置与依赖版本兼容。"]
        if any(item.lower().endswith((".yml", ".yaml", ".properties")) for item in changed):
            rollback.append("配置变更需要准备独立回退项，避免仅回滚代码但配置仍保留新值。")
        if any(".sql" in item.lower() or "migration" in item.lower() for item in changed):
            rollback.append("数据库变更需要确认是否可逆，优先采用兼容性发布，避免直接依赖回滚 DDL。")
        if "redis" in lower or "cache" in lower or any("缓存" in item for item in risks):
            rollback.append("缓存逻辑变更需要准备缓存清理、预热或 key 版本切换方案。")
        if any(token in lower for token in ("mq", "kafka", "rocketmq")):
            rollback.append("消息链路变更需要确认消费位点、重复消费和消息格式兼容。")
        score = len(risks) + (2 if len(changed) >= 12 else 0) + (
            2 if any(any(token in item for token in ("数据库", "事务", "外部依赖")) for item in risks) else 0)
        risk = "HIGH" if score >= 8 else "MEDIUM" if score >= 4 else "LOW"
        report = {"repositoryPath": diff.get("repositoryPath", ""),
                  "changeRef": diff.get("changeRef") or state["task"].get("changeRef") or "working_tree",
                  "riskLevel": risk, "impactScopes": scopes, "riskPoints": risks,
                  "regressionFocus": regression, "onlineObservationMetrics": observations,
                  "rollbackFocus": rollback,
                  "knowledgeReferences": ["未命中发布规范、复盘或 Runbook 文档；建议补充工程知识库后复评。"]}
        knowledge_matches = self._release_knowledge_matches(memory)
        report["knowledgeReferences"] = self._release_knowledge_references(knowledge_matches)
        patch_facts = self._release_patch_facts(patch, tests, diff)
        reflection_failures = self._release_list(state["context"].get("incidentFixReflectionFailures"))
        agent_input = {
            "taskId": state["task"].get("taskId", ""), "taskType": state["task"].get("taskType", ""),
            "goal": state["task"].get("goal", ""), "repositoryPath": report["repositoryPath"],
            "changeRef": report["changeRef"], "diffSummary": str(diff.get("diffSummary") or ""),
            "changedFiles": changed, "relatedTestFiles": related_tests,
            "opsEvidence": memory.get("opsDiagnosis", {}), "fixStrategy": memory.get("fixStrategy", {}),
            "codeLocalization": memory.get("codeLocalization", {}), "patchGeneration": patch,
            "testVerification": tests, "patchFacts": patch_facts,
            "reflectionFailures": reflection_failures, "knowledgeMatches": knowledge_matches[:self._release_max_knowledge()],
            "baselineReport": report,
        }
        agent = await self._release_risk_agent(state, agent_input, report, reflection_failures)
        report = agent["report"]
        patch_reason = self._auto_patch_blocked_reason(patch)
        verification_reason = self._verification_blocked_reason(tests)
        manual_takeover = bool(patch_reason or verification_reason)
        raw = {"phase": "PHASE_6_LLM_RELEASE_RISK", "diffAvailable": bool(diff.get("diffAvailable")),
               "diffSummary": str(diff.get("diffSummary") or ""), "changedFiles": changed,
               "relatedTestFiles": related_tests, "fixStrategy": memory.get("fixStrategy", {}),
               "patchFacts": patch_facts, "reflectionFailures": reflection_failures,
               "hunkCount": sum(line.startswith("@@") for line in diff_text.splitlines()),
               "knowledgeMatches": knowledge_matches, "releaseRiskReport": report,
               "baselineReleaseRiskReport": agent["baselineReport"], "llmReleaseRiskSuccess": agent["success"],
               "llmReleaseRiskFallback": agent["fallback"], "releaseRiskReasoning": agent["reasoning"],
               "humanApprovalPoints": agent["humanApprovalPoints"], "codeReview": agent["codeReview"],
               "reviewVerdict": agent["reviewVerdict"], "qualityScore": agent["qualityScore"],
               "patchDecision": agent["patchDecision"], "manualTakeoverRequired": manual_takeover,
               "autoPatchBlockedReason": patch_reason, "verificationBlockedReason": verification_reason,
               "blockedAutomationSummary": self._blocked_automation_summary(patch_reason, verification_reason),
               "repairObservations": state["context"].get("repairObservations", []),
               "patchAttempts": state["context"].get("patchAttempts", []),
               "llmReleaseRiskError": agent["errorMessage"], "llmReleaseRiskRawContent": agent["rawContent"],
               "llmReleaseRiskCostMillis": agent["costMillis"], "llmUsage": agent.get("llmUsage", {}),
               "modelRouting": agent["modelRouting"], "riskLevel": report["riskLevel"]}
        return {"context": {**state["context"], "releaseRisk": report, "releaseRiskRaw": raw},
                "steps": state["steps"] + [self._step(len(state["steps"]) + 1, "release_risk", "COMPLETED", json.dumps(report))]}

    def _release_max_knowledge(self) -> int:
        return max(1, int(getattr(self.settings, "codeops_agent_release_risk_max_knowledge", 5) or 5))

    @staticmethod
    def _release_mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _release_list(value: Any) -> list[Any]:
        return list(value) if isinstance(value, list) else []

    @classmethod
    def _release_string_list(cls, value: Any) -> list[str]:
        return [str(item) for item in cls._release_list(value) if str(item).strip()]

    @classmethod
    def _release_first_list(cls, *values: Any) -> list[str]:
        for value in values:
            items = cls._release_string_list(value)
            if items:
                return items
        return []

    @staticmethod
    def _release_int(value: Any) -> int:
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _release_bool_or_none(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        return None

    def _release_knowledge_matches(self, memory: dict[str, Any]) -> list[dict[str, Any]]:
        knowledge = self._release_mapping(memory.get("engineeringKnowledge"))
        values = self._release_list(knowledge.get("matches") or knowledge.get("hits"))
        return [self._release_mapping(value) for value in values if isinstance(value, dict)]

    def _release_knowledge_references(self, matches: list[dict[str, Any]]) -> list[str]:
        references = []
        for match in matches[:self._release_max_knowledge()]:
            category = str(match.get("category") or "")
            score = str(match.get("score") or "")
            title = str(match.get("title") or "")
            path = str(match.get("path") or "")
            if not any((category, score, title, path)):
                continue
            references.append(f"[{category}][{score}] {title} -> {path}")
        return references or ["未命中发布规范、复盘或 Runbook 文档；建议补充工程知识库后复评。"]

    def _release_patch_facts(self, patch: dict[str, Any], tests: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
        guard = self._release_mapping(patch.get("patchScopeGuard"))
        patch_apply = self._release_mapping(patch.get("patchApply"))
        compile_gate = self._release_mapping(patch.get("compileGate"))
        quality = self._release_mapping(patch.get("patchQuality"))
        analysis = self._release_mapping(patch.get("patchDiffAnalysis"))
        facts = {
            "patchGenerated": patch.get("llmGenerated") is True or bool(str(patch.get("patchDraft") or "").strip()),
            "scopeGuardPassed": guard.get("passed") is True or guard.get("allowed") is True,
            "scopeGuardFailureType": str(guard.get("failureType") or ""),
            "scopeViolations": self._release_string_list(guard.get("violations")),
            "patchApplied": patch_apply.get("applied") is True,
            "compilePassed": compile_gate.get("success") if "success" in compile_gate else None,
            "testsPassed": tests.get("testsPassed") if "testsPassed" in tests else None,
            "testFailureType": str(tests.get("testFailureType") or ""),
            "changedFiles": self._release_first_list(analysis.get("touchedFiles"), patch.get("changedFiles"), diff.get("changedFiles")),
            "changedMethods": self._release_first_list(analysis.get("changedMethods"), guard.get("changedMethods")),
            "testsChanged": quality.get("testsChanged") is True or analysis.get("testsChanged") is True,
            "staticSafetyPassed": quality.get("staticSafetyPassed") is not False,
            "sensitiveFiles": self._release_first_list(analysis.get("sensitiveFiles")),
            "configFileCount": self._release_int(analysis.get("configFileCount")),
            "productionFileCount": self._release_int(analysis.get("productionFileCount")),
            "testFileCount": self._release_int(analysis.get("testFileCount")),
            "minimalChangeScore": self._release_int(quality.get("minimalChangeScore")),
            "requiresHumanApproval": quality.get("requiresHumanApproval") is True,
            "recommendedTests": self._release_string_list(tests.get("recommendedTests")),
            "mavenCommands": self._release_string_list(tests.get("mavenCommands")),
            "testExecutionResults": self._release_string_list(tests.get("testExecutionResults"))[:5],
        }
        red_lines = []
        if facts["patchGenerated"] and not facts["scopeGuardPassed"]:
            red_lines.append("SCOPE_GUARD_NOT_PASSED")
        if facts["patchGenerated"] and facts["compilePassed"] is False:
            red_lines.append("COMPILE_NOT_PASSED")
        if facts["patchGenerated"] and facts["testsPassed"] is False:
            red_lines.append("TESTS_NOT_PASSED")
        if facts["testsChanged"]:
            red_lines.append("TESTS_CHANGED")
        if facts["sensitiveFiles"]:
            red_lines.append("SENSITIVE_FILES_TOUCHED")
        if facts["configFileCount"] > 0:
            red_lines.append("CONFIG_FILES_TOUCHED")
        facts["redLines"] = red_lines
        return facts

    def _auto_patch_blocked_reason(self, patch: dict[str, Any]) -> str:
        if not patch:
            return ""
        guard = self._release_mapping(patch.get("patchScopeGuard"))
        if guard and (guard.get("allowed") is False or guard.get("passed") is False):
            return "PatchScopeGuard blocked production patch: " + "; ".join(self._release_string_list(guard.get("violations")))
        patch_apply = self._release_mapping(patch.get("patchApply"))
        if patch_apply and patch_apply.get("applied") is False:
            return "Production patch was not applied: " + str(patch_apply.get("errorMessage") or "")
        validation = self._release_mapping(patch.get("patchValidation"))
        if validation and validation.get("valid") is False:
            return "Production patch validation failed: " + "; ".join(self._release_string_list(validation.get("errors")))
        return ""

    def _verification_blocked_reason(self, tests: dict[str, Any]) -> str:
        if not tests:
            return ""
        explicit = tests.get("verificationBlockedReason")
        if explicit is not None and str(explicit).strip():
            return str(explicit)
        skipped = self._release_string_list(tests.get("skippedMavenCommands"))
        if skipped:
            return "Some verification commands were skipped: " + "; ".join(skipped)
        failure = str(tests.get("testFailureType") or "")
        return "Verification failed with type=" + failure if failure else ""

    @staticmethod
    def _blocked_automation_summary(patch_reason: str, verification_reason: str) -> dict[str, Any]:
        result: dict[str, Any] = {"manualTakeoverRequired": bool(patch_reason or verification_reason)}
        if patch_reason:
            result["autoPatchBlockedReason"] = patch_reason
        if verification_reason:
            result["verificationBlockedReason"] = verification_reason
        return result

    async def _release_risk_agent(self, state: CodeOpsState, agent_input: dict[str, Any], baseline_report: dict[str, Any],
                                  reflection_failures: list[Any]) -> dict[str, Any]:
        disabled = not bool(getattr(self.settings, "codeops_agent_release_risk_llm_enabled", True))
        available = getattr(self.llm, "available", True)
        if disabled:
            return self._release_agent_unavailable("Release risk LLM agent is disabled.", baseline_report)
        if available is False:
            return self._release_agent_unavailable("OPENAI_API_KEY or OPENAI_BASE_URL is not configured", baseline_report)
        routing_context = {**state["context"], "previousFlashFailures": len(reflection_failures)}
        decision = self.model_router.route("release_risk_analysis", routing_context, state.get("round", 0) + 1)
        routing = {"model": decision.model, "modelTier": "flash" if "flash" in decision.model.lower() else "pro",
                   "reason": decision.reason, "reflectionExhausted": len(reflection_failures) >= 3}
        prompt = self._release_risk_prompt(agent_input)
        started = datetime.now()
        try:
            content = await self.llm.complete(prompt, system="You are a careful senior Java backend code reviewer.", model=decision.model)
            payload = self._json_payload(content)
            if not payload:
                raise ValueError("Release risk LLM did not return a JSON object")
            report = {"repositoryPath": baseline_report.get("repositoryPath", ""),
                      "changeRef": baseline_report.get("changeRef", ""),
                      "riskLevel": str(payload.get("riskLevel") or ""),
                      "impactScopes": self._release_string_list(payload.get("impactScopes")),
                      "riskPoints": self._release_string_list(payload.get("riskPoints")),
                      "regressionFocus": self._release_string_list(payload.get("regressionFocus")),
                      "onlineObservationMetrics": self._release_string_list(payload.get("onlineObservationMetrics")),
                      "rollbackFocus": self._release_string_list(payload.get("rollbackFocus")),
                      "knowledgeReferences": self._release_string_list(payload.get("knowledgeReferences"))}
            review = self._release_code_review(payload)
            raw_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            usage = self.cost_control.estimate(decision.tier, decision.model, prompt, content, "release_risk_analysis")
            return {"success": True, "fallback": False, "report": report, "baselineReport": baseline_report,
                    "reasoning": self._release_string_list(payload.get("reasoning")),
                    "humanApprovalPoints": self._release_string_list(payload.get("humanApprovalPoints")),
                    "codeReview": review, "reviewVerdict": str(payload.get("reviewVerdict") or "ACCEPT_WITH_HUMAN_REVIEW"),
                    "qualityScore": self._release_int(payload.get("qualityScore")),
                    "patchDecision": str(payload.get("patchDecision") or "HUMAN_REVIEW"), "modelRouting": routing,
                    "rawContent": raw_content, "errorMessage": "", "costMillis": self._release_cost_millis(started),
                    "llmUsage": usage}
        except Exception as exc:
            reason = str(exc)
            return {"success": False, "fallback": True, "report": baseline_report, "baselineReport": baseline_report,
                    "reasoning": [], "humanApprovalPoints": ["Release risk LLM failed: " + reason],
                    "codeReview": self._release_fallback_code_review("LLM_FAILED", reason),
                    "reviewVerdict": "REVIEW_UNAVAILABLE", "qualityScore": 0, "patchDecision": "HUMAN_REVIEW",
                    "modelRouting": routing, "rawContent": "", "errorMessage": reason,
                    "costMillis": self._release_cost_millis(started), "llmUsage": {}}

    @staticmethod
    def _release_cost_millis(started: datetime) -> int:
        return int((datetime.now() - started).total_seconds() * 1000)

    def _release_agent_unavailable(self, reason: str, baseline_report: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "fallback": True, "report": baseline_report, "baselineReport": baseline_report,
                "reasoning": [], "humanApprovalPoints": [reason],
                "codeReview": {"reviewVerdict": "REVIEW_UNAVAILABLE", "qualityScore": 0,
                               "patchDecision": "HUMAN_REVIEW", "reason": reason},
                "reviewVerdict": "REVIEW_UNAVAILABLE", "qualityScore": 0, "patchDecision": "HUMAN_REVIEW",
                "modelRouting": {}, "rawContent": "", "errorMessage": reason, "costMillis": 0, "llmUsage": {}}

    def _release_code_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"reviewVerdict": str(payload.get("reviewVerdict") or "ACCEPT_WITH_HUMAN_REVIEW"),
                "qualityScore": self._release_int(payload.get("qualityScore")),
                "deterministicScore": self._release_int(payload.get("deterministicScore")),
                "semanticScore": self._release_int(payload.get("semanticScore")),
                "patchDecision": str(payload.get("patchDecision") or "HUMAN_REVIEW"),
                "rootCauseAddressed": self._release_bool_or_none(payload.get("rootCauseAddressed")),
                "workaround": self._release_bool_or_none(payload.get("workaround")),
                "minimalChange": self._release_bool_or_none(payload.get("minimalChange")),
                "scopeSafe": self._release_bool_or_none(payload.get("scopeSafe")),
                "testSufficient": self._release_bool_or_none(payload.get("testSufficient")),
                "businessRisks": self._release_string_list(payload.get("businessRisks")),
                "concurrencyRisks": self._release_string_list(payload.get("concurrencyRisks")),
                "reviewFindings": self._release_string_list(payload.get("reviewFindings")),
                "mustReview": self._release_string_list(payload.get("mustReview"))}

    def _release_fallback_code_review(self, verdict: str, reason: str) -> dict[str, Any]:
        return {"reviewVerdict": verdict, "qualityScore": 0, "deterministicScore": 0, "semanticScore": 0,
                "patchDecision": "HUMAN_REVIEW", "reviewFindings": [reason], "mustReview": [reason]}

    @staticmethod
    def _release_risk_prompt(agent_input: dict[str, Any]) -> str:
        return """You are a senior Java backend code reviewer and release risk agent.

Analyze the actual incident, code localization, patch proposal, deterministic patch facts,
test verification, diff summary, and engineering knowledge. Your primary job is independent
code review of the LLM-generated patch. Release risk is part of the same reviewer decision.

Important rules:
- Output only JSON.
- Use the baselineReport only as a safety checklist, not as the final answer.
- Ground every review conclusion in opsEvidence, fixStrategy, codeLocalization, patchGeneration,
  patchFacts, testVerification, changedFiles, reflectionFailures, or knowledgeMatches.
- You are not the patch author. Do not defend the patch. Review it as an independent senior reviewer.
- patchFacts are deterministic facts. Do not contradict them. If tests failed, compile failed,
  Guard failed, sensitive files changed, or tests were weakened, the patch cannot be release-ready.
- Judge root-cause coverage, workaround status, minimality, test sufficiency, and business/concurrency safety.
- If fixStrategy says shouldEnterCodeRepair=false, produce operational/config/capacity risk rather than a code patch review.
- Failed verification, reflection exhaustion, patch rollback, or missing generated tests must yield a failed-verification
  release risk report with confidence, failing command/error summary, manual takeover checklist, rollback plan and metrics.
- Do not invent services, files, metrics, tests, or rollback procedures not implied by the input.
- If tests are missing or the patch is not validated, mark human approval points clearly.

Return JSON matching this schema:
{"reviewVerdict":"ACCEPT|ACCEPT_WITH_HUMAN_REVIEW|RETRY_REPAIR|REJECT|NO_CODE_FIX","qualityScore":0,
"deterministicScore":0,"semanticScore":0,"patchDecision":"RELEASE_READY|HUMAN_REVIEW|RETRY_REPAIR|REJECT|NO_CODE_FIX",
"rootCauseAddressed":true,"workaround":false,"minimalChange":true,"scopeSafe":true,"testSufficient":true,
"businessRisks":["string"],"concurrencyRisks":["string"],"reviewFindings":["string"],"mustReview":["string"],
"riskLevel":"LOW|MEDIUM|HIGH","impactScopes":["string"],"riskPoints":["string"],"regressionFocus":["string"],
"onlineObservationMetrics":["string"],"rollbackFocus":["string"],"knowledgeReferences":["string"],
"reasoning":["string"],"humanApprovalPoints":["string"]}

Release risk input:
""" + json.dumps(agent_input, ensure_ascii=False, default=str, separators=(",", ":"))

    async def _summarize(self, state: CodeOpsState) -> dict[str, Any]:
        waiting = state.get("approval_required", False) and state.get("approval", {}).get("status") == "PENDING"
        exhausted = bool(state["context"].get("incidentFixReflectionExhausted"))
        stop_reason = state.get("stop_reason", "")
        latest_failed = any(next((step.get("status") for step in reversed(state["steps"])
                                  if step.get("selectedSkill") == skill), None) == "FAILED"
                            for skill in ("bug_fix", "test_verification"))
        failed = exhausted or "工具调用预算" in stop_reason or "最大执行轮数" in stop_reason or latest_failed
        status = "FAILED" if failed else "WAITING_APPROVAL" if waiting else "COMPLETED"
        raw = self._latest_raw_outputs(state)
        evidence = raw.get("evidenceCoverage") if isinstance(raw.get("evidenceCoverage"), dict) else {}
        if "realEvidenceCoverage" not in evidence:
            diagnosis = raw.get("opsDiagnosis") if isinstance(raw.get("opsDiagnosis"), dict) else {}
            evidence = diagnosis.get("evidenceCoverage") if isinstance(diagnosis.get("evidenceCoverage"), dict) else {}
        quality = raw.get("patchQuality") if isinstance(raw.get("patchQuality"), dict) else {}
        sandbox = raw.get("patchSandbox") if isinstance(raw.get("patchSandbox"), dict) else {}
        approval = state.get("approval") or {}
        guardrail = {"realEvidenceCoverage": evidence.get("realEvidenceCoverage", 0.0),
                     "fixtureFallbackUsed": evidence.get("fixtureFallbackUsed", False),
                     "patchSandboxMode": sandbox.get("mode", ""),
                     "patchSandboxIsolated": sandbox.get("isolated", False),
                     "patchStaticSafetyPassed": quality.get("staticSafetyPassed", False),
                     "minimalChangeScore": quality.get("minimalChangeScore", 0),
                     "testsPassed": self._real_tests_passed(raw),
                     "approvalStatus": approval.get("status", "NOT_REQUIRED_OR_NOT_SUBMITTED"),
                     "approvalReasons": approval.get("approvalReasons", [])}
        memory = {**state.get("working_memory", {}), "safetySummary": guardrail}
        context = {**state["context"], "guardrailSummary": guardrail, "incidentFixWorkingMemory": memory}
        outcome = "执行失败或未收敛" if status == "FAILED" else "执行完成"
        summary = (f"CodeOps Incident-to-Fix 任务{outcome}：taskType={state['task']['taskType']}，"
                   f"steps={len(state['steps'])}，usedToolCalls={state['tool_calls']}，"
                   f"realEvidenceCoverage={guardrail['realEvidenceCoverage']}，"
                   f"patchSandboxIsolated={str(guardrail['patchSandboxIsolated']).lower()}，"
                   f"patchStaticSafetyPassed={str(guardrail['patchStaticSafetyPassed']).lower()}，"
                   f"minimalChangeScore={guardrail['minimalChangeScore']}，approvalStatus={guardrail['approvalStatus']}。"
                   "当前已由 Orchestrator 根据 IncidentFixWorkingMemory 逐步选择 Agent，并将线上诊断、代码定位、修复生成、"
                   "测试验证和发布风险产物写入共享记忆。")
        task = {**state["task"], "status": status, "finalSummary": summary, "steps": state["steps"],
                "usedToolCalls": state["tool_calls"], "context": context, "updateTime": now_iso()}
        if state.get("approval"):
            task["approval"] = state["approval"]
        if self.memory:
            await self.memory.remember(task)
        return {"task": task, "status": status, "final_summary": summary}

    def _loop_prompt(self, request: dict[str, Any], steps: list[dict[str, Any]]) -> str:
        completed = len({item.get("turnNo") for item in steps})
        payload = {"goal": request.get("goal", ""), "repository": request.get("repository", ""),
                   "changeRef": request.get("changeRef", ""), "focusAreas": request.get("focusAreas", []),
                   "maxTurns": request.get("maxTurns", 0), "completedTurns": completed,
                   "remainingTurns": max(0, int(request.get("maxTurns", 0)) - completed),
                   "availableTools": self.engineering_tools.list_registered_tools(),
                   "metadata": request.get("context", {}), "previousSteps": steps}
        return ("You are the CodeOps agent loop planner inside an engineering diagnosis harness. Decide the next "
                "tool call(s) or produce a final answer. Use only tools listed in availableTools. Prefer read-only "
                "repository tools before command execution. Cite concrete files, methods, snippets and tool "
                "observations. Do not call more than 3 tools in one turn. If remainingTurns <= 1, produce a final "
                "answer and no tools. Return JSON only with thoughtSummary, toolCalls and finalAnswer. finalAnswer "
                "must contain summary, fixStrategy, scopeDecision, rootCauseLocationType, directEvidenceFiles, "
                "relatedFiles, rootCauseCandidateFiles, doNotModifyFiles, targetFiles, targetMethods, "
                "supportingCodeEvidence, negativeEvidence, reasoning, recommendedTests, shouldEnterCodeRepair, "
                "localizationConfidence and missingEvidence.\nRuntime input:\n"
                + json.dumps(payload, ensure_ascii=False, default=str))

    @staticmethod
    def _mock_loop_decision(request: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
        if not steps:
            goal = str(request.get("goal") or "").lower()
            keyword = "OrderService" if "orderservice" in goal else "Order" if "order" in goal else "Service"
            return {"thoughtSummary": "Dry-run first turn: search repository text for goal keywords.",
                    "toolCalls": [{"toolName": "repo.search_text", "arguments": {
                        "repository": request.get("repository", ""), "queries": [keyword], "maxMatches": 20}}]}
        last = steps[-1]
        tool_result = last.get("toolResult") or {}
        final = {"summary": f"Dry-run agent loop completed. Last tool={last.get('toolName')}, "
                             f"status={tool_result.get('status', '')}, summary={tool_result.get('summary', '')}",
                 "targetFiles": ["src/main/java/com/example/order/OrderServiceApplication.java"],
                 "recommendedTests": ["src/test/java/com/example/order/OrderServiceApplicationTests.java"],
                 "shouldEnterCodeRepair": True, "localizationConfidence": "MEDIUM",
                 "missingEvidence": ["dry-run uses a deterministic mock summary instead of model reasoning"]}
        return {"thoughtSummary": "Dry-run second turn: summarize the observed tool result.", "final": True,
                "finalAnswer": json.dumps(final, ensure_ascii=False, separators=(",", ":"))}

    @classmethod
    def _parse_loop_decision(cls, content: str) -> dict[str, Any]:
        parsed = cls._json_payload(content)
        if not parsed:
            return {"thoughtSummary": "Failed to parse model JSON", "final": True,
                    "finalAnswer": "模型输出无法解析为 agent loop JSON：invalid JSON\n原始输出：" + content[:1200]}
        raw_calls = parsed.get("toolCalls", parsed.get("tool_calls", []))
        calls = [{"toolCallId": item.get("toolCallId") or f"tool-call-{uuid.uuid4()}",
                  "toolName": item["toolName"], "arguments": dict(item.get("arguments") or {})}
                 for item in raw_calls if isinstance(item, dict) and str(item.get("toolName") or "").strip()
                 ] if isinstance(raw_calls, list) else []
        answer = parsed.get("finalAnswer", parsed.get("final_answer", ""))
        if isinstance(answer, dict):
            answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
        return {"thoughtSummary": parsed.get("thoughtSummary", parsed.get("thought_summary", "")),
                "toolCalls": calls, "final": bool(str(answer or "").strip()), "finalAnswer": str(answer or "")}

    @staticmethod
    def _json_payload(value: str) -> dict[str, Any]:
        text = str(value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        try:
            result = json.loads(text)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return [] if value is None or not str(value).strip() else [str(value)]

    @staticmethod
    def _boolean(value: Any, fallback: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return fallback
        return str(value).lower() == "true"

    @staticmethod
    def _service_from_goal(value: str) -> str:
        match = re.search(r"([a-zA-Z0-9_-]+(?:-service|-app|-gateway))", str(value or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _search_terms(goal: str, context: dict[str, Any]) -> list[str]:
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.$/-]{2,}", goal)
        for key in ("traceId", "endpoint", "alertName", "serviceName"):
            if context.get(key):
                tokens.append(str(context[key]))
        stop = {"the", "and", "for", "with", "from", "this", "that", "please", "review", "repository"}
        return list(dict.fromkeys(token for token in tokens if token.lower() not in stop))[:30]

    @staticmethod
    def _proposal(data: dict[str, Any]) -> PatchProposal:
        return PatchProposal.from_llm(json.dumps(data, ensure_ascii=False))

    @staticmethod
    def _step(no: int, skill: str, status: str, summary: str) -> dict[str, Any]:
        return {"stepNo": no, "decision": "EXECUTE", "selectedSkill": skill, "reason": summary[:500],
                "expectedEvidence": [], "resultSummary": summary, "rawEvidenceJson": "{}", "status": status}
