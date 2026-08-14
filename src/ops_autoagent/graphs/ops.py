from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..llm import OpenAICompatibleClient
from ..ops import (EvidenceReviewer, EvidenceSignalExtractor, HistoricalMemoryService, InvestigationPlanner,
                   OpsAgentSkillService, OpsMultiChatAgentService, RunbookRagService, SensitiveMasker, ToolGovernance)
from ..schemas import IncidentAnalyzeRequest, now_iso
from ..store import Store
from ..tools import ObservabilityTools


class OpsState(TypedDict, total=False):
    state_id: str
    diagnosis_id: str
    session_id: str
    request: dict[str, Any]
    status: str
    intent: dict[str, Any]
    metrics: dict[str, Any]
    logs: dict[str, Any]
    traces: dict[str, Any]
    evidence: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    runbooks: list[dict[str, Any]]
    report: str
    error: str
    events: list[dict[str, Any]]
    plan: dict[str, Any]
    historical_memories: list[dict[str, Any]]
    matched_skills: list[dict[str, Any]]
    evidence_review: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    review_round: int


class OpsDiagnosisGraph:
    def __init__(self, tools: ObservabilityTools, llm: OpenAICompatibleClient, store: Store | None = None,
                 checkpointer: Any | None = None):
        self.tools = tools
        self.llm = llm
        self.store = store
        self.planner = InvestigationPlanner()
        self.signal_extractor = EvidenceSignalExtractor()
        self.reviewer = EvidenceReviewer()
        self.memory = HistoricalMemoryService(store) if store else None
        disabled = {item.strip() for item in tools.settings.ops_tool_policy_disabled_tools.split(",") if item.strip()}
        self.governance = (ToolGovernance(store, disabled, tools.settings.ops_tool_policy_max_repeat_per_tool)
                           if store and tools.settings.ops_agent_tool_policy_enabled else None)
        self.runbook_rag = RunbookRagService(tools.settings)
        self.chat_agents = OpsMultiChatAgentService(tools.settings, store, llm)
        self.skills = OpsAgentSkillService(tools.settings)
        self.masker = SensitiveMasker()
        builder = StateGraph(OpsState)
        builder.add_node("understand_incident", self._understand)
        builder.add_node("collect_metrics", self._metrics)
        builder.add_node("collect_logs", self._logs)
        builder.add_node("collect_traces", self._traces)
        builder.add_node("correlate_evidence", self._correlate)
        builder.add_node("supplement_evidence", self._supplement)
        builder.add_node("retrieve_runbooks", self._runbooks)
        builder.add_node("generate_report", self._report)
        builder.add_edge(START, "understand_incident")
        builder.add_edge("understand_incident", "collect_metrics")
        builder.add_edge("collect_metrics", "collect_logs")
        builder.add_edge("collect_logs", "collect_traces")
        builder.add_edge("collect_traces", "retrieve_runbooks")
        builder.add_edge("retrieve_runbooks", "correlate_evidence")
        builder.add_conditional_edges("correlate_evidence", self._route_review,
                                      {"supplement": "supplement_evidence", "continue": "generate_report"})
        builder.add_edge("supplement_evidence", "correlate_evidence")
        builder.add_edge("generate_report", END)
        self.graph = builder.compile(checkpointer=checkpointer or InMemorySaver())

    async def invoke(self, request: IncidentAnalyzeRequest, *, session_id: str | None = None,
                     diagnosis_id: str | None = None) -> OpsState:
        initial = self.create_state(request, session_id=session_id, diagnosis_id=diagnosis_id)
        await self._persist_incident_state(initial)
        result = await self.graph.ainvoke(initial, {"configurable": {"thread_id": initial["diagnosis_id"]}})
        await self._persist_incident_state(result)
        return result

    def create_state(self, request: IncidentAnalyzeRequest, *, session_id: str | None = None,
                     diagnosis_id: str | None = None) -> OpsState:
        diagnosis_id = diagnosis_id or f"diag-{uuid.uuid4()}"
        session_id = session_id or str(uuid.uuid4())
        return {
            "state_id": f"state-{uuid.uuid4()}",
            "diagnosis_id": diagnosis_id,
            "session_id": session_id,
            "request": request.model_dump(by_alias=True),
            "status": "RUNNING",
            "review_round": 0,
            "tool_trace": [],
            "events": [],
        }

    async def stream(self, initial: OpsState) -> AsyncIterator[tuple[dict[str, Any] | None, OpsState | None]]:
        await self._persist_incident_state(initial)
        emitted, final = 0, initial
        try:
            async for state in self.graph.astream(
                initial, {"configurable": {"thread_id": initial["diagnosis_id"]}}, stream_mode="values"
            ):
                final = state
                events = state.get("events", [])
                for event in events[emitted:]:
                    yield event, None
                emitted = len(events)
        except Exception as exc:
            final = {**final, "status": "FAILED", "error": str(exc)}
            await self._persist_incident_state(final)
            yield self._event("error", "diagnosis_error", None,
                              f"Ops incident diagnosis failed: {exc}", initial["session_id"], True), final
            return
        await self._persist_incident_state(final)
        yield None, final

    async def _understand(self, state: OpsState) -> dict[str, Any]:
        req = state["request"]
        problem = str(req["problem"])
        tokens = sorted(set(re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{2,}", problem.lower())))
        intent = {"service": req["serviceName"], "problem": problem, "keywords": tokens[:20],
                  "window": {"start": req["startTime"], "end": req["endTime"]}}
        plan = self.planner.build(req, int(req.get("maxStep", 10)))
        plan.update({"diagnosisId": state["diagnosis_id"], "stateId": state["state_id"], "round": 1})
        memories = await self.memory.recall(req["serviceName"], problem) if self.memory else []
        matched_skills = self.skills.match(json.dumps(req, ensure_ascii=False), 3)
        plan["matchedSkills"] = matched_skills
        plan["requiredTools"] = list(dict.fromkeys(
            [step["tool"] for step in plan["steps"]] + self.skills.recommended_tools(matched_skills)))
        if self.tools.settings.ops_agent_planner_enabled and self.tools.settings.ops_agent_chat_enabled:
            output = await self.chat_agents.call(
                "PLANNER",
                "Return JSON with hypotheses, steps, requiredTools, expectedEvidence, riskLevel and rationale.\n"
                + json.dumps({"request": req, "ruleBasedPlan": plan, "historicalMemories": memories},
                             ensure_ascii=False),
                "You are the planning agent for an ops incident. Produce a structured investigation plan.",
            )
            parsed = self._json_object(output.get("content", "")) if output.get("success") else {}
            self._enforce_required_chat("PLANNER", output, parsed)
            for key in ("hypotheses", "steps", "requiredTools", "expectedEvidence", "riskLevel"):
                if parsed.get(key):
                    plan[key] = parsed[key]
            plan["plannerType"] = "CHAT_AGENT" if parsed else "RULE_BASED"
            plan["chatAgent"] = {**output, "rationale": parsed.get("rationale", ""),
                                 "fallbackReason": "" if parsed else output.get("errorMessage", "invalid output")}
        if self.store:
            await self.store.put("plans", plan["planId"], plan, plan["updateTime"])
        return {"intent": intent, "plan": self._plan_status(plan, "intent", "COMPLETED"),
                "historical_memories": memories, "matched_skills": matched_skills,
                "events": state["events"] + [self._event(
                    "analysis", "intent", 1,
                    f"Start diagnosing service [{req['serviceName']}] in window "
                    f"[{req['startTime']} ~ {req['endTime']}]. Problem: {req['problem']}", state["session_id"])]}

    async def _metrics(self, state: OpsState) -> dict[str, Any]:
        req = state["request"]
        started = time.perf_counter()
        allowed, trace = await self._authorize(state, "query_prometheus")
        value = await self.tools.prometheus(req["serviceName"], req["startTime"], req["endTime"],
                                            req.get("fixtureCaseId", ""), req.get("endpoint", ""),
                                            req.get("problem", "")) if allowed else {"available": False, "source": "DENIED", "error": trace[-1]["reason"]}
        await self._record_tool(state, "query_prometheus", {"service": req["serviceName"], "start": req["startTime"], "end": req["endTime"]}, value, started)
        return {"metrics": value, "tool_trace": trace, "plan": self._plan_status(state["plan"], "metrics", "COMPLETED" if value.get("available") else "FAILED"),
                "events": state["events"] + [self._event(
                    "metric", "prometheus", 2, self._evidence_message("Metric", value), state["session_id"])]}

    async def _logs(self, state: OpsState) -> dict[str, Any]:
        req = state["request"]
        started = time.perf_counter()
        allowed, trace = await self._authorize(state, "query_elasticsearch")
        value = await self.tools.elk(req["serviceName"], req["startTime"], req["endTime"], req["problem"], req.get("fixtureCaseId", "")) if allowed else {"available": False, "source": "DENIED", "error": trace[-1]["reason"]}
        await self._record_tool(state, "query_elasticsearch", {"service": req["serviceName"], "start": req["startTime"], "end": req["endTime"], "problem": req["problem"]}, value, started)
        return {"logs": value, "tool_trace": trace, "plan": self._plan_status(state["plan"], "logs", "COMPLETED" if value.get("available") else "FAILED"),
                "events": state["events"] + [self._event(
                    "log", "elk", 3, self._evidence_message("Log", value), state["session_id"])]}

    async def _traces(self, state: OpsState) -> dict[str, Any]:
        req = state["request"]
        started = time.perf_counter()
        allowed, trace = await self._authorize(state, "query_skywalking_trace")
        value = await self.tools.skywalking(req["serviceName"], req.get("traceId"), req["startTime"], req["endTime"],
                                            req.get("fixtureCaseId", ""), req.get("endpoint", ""),
                                            req.get("problem", "")) if allowed else {"available": False, "source": "DENIED", "error": trace[-1]["reason"]}
        await self._record_tool(state, "query_skywalking_trace", {"service": req["serviceName"], "traceId": req.get("traceId"), "start": req["startTime"], "end": req["endTime"]}, value, started)
        return {"traces": value, "tool_trace": trace, "plan": self._plan_status(state["plan"], "traces", "COMPLETED" if value.get("available") else "FAILED"),
                "events": state["events"] + [self._event(
                    "trace", "skywalking", 4, self._evidence_message("Trace", value), state["session_id"])]}

    async def _correlate(self, state: OpsState) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        for source in ("metrics", "logs", "traces"):
            item = state.get(source, {})
            evidence.append({"source": source.upper(), "available": item.get("available", False),
                             "origin": item.get("source"), "summary": json.dumps(item.get("raw", {}), ensure_ascii=False)[:1200]})
        signals = self.signal_extractor.extract(
            state.get("metrics", {}), state.get("logs", {}), state.get("traces", {}), state["request"])
        review_round = state.get("review_round", 0) + 1
        review = self.reviewer.review(
            state.get("metrics", {}), state.get("logs", {}), state.get("traces", {}),
            state.get("runbooks", []), review_round, self.tools.settings.ops_agent_max_rounds,
            state["request"], self.tools.settings.ops_agent_reviewer_enabled,
            self.tools.settings.ops_agent_reviewer_min_confidence)
        if self.tools.settings.ops_agent_reviewer_enabled and self.tools.settings.ops_agent_chat_enabled:
            output = await self.chat_agents.call(
                "EVIDENCE_REVIEWER",
                "Return JSON only. Include status, round, sufficient, confidenceScore, confirmedFacts, "
                "weakEvidence, missingEvidence, requiredTools, reportConstraints, evidenceSemantics, "
                "sufficiency, conclusionType, rootCause, rootCauseCategory, rootCauseConfidence, "
                "rootCauseRationale, candidateRootCauses and rationale.\n"
                + json.dumps({"incidentContext": state["request"], "evidenceSignals": signals,
                              "runbookMatches": state.get("runbooks", []), "ruleBasedReview": review,
                              "round": review_round, "maxRounds": self.tools.settings.ops_agent_max_rounds},
                             ensure_ascii=False),
                "You are the evidence reviewer agent. Classify neutral signals first. Never turn symptoms "
                "into confirmed root causes; apply the supplied sufficiency rubric.",
            )
            parsed = self._json_object(output.get("content", "")) if output.get("success") else {}
            self._enforce_required_chat("EVIDENCE_REVIEWER", output, parsed)
            if parsed:
                review = self.reviewer.normalize_chat(
                    parsed, review, review_round, self.tools.settings.ops_agent_max_rounds)
                review["rationale"] = (review.get("rationale", "") + "; chatAgent=CHAT_AGENT"
                                       + f", client={output.get('clientBeanName') or 'unknown'}"
                                       + f", resolution={output.get('resolutionSource') or 'unknown'}"
                                       + f", costMillis={output.get('costMillis') or 0}")
            else:
                review["rationale"] += "; chatAgent=RULE_BASED_FALLBACK, reason=Chat Agent unavailable or invalid output"
        review = self._normalize_supplement_requests(state, review)
        candidates = self._reviewer_candidates(state, signals, review)
        if self.store:
            stored_review = {**review, "reviewId": f"review-{uuid.uuid4()}",
                             "diagnosisId": state["diagnosis_id"], "stateId": state["state_id"],
                             "planId": state["plan"]["planId"], "createTime": now_iso(), "updateTime": now_iso()}
            await self.store.put("reviews", stored_review["reviewId"], stored_review,
                                 stored_review["createTime"])
        correlation_events = [
            self._event("analysis", "evidence_signals" if review_round == 1 else f"evidence_signals_round_{review_round}", 5,
                        json.dumps(signals, ensure_ascii=False), state["session_id"]),
            self._event("analysis", "evidence_chain" if review_round == 1 else f"evidence_chain_round_{review_round}", 5,
                        ("Root-cause candidates are not pre-generated by rules. Evidence Reviewer Agent will "
                         "decide from neutral evidence signals and Runbook/RAG context."), state["session_id"]),
            self._event("agent", "evidence_reviewer_agent", 5,
                        self._review_message(review), state["session_id"]),
        ]
        return {"evidence": evidence + signals, "candidates": candidates, "evidence_review": review,
                "review_round": review_round, "plan": self._plan_status(state["plan"], "correlation", "COMPLETED"),
                "events": state["events"] + correlation_events}

    def _route_review(self, state: OpsState) -> Literal["supplement", "continue"]:
        review = state.get("evidence_review", {})
        can_retry = (state.get("review_round", 0) == 1
                     and state.get("review_round", 0) < self.tools.settings.ops_agent_max_rounds)
        within_budget = len(state.get("tool_trace", [])) < self.tools.settings.ops_agent_max_tool_calls
        needs_supplement = (not review.get("sufficient") and review.get("status") == "NEED_MORE_EVIDENCE"
                            and bool(review.get("requiredTools")))
        return "supplement" if needs_supplement and can_retry and within_budget else "continue"

    async def _supplement(self, state: OpsState) -> dict[str, Any]:
        req, trace = state["request"], list(state.get("tool_trace", []))
        updates: dict[str, Any] = {}
        targets = set(state.get("evidence_review", {}).get("requiredTools", []))
        if "query_prometheus" in targets and len(trace) < self.tools.settings.ops_agent_max_tool_calls:
            allowed, trace = await self._authorize({**state, "tool_trace": trace}, "query_prometheus")
            if allowed:
                updates["metrics"] = await self.tools.prometheus(
                    req["serviceName"], req["startTime"], req["endTime"], req.get("fixtureCaseId", ""),
                    req.get("endpoint", ""), req.get("problem", ""))
        if "query_elasticsearch" in targets and len(trace) < self.tools.settings.ops_agent_max_tool_calls:
            allowed, trace = await self._authorize({**state, "tool_trace": trace}, "query_elasticsearch")
            if allowed:
                updates["logs"] = await self.tools.elk(req["serviceName"], req["startTime"], req["endTime"], req["problem"], req.get("fixtureCaseId", ""))
        if "query_skywalking_trace" in targets and len(trace) < self.tools.settings.ops_agent_max_tool_calls:
            allowed, trace = await self._authorize({**state, "tool_trace": trace}, "query_skywalking_trace")
            if allowed:
                updates["traces"] = await self.tools.skywalking(
                    req["serviceName"], req.get("traceId"), req["startTime"], req["endTime"],
                    req.get("fixtureCaseId", ""), req.get("endpoint", ""), req.get("problem", ""))
        refreshed = {**state, **updates}
        signals = self.signal_extractor.extract(refreshed.get("metrics", {}), refreshed.get("logs", {}),
                                                refreshed.get("traces", {}), req)
        terms = [*state.get("intent", {}).get("keywords", []),
                 *(str(item.get("name", "")) for item in signals),
                 *(str(item.get("summary", "")) for item in signals)]
        allowed, trace = await self._authorize({**state, "tool_trace": trace}, "query_runbook")
        runbooks = await self.runbook_rag.search(" ".join(terms), 4) if allowed else []
        skills = self.skills.to_runbook_matches(state.get("matched_skills", []))
        updates["runbooks"] = [*runbooks, *skills]
        return {**updates, "tool_trace": trace,
                "events": state["events"] + [self._event(
                    "rag", "runbook_pattern", 5, json.dumps(updates["runbooks"], ensure_ascii=False),
                    state["session_id"])]}

    async def _runbooks(self, state: OpsState) -> dict[str, Any]:
        allowed, trace = await self._authorize(state, "query_runbook")
        signals = self.signal_extractor.extract(state.get("metrics", {}), state.get("logs", {}),
                                                state.get("traces", {}), state["request"])
        terms = [*state.get("intent", {}).get("keywords", []),
                 *(str(item.get("name", "")) for item in signals),
                 *(str(item.get("summary", "")) for item in signals)]
        runbooks = await self.runbook_rag.search(" ".join(terms), 4) if allowed else []
        events = ([self._event("rag", "runbook_pattern", 5, json.dumps(runbooks, ensure_ascii=False),
                               state["session_id"])] if allowed else [])
        return {"runbooks": runbooks, "tool_trace": trace,
                "plan": self._plan_status(state["plan"], "runbook", "COMPLETED"),
                "events": state["events"] + events}

    async def _report(self, state: OpsState) -> dict[str, Any]:
        runbooks = list(state.get("runbooks", []))
        step6_events: list[dict[str, Any]] = []
        if runbooks:
            step6_events.append(self._event(
                "rag", "runbook", 6,
                f"Runbook patterns were already retrieved before Evidence Reviewer. Reusing matches: {len(runbooks)}",
                state["session_id"]))
        else:
            skill_matches = self.skills.to_runbook_matches(state.get("matched_skills", []))
            runbooks.extend(skill_matches)
            step6_events.extend([
                self._event("rag", "runbook", 6, json.dumps(runbooks, ensure_ascii=False), state["session_id"]),
                self._event("skill", "runbook_skill", 6, json.dumps(skill_matches, ensure_ascii=False),
                            state["session_id"]),
                self._event("analysis", "root_cause", 6,
                            "Evidence chain, Runbook context, and structured ops skills have been generated. "
                            "Ranking root-cause candidates and preparing remediation suggestions.",
                            state["session_id"]),
            ])
        prompt = "Write a concise SRE incident diagnosis in Markdown. Use only this JSON evidence:\n" + json.dumps(
            {"request": state["request"], "plan": state.get("plan"), "evidence": state["evidence"],
             "evidenceReview": state.get("evidence_review"), "historicalMemories": state.get("historical_memories", []),
             "candidates": state["candidates"], "runbooks": runbooks}, ensure_ascii=False
        )
        try:
            output = await self.chat_agents.call(
                "REPORT_WRITER", prompt,
                "You are the report writer agent. Generate a concise diagnosis grounded only in supplied evidence.")
            if not output.get("success"):
                raise RuntimeError(output.get("errorMessage") or "Report Writer Chat Agent returned invalid output")
            parsed = self._json_object(output["content"])
            report = parsed.get("finalReportMarkdown") or output["content"]
            runtime = "CHAT_AGENT"
        except Exception as exc:
            if self.tools.settings.ops_agent_chat_required and not self.tools.settings.ops_fixture_fallback:
                raise RuntimeError(f"Required REPORT_WRITER Chat Agent failed: {exc}") from exc
            top = state["candidates"][0]
            report = (f"# Incident diagnosis\n\n## Summary\n{state['request']['problem']}\n\n"
                      f"## Most likely cause\n{top['cause']} (confidence {top['confidence']}%)\n\n"
                      "## Evidence\n" + "\n".join(f"- {e['source']}: {e.get('origin', e.get('status', 'observed'))}" for e in state["evidence"]) +
                      f"\n\n## Runtime\nDeterministic fallback: {exc}")
            runtime = "DETERMINISTIC_FALLBACK"
        if self.memory:
            top = state["candidates"][0]
            await self.memory.remember({"diagnosisId": state["diagnosis_id"], "serviceName": state["request"]["serviceName"],
                                        "problem": state["request"]["problem"], "report": report, "status": "SUCCESS"},
                                       top["category"], int(top["confidence"]))
        return {"report": report, "runbooks": runbooks, "status": "SUCCESS",
                "plan": self._plan_status(state["plan"], "report", "COMPLETED"),
                "events": state["events"] + step6_events + [
            self._event("agent", "evidence_reviewer_agent", 7,
                        "Evidence Reviewer Agent started. Auditing collected Prometheus, ELK, SkyWalking and "
                        "runbook evidence before report generation.", state["session_id"]),
            self._event("agent", "report_writer_agent", 7,
                        f"Diagnosis report generated ({runtime})", state["session_id"]),
            self._event("report", "diagnosis_report", 7, report, state["session_id"]),
            self._event("review", "diagnosis_record", 7,
                        f"Diagnosis review record saved. diagnosisId={state['diagnosis_id']}", state["session_id"]),
            self._event("complete", "diagnosis_completed", None,
                        "Ops incident diagnosis completed", state["session_id"], True),
        ]}

    def _normalize_supplement_requests(self, state: OpsState, review: dict[str, Any]) -> dict[str, Any]:
        required = review.get("requiredTools") or []
        if not required:
            return review
        available = {
            "query_prometheus": bool(state.get("metrics", {}).get("available")),
            "query_elasticsearch": bool(state.get("logs", {}).get("available")),
            "query_skywalking_trace": bool(state.get("traces", {}).get("available")),
            "query_runbook": state.get("runbooks") is not None,
        }
        collectable = [tool for tool in required if tool in available and not available[tool]]
        suppressed = [tool for tool in required if tool not in collectable]
        if not suppressed:
            return review
        review = {**review, "requiredTools": collectable}
        review["confirmedFacts"] = [*review.get("confirmedFacts", []),
            "Supplement request suppressed because already collected sources returned normal/empty evidence, "
            f"not missing evidence: {suppressed}"]
        review["reportConstraints"] = [*review.get("reportConstraints", []),
            "Treat successfully collected NO_ANOMALY/OK sources as negative evidence; do not re-collect the same "
            "source only because no abnormal records were found."]
        if not collectable and review.get("status") == "NEED_MORE_EVIDENCE":
            review["status"] = "SUFFICIENT"
            review["sufficient"] = True
            review["conclusionType"] = ("PROBABLE_ROOT_CAUSE" if str(review.get("rootCause") or "").strip()
                                         else "INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED")
            review["rationale"] = (review.get("rationale", "")
                + "; supplementGate=SUPPRESSED_ALREADY_COLLECTED_NORMAL_SOURCES, convertedConclusionType="
                + review["conclusionType"] + f", suppressedTools={suppressed}")
        return review

    @staticmethod
    def _reviewer_candidates(state: OpsState, signals: list[dict[str, Any]],
                             review: dict[str, Any]) -> list[dict[str, Any]]:
        root_cause = str(review.get("rootCause") or "").strip()
        if root_cause:
            conclusion = review.get("conclusionType")
            return [{"cause": root_cause,
                     "category": review.get("rootCauseCategory") or (
                         "confirmed_root_cause" if conclusion == "ROOT_CAUSE_CONFIRMED" else
                         "probable_root_cause" if conclusion == "PROBABLE_ROOT_CAUSE" else "agent_reviewed"),
                     "confidence": review.get("rootCauseConfidence", review.get("confidenceScore", 0)),
                     "reasoning": review.get("rootCauseRationale"),
                     "evidences": [*signals, *state.get("runbooks", [])],
                     "remediationSuggestions": review.get("reportConstraints", []),
                     "hypothesis": conclusion != "ROOT_CAUSE_CONFIRMED", "origin": "EVIDENCE_REVIEWER_AGENT",
                     "missingEvidence": review.get("missingEvidence", []),
                     "supportingSignals": [item.get("signalId") for item in signals if item.get("signalId")]}]
        unresolved = review.get("conclusionType") == "INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED"
        return [{"cause": ("Investigation complete; specific root cause is not confirmed by current evidence"
                           if unresolved else "Evidence Reviewer did not finalize a root cause"),
                 "category": ("investigation_complete_root_cause_unresolved" if unresolved
                              else "insufficient_evidence"),
                 "confidence": review.get("confidenceScore", 0), "reasoning": review.get("rationale"),
                 "evidences": [*signals, *state.get("runbooks", [])],
                 "remediationSuggestions": review.get("reportConstraints", []), "hypothesis": True,
                 "origin": "EVIDENCE_REVIEWER_AGENT", "missingEvidence": review.get("missingEvidence", []),
                 "supportingSignals": [item.get("signalId") for item in signals if item.get("signalId")]}]

    @staticmethod
    def _review_message(review: dict[str, Any]) -> str:
        def joined(key: str) -> str:
            values = review.get(key) or []
            return "\n".join(str(item) for item in values) if values else "- none"
        return (f"Evidence Reviewer Agent status={review.get('status')}, "
                f"conclusionType={review.get('conclusionType')}, round={review.get('round')}, "
                f"confidence={review.get('confidenceScore')}\nRoot cause:\n{review.get('rootCause') or '- none'}"
                f"\nConfirmed facts:\n{joined('confirmedFacts')}\nWeak evidence:\n{joined('weakEvidence')}"
                f"\nMissing evidence:\n{joined('missingEvidence')}\nRequired tools:\n{joined('requiredTools')}"
                f"\nReport constraints:\n{joined('reportConstraints')}\nRationale: {review.get('rationale')}")

    async def _authorize(self, state: OpsState, tool: str) -> tuple[bool, list[dict[str, Any]]]:
        trace = list(state.get("tool_trace", []))
        planned = set(state.get("plan", {}).get("requiredTools", []))
        if self.tools.settings.ops_agent_plan_driven_enabled and planned and tool not in planned:
            decision = {"tool": tool, "allowed": False, "reason": "tool is not present in investigation plan",
                        "createTime": now_iso()}
        elif self.governance:
            decision = await self.governance.decide(state["diagnosis_id"], tool, trace)
        else:
            decision = {"tool": tool, "allowed": True, "reason": "governance store is not configured", "createTime": now_iso()}
        trace.append(decision)
        return bool(decision["allowed"]), trace

    async def _record_tool(self, state: OpsState, tool: str, request: dict[str, Any], response: dict[str, Any], started: float) -> None:
        if not self.store:
            return
        record = {"toolCallId": f"tool-{uuid.uuid4()}", "diagnosisId": state["diagnosis_id"],
                  "sessionId": state["session_id"], "toolName": tool, "protocol": response.get("source", "INTERNAL"),
                  "target": state["request"].get("serviceName", ""), "request": self.masker.sanitize(request),
                  "response": self.masker.sanitize(response), "success": bool(response.get("available")),
                  "costMillis": int((time.perf_counter() - started) * 1000), "errorMessage": response.get("error", ""),
                  "createTime": now_iso(), "updateTime": now_iso()}
        await self.store.put("tool_logs", record["toolCallId"], record, record["updateTime"])

    @staticmethod
    def _event(event_type: str, sub_type: str, step: int | None, content: str,
               session_id: str, completed: bool = False) -> dict[str, Any]:
        event: dict[str, Any] = {"type": event_type, "subType": sub_type, "content": content,
                                 "completed": completed, "timestamp": int(time.time() * 1000),
                                 "sessionId": session_id}
        if step is not None:
            event["step"] = step
        return event

    @staticmethod
    def _evidence_message(kind: str, value: dict[str, Any]) -> str:
        labels = {"Metric": "Observations", "Log": "Error samples", "Trace": "Span summary"}
        source = str(value.get("source", kind.lower()))
        status = "available" if value.get("available") else "unavailable"
        summary = value.get("summary") or value.get("error") or "Evidence collected"
        raw = value.get("observations") or value.get("errorSamples") or value.get("spans") or value.get("raw") or []
        detail = "\n".join(str(item) for item in raw) if isinstance(raw, list) else json.dumps(raw, ensure_ascii=False)
        return f"{kind} source: {source}\nStatus: {status}\nSummary: {summary}\n{labels[kind]}:\n{detail}"

    @staticmethod
    def _plan_status(plan: dict[str, Any], step_id: str, status: str) -> dict[str, Any]:
        updated = {**plan, "steps": [{**step, "status": status if step.get("stepId") == step_id else step.get("status", "PENDING")}
                                      for step in plan.get("steps", [])], "updateTime": now_iso()}
        if all(step.get("status") in {"COMPLETED", "FAILED"} for step in updated["steps"]):
            updated["status"] = "COMPLETED"
        return updated

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        text = (value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"\{.*}", text, re.DOTALL)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}

    def _enforce_required_chat(self, role: str, output: dict[str, Any], parsed: dict[str, Any]) -> None:
        if (self.tools.settings.ops_agent_chat_required and not self.tools.settings.ops_fixture_fallback
                and (not output.get("success") or not parsed)):
            detail = output.get("errorMessage") or "Chat Agent returned invalid structured JSON"
            raise RuntimeError(f"Required {role} Chat Agent failed: {detail}")

    async def _persist_incident_state(self, state: OpsState) -> None:
        if not self.store or not self.tools.settings.ops_agent_enabled:
            return
        request = state.get("request", {})
        record = {
            "stateId": state["state_id"], "diagnosisId": state["diagnosis_id"],
            "sessionId": state["session_id"], "eventId": request.get("eventId"),
            "serviceName": request.get("serviceName", "unknown-service"),
            "severity": request.get("severity", ""), "alertRule": request.get("alertRule", ""),
            "timeWindow": {"startTime": request.get("startTime"), "endTime": request.get("endTime")},
            "currentRound": state.get("review_round", 0),
            "maxRounds": self.tools.settings.ops_agent_max_rounds, "plan": state.get("plan", {}),
            "metricsEvidence": state.get("metrics", {}), "logEvidence": state.get("logs", {}),
            "traceEvidence": state.get("traces", {}), "runbookEvidence": state.get("runbooks", []),
            "candidateRootCauses": state.get("candidates", []),
            "missingEvidence": state.get("evidence_review", {}).get("missingEvidence", []),
            "toolHistory": state.get("tool_trace", []),
            "reviewStatus": state.get("evidence_review", {}).get("status", ""),
            "finalReport": state.get("report", ""), "status": state.get("status", "INIT"),
            "errorMessage": state.get("error", ""), "createTime": now_iso(), "updateTime": now_iso(),
        }
        await self.store.put("incident_states", record["stateId"], record, record["updateTime"])
