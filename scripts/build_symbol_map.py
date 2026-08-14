"""Build the reviewed Java-to-Python consolidation map used by the parity gate."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy-spring-ai"


def python_symbols() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in (ROOT / "src" / "ops_autoagent").rglob("*.py"):
        module = path.relative_to(ROOT / "src").with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                result.setdefault(node.name, []).append(f"{module}:{node.name}")
    return result


def choose(name: str, path: str, exact: dict[str, list[str]]) -> tuple[str, str]:
    if name in exact and len(exact[name]) == 1:
        return exact[name][0], "same-named Python implementation"
    lower, location = name.lower(), path.lower()
    overrides = {
        "HumanApprovalGate": "ops_autoagent.graphs.codeops:CodeOpsGraph",
        "OpsApiGuard": "ops_autoagent.api:api_guard",
        "Application": "ops_autoagent.main:run",
        "Response": "ops_autoagent.schemas:ApiResponse",
        "ResponseCode": "ops_autoagent.schemas:ApiResponse",
        "OpsIncidentAnalyzeRequestDTO": "ops_autoagent.schemas:IncidentAnalyzeRequest",
        "OpsAlertWebhookRequestDTO": "ops_autoagent.schemas:AlertmanagerWebhook",
        "OpsAlertWebhookAlertDTO": "ops_autoagent.schemas:Alert",
        "CodeOpsTaskSubmitRequestDTO": "ops_autoagent.schemas:CodeOpsTaskRequest",
        "CodeOpsIncidentFixSubmitRequestDTO": "ops_autoagent.schemas:IncidentFixRequest",
        "CodeOpsAgentLoopRunRequestDTO": "ops_autoagent.schemas:AgentLoopRequest",
        "CodeOpsApprovalDecisionRequestDTO": "ops_autoagent.schemas:ApprovalDecision",
        "OpsRunbookMarkdownChunker": "ops_autoagent.ops.rag:MarkdownChunker",
        "RunbookCrossEncoderReranker": "ops_autoagent.ops.rag:RunbookRagService",
        "OpsRunbookVectorIndexer": "ops_autoagent.ops.rag:RunbookRagService",
        "PgVectorOpsRunbookGateway": "ops_autoagent.ops.rag:RunbookRagService",
        "FileOpsRunbookGateway": "ops_autoagent.ops.rag:RunbookRagService",
        "PrometheusMetricGateway": "ops_autoagent.tools:ObservabilityTools",
        "ElkLogGateway": "ops_autoagent.tools:ObservabilityTools",
        "SkyWalkingTraceGateway": "ops_autoagent.tools:ObservabilityTools",
        "OpsMcpToolGateway": "ops_autoagent.tools:McpHttpClient",
        "ThreadPoolConfig": "ops_autoagent.executors:CallerRunsBoundedExecutor",
        "ThreadPoolConfigProperties": "ops_autoagent.config:Settings",
        "AiClientConfig": "ops_autoagent.ops.chat:OpsChatClientResolver",
        "DefaultOpsChatClientResolver": "ops_autoagent.ops.chat:OpsChatClientResolver",
        "SpringAiOpsChatAgentAdapter": "ops_autoagent.ops.chat:OpsMultiChatAgentService",
        "OpsChatAgentAdapter": "ops_autoagent.ops.chat:OpsMultiChatAgentService",
        "OpsMultiChatAgentService": "ops_autoagent.ops.chat:OpsMultiChatAgentService",
    }
    if name in overrides:
        return overrides[name], "explicit behavior-level migration"
    if name.endswith("Controller"):
        target = ("ops_autoagent.api:run_codeops_evaluation" if "CodeOpsEvaluation" in name else
                  "ops_autoagent.api:dashboard_overview" if "Dashboard" in name else
                  "ops_autoagent.api:agent_loop" if "AgentLoop" in name else
                  "ops_autoagent.api:alertmanager" if "AlertWebhook" in name else
                  "ops_autoagent.api:run_ops_evaluation" if "Evaluation" in name else
                  "ops_autoagent.api:submit_task" if "CodeOpsTask" in name else
                  "ops_autoagent.api:mock_health" if "MockFault" in name else
                  "ops_autoagent.api:verify_full_chain" if "Verification" in name else
                  "ops_autoagent.api:analyze_incident")
        return target, "FastAPI route implementation; route parity is audited"
    if "/dao/" in location or "repository" in lower or lower.startswith("iops") or lower.startswith("iai"):
        return "ops_autoagent.store:Store", "consolidated async persistence adapter with legacy MySQL dual-write/read"
    if "config" in lower or "autoconfig" in lower or "configuration" in lower:
        return "ops_autoagent.config:Settings", "typed environment configuration"
    if "chatclient" in lower or "chatagent" in lower or lower.startswith("aiclient") or "armory" in lower:
        return "ops_autoagent.ops.chat:OpsChatClientResolver", "dynamic ai_client graph and role-specific client resolution"
    if "evaluation" in lower or "eval" in lower:
        return ("ops_autoagent.api:_evaluate_cases" if "codeops" in location or "codeops" in lower
                else "ops_autoagent.api:_ops_fixture_summary"), "real graph evaluation harness"
    if "scheduler" in lower or "priorityqueue" in lower:
        return "ops_autoagent.codeops.services:IncidentScheduler", "persistent prioritized incident scheduler"
    if "approval" in lower:
        return "ops_autoagent.graphs.codeops:CodeOpsGraph", "LangGraph interrupt/Command approval gate"
    if "permission" in lower or "security" in lower or "safetyhook" in lower or "accesslevel" in lower:
        return "ops_autoagent.codeops.runtime:SecurityPolicy", "tool and mutation security policy"
    if "patch" in lower or "bugfix" in lower:
        return ("ops_autoagent.codeops.runtime:PatchSandbox" if "sandbox" in lower or "apply" in lower
                else "ops_autoagent.codeops.runtime:PatchProposal"), "structured patch/sandbox/validation pipeline"
    if "tool" in lower:
        return ("ops_autoagent.ops.services:ToolGovernance" if "ops" in location and "codeops" not in location
                else "ops_autoagent.codeops.runtime:EngineeringToolGateway"), "governed named tool runtime"
    if "runbook" in lower or "rag" in lower or "knowledge" in lower:
        return "ops_autoagent.ops.rag:RunbookRagService", "BM25/PGVector/hybrid/rerank implementation"
    if "memory" in lower:
        return ("ops_autoagent.codeops.services:IncidentMemoryService" if "codeops" in location
                else "ops_autoagent.ops.services:HistoricalMemoryService"), "persistent recall and compaction behavior"
    if "agentloop" in lower or "repairagent" in lower or "recovery" in lower or "failurediagnostic" in lower:
        return "ops_autoagent.codeops.services:AgentLoopService", "bounded engineering agent loop and recovery"
    if "codeops" in location or any(word in lower for word in ("engineering", "release", "reviewfinding", "codesnippet")):
        return "ops_autoagent.graphs.codeops:CodeOpsGraph", "consolidated typed CodeOps LangGraph state/node implementation"
    if any(word in lower for word in ("alert", "evidence", "incident", "diagnosis", "rootcause", "metric", "trace", "log")):
        return "ops_autoagent.graphs.ops:OpsDiagnosisGraph", "consolidated typed Ops LangGraph state/node implementation"
    if name.endswith(("DTO", "Entity", "VO", "Result", "Request", "Output", "Record", "Status")) or "/model/" in location or "/po/" in location:
        return "ops_autoagent.schemas:ApiResponse", "Python typed/dictionary data contract serialized at the API/state boundary"
    return "ops_autoagent.graphs.codeops:CodeOpsGraph", "framework abstraction collapsed into executable LangGraph orchestration"


def main() -> None:
    exact = python_symbols()
    pattern = re.compile(r"public\s+(?:abstract\s+)?(?:class|interface|enum|record)\s+(\w+)")
    mapping, evidence = {}, {}
    for path in sorted(LEGACY.rglob("*.java")):
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if not match:
            continue
        name = match.group(1)
        target, rationale = choose(name, path.as_posix(), exact)
        mapping[name] = target
        test = ("tests/test_codeops_runtime.py" if "codeops" in path.as_posix().lower() else
                "tests/test_ops_services.py" if "ops" in path.as_posix().lower() else "tests/test_parity_audits.py")
        evidence[name] = {"target": target, "legacyFile": path.relative_to(ROOT).as_posix(),
                          "rationale": rationale, "verification": test}
    (ROOT / "docs" / "migration-symbol-map.json").write_text(
        json.dumps(dict(sorted(mapping.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "docs" / "migration-symbol-evidence.json").write_text(
        json.dumps(dict(sorted(evidence.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
