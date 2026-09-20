"""Reproducible CodeOps case-study demonstration.

By default the demo uses a deterministic local model adapter, which makes the
repository-safe workflow runnable without credentials.  ``--real-llm`` uses
the configured OpenAI-compatible client (for example DeepSeek) while keeping
the same graph, sandbox, verification and approval boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .config import Settings
from .graphs.codeops import CodeOpsGraph
from .llm import OpenAICompatibleClient
from .schemas import ApprovalDecisionContract, CodeOpsTaskRequest


class OfflineDemoLLM:
    """Small local adapter that implements the same interface as the LLM client.

    This is not a fake graph: only model responses are deterministic.  Every
    state transition, tool policy, scope guard, sandbox and approval decision
    still comes from the production CodeOps implementation.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.available = True

    async def complete(self, prompt: str, *, system: str = "", model: str | None = None,
                       max_tokens: int | None = None) -> str:
        if "CodeOps agent loop planner" in prompt:
            return self._agent_loop_response(prompt)
        if "Incident fix input" in prompt:
            return self._bugfix_response()
        if "senior Java backend code reviewer and release risk agent" in prompt:
            return self._review_response()
        if "senior Java backend test verification agent" in prompt:
            return json.dumps({
                "recommendedTests": ["IdempotencyServiceAtomicityTest"],
                "coverageGaps": [],
                "mavenCommands": ["mvn", "-q", "test"],
                "verificationNotes": ["Offline demo uses the committed sample repository."],
                "reasoning": ["The regression test already covers concurrent duplicate submissions."],
            }, ensure_ascii=False)
        return json.dumps({"summary": "Offline demo response", "patches": [], "tests": []}, ensure_ascii=False)

    @staticmethod
    def _agent_loop_response(prompt: str) -> str:
        if '"completedTurns": 0' in prompt:
            return json.dumps({
                "thoughtSummary": "先搜索幂等服务和 requestId 的调用点，再给出定位结论。",
                "toolCalls": [{
                    "toolName": "repo.search_text",
                    "arguments": {"queries": ["IdempotencyService", "requestId"], "maxMatches": 20},
                }],
            }, ensure_ascii=False)
        answer = {
            "summary": "并发提交在 alreadyProcessed 与 markProcessed 之间存在 check-then-act 竞态。",
            "fixStrategy": "CODE_FIX",
            "scopeDecision": "MULTI_METHOD",
            "rootCauseLocationType": "STATE_OWNER_SERVICE",
            "directEvidenceFiles": [
                "src/main/java/com/example/order/IdempotencyService.java",
                "src/main/java/com/example/order/OrderSubmitService.java",
            ],
            "relatedFiles": [],
            "rootCauseCandidateFiles": [
                "src/main/java/com/example/order/IdempotencyService.java",
                "src/main/java/com/example/order/OrderSubmitService.java",
            ],
            "doNotModifyFiles": ["pom.xml", "src/test/java/com/example/order/IdempotencyServiceAtomicityTest.java"],
            "targetFiles": [
                "src/main/java/com/example/order/IdempotencyService.java",
                "src/main/java/com/example/order/OrderSubmitService.java",
            ],
            "targetMethods": [
                "IdempotencyService.alreadyProcessed",
                "IdempotencyService.markProcessed",
                "IdempotencyService.tryMarkProcessed",
                "OrderSubmitService.submitFlashSale",
            ],
            "supportingCodeEvidence": [
                "OrderSubmitService.submitFlashSale performs the check and mark in separate calls.",
                "The existing regression test submits the same requestId concurrently.",
            ],
            "negativeEvidence": ["No configuration or dependency change is required."],
            "reasoning": ["The state owner must expose an atomic check-and-mark operation."],
            "recommendedTests": ["IdempotencyServiceAtomicityTest"],
            "shouldEnterCodeRepair": True,
            "localizationConfidence": "HIGH",
            "missingEvidence": ["confirm exact source blocks before patching"],
        }
        return json.dumps({"thoughtSummary": "只读搜索结果已足够，输出结构化定位结论。",
                           "toolCalls": [], "final": True,
                           "finalAnswer": json.dumps(answer, ensure_ascii=False, separators=(",", ":"))},
                          ensure_ascii=False)

    @staticmethod
    def _bugfix_response() -> str:
        return json.dumps({
            "rootCause": "check-then-act race in idempotency state ownership",
            "confidence": "HIGH",
            "targetFiles": [
                "src/main/java/com/example/order/IdempotencyService.java",
                "src/main/java/com/example/order/OrderSubmitService.java",
            ],
            "reasoning": ["Use one synchronized state-owner operation so only one concurrent request wins."],
            "reflectionDiagnosis": {"failureType": "", "failedFiles": [], "mustFix": [], "mustAvoid": []},
            "scopeDecision": {
                "decision": "KEEP_SCOPE", "finalScopeType": "MULTI_METHOD",
                "finalTargetFiles": [
                    "src/main/java/com/example/order/IdempotencyService.java",
                    "src/main/java/com/example/order/OrderSubmitService.java",
                ],
                "finalTargetMethods": [
                    "IdempotencyService.alreadyProcessed", "IdempotencyService.markProcessed",
                    "IdempotencyService.tryMarkProcessed", "OrderSubmitService.submitFlashSale",
                ],
                "whyKeepOrExpand": ["The check and mark must be changed together."],
                "expectedBehaviorChange": "Duplicate requestId is rejected atomically.", "risk": "LOW",
            },
            "unifiedDiffPatch": "", "fileRewrites": [], "exactReplaceBlocks": [],
            "testSuggestions": ["IdempotencyServiceAtomicityTest"],
            "mavenCommands": ["mvn -q -DskipTests compile", "mvn -q -Dtest=IdempotencyServiceAtomicityTest test"],
            "testUnifiedDiffPatch": "", "testFileRewrites": [],
            "riskNotes": ["Observe duplicate-request rate and 5xx rate after delivery."],
        }, ensure_ascii=False)

    @staticmethod
    def _review_response() -> str:
        return json.dumps({
            "reviewVerdict": "ACCEPT_WITH_HUMAN_REVIEW",
            "patchDecision": "HUMAN_REVIEW",
            "riskLevel": "MEDIUM",
            "rootCauseAddressed": True,
            "scopeSafe": True,
            "testSufficient": True,
            "qualityScore": 92,
            "mustReview": ["Confirm duplicate-request observation metrics before rollout."],
            "humanApprovalPoints": ["Approve delivery of the validated patch artifact."],
            "reviewFindings": [],
            "businessRisks": ["A duplicate request may be rejected earlier than before, as intended."],
            "concurrencyRisks": ["Verify the idempotency key store remains process-consistent."],
            "reasoning": ["The patch is scoped to the state owner and its caller; the committed concurrency test passes."],
        }, ensure_ascii=False)


def _patch_proposal() -> dict[str, Any]:
    return {
        "summary": "将幂等检查与标记合并为原子 tryMarkProcessed，消除并发重复创建订单。",
        "rationale": "The existing check-then-act sequence allows two requests to pass before either marks the key.",
        "tests": ["IdempotencyServiceAtomicityTest"],
        "patches": [
            {
                "path": "src/main/java/com/example/order/IdempotencyService.java",
                "old": """    public synchronized boolean alreadyProcessed(String requestId) {
        return processed.contains(requestId);
    }

    public synchronized void markProcessed(String requestId) {
        processed.add(requestId);
    }""",
                "new": """    public synchronized boolean alreadyProcessed(String requestId) {
        return processed.contains(requestId);
    }

    public synchronized void markProcessed(String requestId) {
        processed.add(requestId);
    }

    public synchronized boolean tryMarkProcessed(String requestId) {
        if (processed.contains(requestId)) {
            return false;
        }
        processed.add(requestId);
        return true;
    }""",
            },
            {
                "path": "src/main/java/com/example/order/OrderSubmitService.java",
                "old": """    public String submitFlashSale(String requestId) {
        if (idempotencyService.alreadyProcessed(requestId)) {
            throw new IllegalStateException("Duplicate requestId " + requestId);
        }
        LockSupport.parkNanos(20_000_000L);
        idempotencyService.markProcessed(requestId);
        return "ORDER-" + requestId;
    }""",
                "new": """    public String submitFlashSale(String requestId) {
        if (!idempotencyService.tryMarkProcessed(requestId)) {
            throw new IllegalStateException("Duplicate requestId " + requestId);
        }
        LockSupport.parkNanos(20_000_000L);
        return "ORDER-" + requestId;
    }""",
            },
        ],
    }


async def run_demo(*, approve: bool = True, real_llm: bool = False) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    repository = root / "samples" / "codeops-eval"
    # The project requires Python 3.11+.  A few older local LangGraph wheels
    # do not propagate the runnable context for async interrupt nodes; keep the
    # offline demo usable on those hosts and let supported runtimes exercise
    # the full approval resume path.
    interrupt_supported = sys.version_info >= (3, 11)
    settings_kwargs: dict[str, Any] = {
        "langgraph_checkpoint_backend": "memory",
        "codeops_test_execution_enabled": True,
        # The committed sample uses Java records and requires JDK 17.  Leave
        # this empty when the host has already configured JAVA_HOME correctly.
        "codeops_java_home": "D:\\Java\\jdk17" if Path("D:/Java/jdk17").is_dir() else "",
        "codeops_hitl_approval_enabled": interrupt_supported,
        "codeops_apply_mode": "delivery_only",
        "codeops_subgraphs_enabled": True,
        "codeops_independent_reviewer_enabled": True,
        # Give the real structured repair response enough room for two
        # complete Java rewrites plus its test/verification contract.
        "codeops_llm_timeout_seconds": 180.0 if real_llm else 120.0,
        "codeops_llm_max_output_tokens": 16384 if real_llm else 2048,
        "codeops_llm_agent_loop_max_output_tokens": 8192 if real_llm else 4096,
        "codeops_llm_structured_output_retries": 2 if real_llm else 1,
        "ops_runbook_vector_enabled": False,
        "ops_demo_auto_seed_enabled": False,
    }
    if real_llm:
        # Keep provider settings explicit for the case-study command, while
        # still allowing a caller to override them with process environment
        # variables.  The API key is never written to a file or printed.
        settings_kwargs.update({
            "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
            "openai_model": os.environ.get("OPENAI_MODEL", "deepseek-flash"),
        })
    settings = Settings(**settings_kwargs)
    if real_llm:
        if not settings.openai_api_key:
            raise RuntimeError("--real-llm requires OPENAI_API_KEY in the current process environment")
        llm = OpenAICompatibleClient(settings)
    else:
        llm = OfflineDemoLLM(settings)
    graph = CodeOpsGraph(llm, store=None, checkpointer=InMemorySaver())
    request = CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX",
        goal="订单提交在并发重试下重复创建订单，请定位幂等竞态并生成安全补丁。",
        repository=str(repository),
        focusAreas=["distributed_consistency", "code_fix"],
        maxRounds=12,
        maxToolCalls=20,
        context={
            "serviceName": "order-service",
            "endpoint": "POST /api/orders/submit",
            "evaluationCaseId": "incident-order-idempotency-race",
            "fixtureDataClass": "TEST_SIMULATED_DATA",
            "fixtureEvidence": {
                "prometheus": {"signal": "duplicate requestId and concurrent order creation"},
                "logs": {"signal": "created two orders for the same requestId"},
                "trace": {"signal": "parallel submit spans overlap before idempotency mark"},
            },
            "allowPatchApply": True,
            "allowTestPatchApply": False,
            "agentLoopMaxTurns": 3,
            "evaluationTestCommands": [
                "mvn -q -DskipTests compile",
                "mvn -q -Dtest=IdempotencyServiceAtomicityTest test",
            ],
        },
    )
    if not real_llm:
        # This explicitly labelled fixture is only for the offline regression
        # path.  Real mode must obtain the patch proposal from the live LLM.
        request.context["evaluationFixturePatchProposal"] = _patch_proposal()

    first = await graph.invoke(request)
    output: dict[str, Any] = {"initial": first}
    state = first
    if "__interrupt__" in first:
        task_id = str(first.get("task", {}).get("taskId") or "")
        decision = ApprovalDecisionContract(
            approved=approve,
            action="APPROVE_DELIVERY" if approve else "REJECT",
            operatorId="offline-demo",
            reason="5-minute demo approval" if approve else "5-minute demo rejection",
        )
        state = await graph.resume(task_id, decision)
        output["resumed"] = state
    agent_loop_step = next((item for item in state.get("steps", [])
                            if item.get("selectedSkill") == "agent_loop_investigation"), {})
    agent_loop_raw: dict[str, Any] = {}
    try:
        parsed_step = json.loads(str(agent_loop_step.get("rawEvidenceJson") or "{}"))
        if isinstance(parsed_step, dict):
            agent_loop_raw = parsed_step
    except (TypeError, ValueError):
        agent_loop_raw = {}
    structured_final = agent_loop_raw.get("structuredFinalAnswer")
    if not isinstance(structured_final, dict):
        structured_final = {}
    release_step = next((item for item in state.get("steps", [])
                         if item.get("selectedSkill") == "release_risk_analysis"), {})
    release_raw: dict[str, Any] = {}
    try:
        parsed_release = json.loads(str(release_step.get("rawEvidenceJson") or "{}"))
        if isinstance(parsed_release, dict):
            release_raw = parsed_release
    except (TypeError, ValueError):
        release_raw = {}
    governance = release_raw.get("riskGovernance") if isinstance(release_raw.get("riskGovernance"), dict) else {}
    output["summary"] = {
        "status": state.get("status"),
        "taskId": state.get("task", {}).get("taskId"),
        "steps": [
            {"skill": step.get("selectedSkill"), "status": step.get("status"),
             "summary": str(step.get("resultSummary") or "")[:180]}
            for step in state.get("steps", [])
        ],
        "changedFiles": [patch.get("path") for patch in (state.get("patch_proposal") or {}).get("patches", [])],
        "verification": state.get("verification", {}),
        "approval": state.get("approval", {}),
        "targetRepositoryWrite": False,
        "approvalRuntime": "enabled" if interrupt_supported else "skipped: Python < 3.11 runtime",
        "llmMode": settings.openai_model if real_llm else "offline-deterministic",
        "realLlm": real_llm,
        "agentLoop": {
            "status": agent_loop_raw.get("status", agent_loop_step.get("status")),
            "turns": agent_loop_raw.get("turns"),
            "stopReason": agent_loop_raw.get("stopReason"),
            "targetFiles": agent_loop_raw.get("targetFiles", []),
            "shouldEnterCodeRepair": agent_loop_raw.get("shouldEnterCodeRepair"),
            "finalAnswerKeys": sorted(structured_final.keys()),
        },
        "riskGovernance": governance,
    }
    return output


def _print_summary(result: dict[str, Any]) -> None:
    summary = result["summary"]
    title = "real DeepSeek case study" if summary.get("realLlm") else "5-minute offline demo"
    print(f"\n=== Ops AutoAgent · {title} ===")
    print(f"llm: {summary.get('llmMode')}")
    loop = summary.get("agentLoop") or {}
    if loop:
        print(f"agent loop: status={loop.get('status')}, turns={loop.get('turns')}, "
              f"stop={loop.get('stopReason')}, shouldRepair={loop.get('shouldEnterCodeRepair')}")
        if loop.get("targetFiles"):
            print(f"agent loop target files: {', '.join(loop.get('targetFiles'))}")
    governance = summary.get("riskGovernance") or {}
    if governance:
        blast = governance.get("blastRadius") if isinstance(governance.get("blastRadius"), dict) else {}
        dry_run = governance.get("dryRunResult") if isinstance(governance.get("dryRunResult"), dict) else {}
        print(f"risk governance: risk={governance.get('riskLevel')}, blast={blast.get('level')}, "
              f"dryRun={dry_run.get('status')}, approvalRequired={governance.get('approvalRequired')}")
    print(f"status: {summary.get('status')}")
    print(f"taskId: {summary.get('taskId')}")
    print("flow:")
    for step in summary.get("steps", []):
        print(f"  - {step.get('skill')}: {step.get('status')} — {step.get('summary')}")
    print("changed files:")
    for path in summary.get("changedFiles", []):
        print(f"  - {path}")
    verification = summary.get("verification") or {}
    if verification:
        print(f"verification: {verification.get('status', 'n/a')}")
    approval = summary.get("approval") or {}
    if approval:
        print(f"approval: {approval.get('status', 'n/a')} / {approval.get('action', 'n/a')}")
    if summary.get("approvalRuntime") != "enabled":
        print(f"approval runtime: {summary.get('approvalRuntime')}")
    print("target repository write: BLOCKED (delivery_only)")
    if summary.get("realLlm"):
        print("\n说明：本次运行使用了配置的 OpenAI-compatible LLM；线上指标仍使用仓库内明确标注的可复现 fixture，代码写入保持 delivery_only。")
    else:
        print("\n说明：这是离线演示模型，证据来自仓库内的可复现 fixture；生产环境可替换为真实 LLM 和观测系统。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the zero-config Ops AutoAgent CodeOps demo.")
    parser.add_argument("--reject", action="store_true", help="resume the approval interrupt with a rejection")
    parser.add_argument("--real-llm", action="store_true",
                        help="use OPENAI_API_KEY/OPENAI_BASE_URL with the configured OpenAI-compatible model")
    args = parser.parse_args()
    result = asyncio.run(run_demo(approve=not args.reject, real_llm=args.real_llm))
    _print_summary(result)


if __name__ == "__main__":
    main()
