"""Domain subgraphs used by the CodeOps control plane.

The parent graph owns routing, budgets, approval and effects.  These subgraphs
only normalize their domain contract and return references to durable artifacts.
The callback is deliberately injected so the legacy skill implementations can
be reused without renaming their checkpoint-visible parent nodes.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..schemas import (
    EvidenceReviewContract,
    InvestigationDecisionContract,
    PatchProposalContract,
    ReleaseReviewContract,
)
from .state_models import digest_json


class SubgraphState(TypedDict, total=False):
    input: dict[str, Any]
    output: dict[str, Any]
    artifact_refs: list[str]
    status: str
    blocked_reason: str


Runner = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


class _ContractSubgraph:
    name = "subgraph"
    node_name = "contract"
    allowed_tools: tuple[str, ...] = ()
    effect_boundary = "READ_ONLY"

    def __init__(self, runner: Runner | None = None, checkpointer: Any | None = None):
        self.runner = runner or self._default_runner
        builder = StateGraph(SubgraphState)
        builder.add_node("prepare_input", self._prepare_input)
        builder.add_node(self.node_name, self._execute)
        builder.add_node("publish_contract", self._publish_contract)
        builder.add_edge(START, "prepare_input")
        builder.add_edge("prepare_input", self.node_name)
        builder.add_edge(self.node_name, "publish_contract")
        builder.add_edge("publish_contract", END)
        self.graph = builder.compile(checkpointer=checkpointer or InMemorySaver())

    async def ainvoke(self, input_state: dict[str, Any], *, thread_id: str = "") -> dict[str, Any]:
        value = {"input": dict(input_state or {})}
        config = {"configurable": {"thread_id": thread_id or self._thread_id(input_state)}}
        started = time.perf_counter()
        result = dict(await self.graph.ainvoke(value, config))
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    async def checkpoint(self, thread_id: str) -> Any:
        return await self.graph.aget_state({"configurable": {"thread_id": thread_id}})

    async def _prepare_input(self, state: SubgraphState) -> dict[str, Any]:
        return {"status": "RUNNING"}

    async def _execute(self, state: SubgraphState) -> dict[str, Any]:
        value = self.runner(dict(state.get("input") or {}))
        output = await value if hasattr(value, "__await__") else value
        return {"output": dict(output or {})}

    async def _publish_contract(self, state: SubgraphState) -> dict[str, Any]:
        input_state = state.get("input") or {}
        output = self._validate_output(dict(state.get("output") or {}), input_state)
        artifact_id = self._artifact_id(input_state, output)
        output.setdefault("artifactRefs", []).append(artifact_id)
        output["subgraph"] = self.name
        output["effectBoundary"] = self.effect_boundary
        return {"output": output, "artifact_refs": [artifact_id], "status": output.get("status", "COMPLETED"),
                "blocked_reason": output.get("blockedReason", "")}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        return output

    @staticmethod
    def _artifact_id(input_state: dict[str, Any], output: dict[str, Any]) -> str:
        digest = hashlib.sha256(digest_json({"input": input_state, "output": output}).encode()).hexdigest()[:20]
        return "artifact-" + digest

    @staticmethod
    def _thread_id(input_state: dict[str, Any]) -> str:
        return "subgraph-" + hashlib.sha256(digest_json(input_state).encode()).hexdigest()[:20]

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        return {"status": "COMPLETED", "summary": "No callback was configured."}


class OpsEvidenceSubgraph(_ContractSubgraph):
    name = "ops_evidence"
    node_name = "collect_and_review_evidence"
    allowed_tools = ("query_prometheus", "query_elasticsearch", "query_skywalking_trace", "query_runbook")

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        bundle = input_state.get("evidenceBundle") if isinstance(input_state.get("evidenceBundle"), dict) else {}
        available = any(isinstance(bundle.get(key), dict) and bundle[key].get("available")
                        for key in ("metrics", "logs", "traces")) or bool(bundle.get("signals") or bundle.get("runbooks"))
        return {"status": "SUFFICIENT" if available else "NEED_MORE_EVIDENCE",
                "evidenceBundle": bundle or {"artifactRefs": [], "signals": []},
                "summary": "Evidence contract adapter completed."}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        review_payload = dict(output.get("evidenceReview") or output)
        if "sufficient" not in review_payload:
            review_payload["sufficient"] = review_payload.get("status") == "SUFFICIENT"
        review = EvidenceReviewContract.model_validate(review_payload)
        output["evidenceReview"] = review.model_dump(by_alias=True)
        output.setdefault("evidenceBundle", {"artifactRefs": output.get("artifactRefs", []), "signals": []})
        output["status"] = review.status
        if review.status != "SUFFICIENT":
            output["blockedReason"] = review.status
        return output


class RepositoryInvestigationSubgraph(_ContractSubgraph):
    name = "repository_investigation"
    node_name = "readonly_investigation"
    allowed_tools = ("repo.create_snapshot", "repo.search_text", "repo.list_files", "repo.read_file_snippet",
                     "repo.git_diff", "repo.git_log", "repo.find_tests", "knowledge.search")

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        files = list(input_state.get("targetFiles") or [])
        methods = list(input_state.get("targetMethods") or [])
        return {"status": "LOCALIZED" if files else "LOCALIZATION_BLOCKED", "targetFiles": files,
                "targetMethods": methods, "shouldEnterCodeRepair": bool(files),
                "missingEvidence": [] if files else ["read-only investigation callback"]}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        decision = InvestigationDecisionContract.model_validate(output)
        normalized = decision.model_dump(by_alias=True)
        normalized["status"] = decision.status
        if not decision.target_files or not decision.should_enter_code_repair:
            normalized["shouldEnterCodeRepair"] = False
            if decision.status == "LOCALIZED":
                normalized["status"] = "NEED_MORE_EVIDENCE"
        return {**output, **normalized}


class RepairProposalSubgraph(_ContractSubgraph):
    name = "repair_proposal"
    node_name = "sandbox_repair_proposal"
    allowed_tools = ("sandbox.patch", "sandbox.validate", "sandbox.compile")
    effect_boundary = "MANAGED_PATCH_SANDBOX_ONLY"

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        proposal = input_state.get("patchProposal") if isinstance(input_state.get("patchProposal"), dict) else {}
        return {**proposal, "status": proposal.get("status") or ("PROPOSED" if proposal.get("patches") else "NO_CODE_FIX"),
                "patchDigest": input_state.get("patchDigest", ""), "scopeGuard": proposal.get("scopeGuard", {"passed": True})}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        contract = PatchProposalContract.model_validate(output)
        normalized = contract.model_dump(by_alias=True)
        normalized["patchDigest"] = str(output.get("patchDigest") or digest_json(output))
        normalized["status"] = output.get("status") or ("PROPOSED" if output.get("patches") else "NO_CODE_FIX")
        return {**output, **normalized}


class VerificationSubgraph(_ContractSubgraph):
    name = "verification"
    node_name = "run_verification"
    allowed_tools = ("repo.maven", "repo.maven_background", "task.background_status")

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        verification = input_state.get("verification") if isinstance(input_state.get("verification"), dict) else {}
        return {**verification, "status": verification.get("status", "SKIPPED"),
                "testsPassed": bool(verification.get("testsPassed", False)),
                "backgroundTaskState": verification.get("backgroundTaskState", "NOT_STARTED")}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        status = str(output.get("status") or "SKIPPED").upper()
        passed = bool(output.get("testsPassed")) and status in {"SUCCESS", "PASSED"}
        if status == "SKIPPED":
            passed = False
        return {**output, "status": status, "testsPassed": passed,
                "verificationArtifactRefs": output.get("verificationArtifactRefs", [])}


class IndependentReviewSubgraph(_ContractSubgraph):
    name = "independent_review"
    node_name = "review_patch_facts"
    allowed_tools = ("knowledge.search", "artifact.generate_review_report")

    @staticmethod
    def _default_runner(input_state: dict[str, Any]) -> dict[str, Any]:
        review = input_state.get("review") if isinstance(input_state.get("review"), dict) else {}
        return review or {"reviewVerdict": "REVIEW_UNAVAILABLE", "patchDecision": "HUMAN_REVIEW",
                          "humanApprovalPoints": ["No independent reviewer callback was configured."]}

    def _validate_output(self, output: dict[str, Any], input_state: dict[str, Any]) -> dict[str, Any]:
        try:
            contract = ReleaseReviewContract.model_validate(output.get("review") or output)
        except Exception as exc:
            contract = ReleaseReviewContract(
                reviewVerdict="REVIEW_UNAVAILABLE", patchDecision="HUMAN_REVIEW", riskLevel="UNKNOWN",
                humanApprovalPoints=["Reviewer structured output validation failed: " + type(exc).__name__],
                mustReview=["Human review required before delivery"],
            )
            output["reviewValidationError"] = type(exc).__name__
        facts = input_state.get("patchFacts") if isinstance(input_state.get("patchFacts"), dict) else {}
        unsafe = any(facts.get(key) is False for key in ("scopeGuardPassed", "compilePassed", "testsPassed"))
        if unsafe and contract.review_verdict in {"RELEASE_READY", "ACCEPT"}:
            contract.review_verdict = "ACCEPT_WITH_HUMAN_REVIEW"
            contract.patch_decision = "HUMAN_REVIEW"
            contract.must_review = [*contract.must_review, "Deterministic patch/test facts prevent release-ready routing."]
        normalized = contract.model_dump(by_alias=True)
        normalized["review"] = normalized.copy()
        normalized["reviewFallback"] = contract.review_verdict == "REVIEW_UNAVAILABLE"
        normalized["status"] = contract.review_verdict
        return {**output, **normalized}


__all__ = [
    "SubgraphState", "OpsEvidenceSubgraph", "RepositoryInvestigationSubgraph", "RepairProposalSubgraph",
    "VerificationSubgraph", "IndependentReviewSubgraph",
]
