from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from pathlib import Path

from ..schemas import now_iso
from ..store import Store
from .runtime import EngineeringToolGateway, SecurityPolicy, ToolBudget, ToolRuntime


class CodeOpsTaskDagService:
    STAGE = {"agent_loop": "code_localization", "agent_loop_investigation": "code_localization",
             "repo_understanding": "code_localization", "engineering_knowledge_rag": "knowledge_rag",
             "bug_fix": "code_repair", "test_verification": "test_verification",
             "release_risk": "release_risk", "release_risk_analysis": "release_risk"}

    def mark(self, context: dict[str, Any], step_no: int, skill: str, status: str, summary: str,
             artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
        nodes = [dict(item) for item in context.get("taskDagNodes", [])]
        node_id = f"step-{step_no}-{skill or 'unknown'}"
        normalized = {"SUCCESS": "COMPLETED", "NO_DIFF": "COMPLETED", "SKIPPED": "COMPLETED",
                      "STOPPED": "BLOCKED"}.get(status.upper(), status.upper())
        now = now_iso()
        target = next((item for item in nodes if item.get("nodeId") == node_id), None)
        if target is None:
            target = {"nodeId": node_id, "stepNo": step_no, "skillId": skill,
                      "stage": self.STAGE.get(skill, skill or "unknown"), "owner": "agent_loop",
                      "blockedBy": [nodes[-1]["nodeId"]] if nodes else [], "createTime": now}
            nodes.append(target)
        target.update({"status": normalized, "summary": summary or "", "artifacts": artifacts or {}, "updateTime": now})
        return {**context, "taskDagNodes": nodes}


class CodeOpsHookService:
    EVENTS = {"BEFORE_TOOL_USE", "AFTER_TOOL_USE", "BEFORE_PATCH_APPLY", "AFTER_PATCH_APPLY",
              "AFTER_COMPILE", "AFTER_TEST", "ON_FAILURE_DIAGNOSTIC", "BEFORE_RELEASE_RISK"}

    def emit(self, context: dict[str, Any], event: str, phase: str, summary: str,
             payload: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if event not in self.EVENTS:
            raise ValueError(f"Unknown CodeOps hook event: {event}")
        decisions = []
        if event == "BEFORE_TOOL_USE" and (payload or {}).get("toolName") == "repo.exact_replace":
            arguments = (payload or {}).get("arguments") or {}
            repository = arguments.get("repository", ".")
            file_path = str(arguments.get("filePath", ""))
            allowed = SecurityPolicy.is_write_allowed(repository, file_path, source_only=True)
            decisions.append({"handler": "source_write_safety", "allowed": allowed,
                              "requiresApproval": False,
                              "reason": "repo.exact_replace is disabled outside apply_approved_patch"
                              if not allowed else "legacy hook validation passed; mutation remains effect-node-only"})
        requires_approval = any(item.get("requiresApproval") for item in decisions)
        allowed = not requires_approval and all(item.get("allowed", True) for item in decisions)
        reason = next((item.get("reason", "") for item in decisions if not item.get("allowed", True)), "")
        result = {"allowed": allowed, "requiresApproval": requires_approval, "reason": reason, "decisions": decisions}
        observations = list(context.get("repairObservations", []))
        observations.append({"phase": phase or "HOOK", "source": "hook", "action": event,
                             "status": "OBSERVED" if allowed else ("REQUIRES_APPROVAL" if requires_approval else "BLOCKED"),
                             "success": allowed, "summary": summary or "", "errorType": "" if allowed else "HOOK_BLOCKED",
                             "errorMessage": "" if allowed else reason,
                             "output": {**(payload or {}), "hookEvent": event, "hookResult": result}, "time": now_iso()})
        return {**context, "repairObservations": observations}, result


class AgentLoopService:
    READ_ONLY_TOOLS = {
        "repo.create_snapshot", "repo.search_text", "repo.list_files", "repo.read_file_snippet",
        "repo.git_diff", "repo.git_log", "repo.find_tests", "knowledge.search",
        "task.background_status",
    }

    def __init__(self, gateway: EngineeringToolGateway):
        self.gateway = gateway

    @staticmethod
    def _normalize_tool_arguments(tool_name: str, arguments: dict[str, Any], repository: Any) -> dict[str, Any]:
        """Normalize model paths without widening the repository security boundary."""
        normalized = dict(arguments or {})
        if tool_name.startswith("repo.") and not str(normalized.get("repository") or "").strip():
            normalized["repository"] = repository
        if tool_name != "repo.read_file_snippet":
            return normalized
        file_path = str(normalized.get("filePath") or "").strip()
        if not file_path:
            return normalized
        root = Path(str(normalized.get("repository") or ".")).resolve()
        candidate = Path(file_path)
        if not candidate.is_absolute():
            return normalized
        try:
            normalized["filePath"] = candidate.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise PermissionError("Absolute read path is outside the target repository") from exc
        return normalized

    async def run(self, request: dict[str, Any], model_client: Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        requested_turns = int(request.get("maxTurns") or 0)
        max_turns = 8 if requested_turns <= 0 else min(requested_turns, 32)
        max_tools = max(1, int(request.get("maxToolCalls") or 20))
        budget = ToolBudget(max_tools)
        steps: list[dict[str, Any]] = []
        task = request.get("task") or {"taskId": f"agent-loop-{uuid.uuid4()}", "context": {}}
        token = self.gateway.runtime.bind(task, trace_id=str(request.get("traceId", "")), agent_or_skill="agent_loop")
        try:
            for turn in range(1, max_turns + 1):
                consumed_notifications = self.gateway.consume_terminal_notifications(task, "agent-loop-service")
                if consumed_notifications:
                    metadata = request.setdefault("metadata", {})
                    observations = [self._notification_observation(item) for item in consumed_notifications]
                    metadata["backgroundTaskObservations"] = observations
                    metadata["latestBackgroundTaskObservation"] = observations[-1]
                decision = await model_client(request, list(steps))
                if not decision:
                    return self._result("FAILED", "", "model returned null decision", turn, steps)
                self._append_trace(task, turn, decision, steps, consumed_notifications)
                if decision.get("final"):
                    return self._result("COMPLETED", str(decision.get("finalAnswer", "")), "final_answer", turn, steps)
                calls = decision.get("toolCalls") or []
                if not calls:
                    return self._result("COMPLETED", "", "no_tool_calls", turn, steps)
                for call in calls:
                    started = now_iso()
                    name = str(call.get("toolName", ""))
                    try:
                        arguments = self._normalize_tool_arguments(name, dict(call.get("arguments") or {}),
                                                                  request.get("repository"))
                    except PermissionError as exc:
                        arguments = dict(call.get("arguments") or {})
                        tool_result = {"status": "DENIED", "success": False, "summary": str(exc), "output": None}
                        steps.append({"turnNo": turn, "toolCallId": call.get("toolCallId", str(uuid.uuid4())),
                                      "toolName": name, "arguments": arguments,
                                      "permissionDecision": {"status": "DENIED"}, "toolResult": tool_result,
                                      "startedAt": started, "finishedAt": now_iso()})
                        return self._result(tool_result["status"], "", tool_result["summary"], turn, steps)
                    try:
                        if name not in self.READ_ONLY_TOOLS or not self.gateway.is_registered_tool(name):
                            raise PermissionError(f"Unknown tool: {name}")
                        output = await self.gateway.invoke(name, arguments, budget=budget)
                        tool_result = {"status": "SUCCESS", "success": True, "summary": self.gateway._summary(output), "output": output}
                    except PermissionError as exc:
                        tool_result = {"status": "DENIED", "success": False, "summary": str(exc), "output": None}
                    except Exception as exc:
                        tool_result = {"status": "FAILED", "success": False, "summary": str(exc), "output": None}
                    steps.append({"turnNo": turn, "toolCallId": call.get("toolCallId", str(uuid.uuid4())),
                                  "toolName": name, "arguments": arguments,
                                  "permissionDecision": {"status": "ALLOWED" if tool_result["status"] != "DENIED" else "DENIED"},
                                  "toolResult": tool_result, "startedAt": started, "finishedAt": now_iso()})
                    if tool_result["status"] in {"DENIED", "REQUIRES_APPROVAL"}:
                        # A malformed read-only call is recoverable model feedback. Let the next
                        # turn correct its path/arguments; mutation and approval denials remain terminal.
                        if tool_result["status"] == "DENIED" and name in self.READ_ONLY_TOOLS:
                            continue
                        return self._result(tool_result["status"], "", tool_result["summary"], turn, steps)
            return self._result("MAX_TURNS_REACHED", "", f"agent loop reached maxTurns={max_turns}", max_turns, steps)
        finally:
            self.gateway.runtime.clear(token)

    @staticmethod
    def _notification_observation(notification: dict[str, Any]) -> dict[str, Any]:
        return {"notificationId": notification.get("notificationId", ""), "backgroundTaskId": notification.get("backgroundTaskId", ""),
                "type": notification.get("type", ""), "status": notification.get("status", ""),
                "summary": notification.get("summary", ""), "payload": notification.get("payload", {})}

    @staticmethod
    def _append_trace(task: dict[str, Any], turn: int, decision: dict[str, Any], steps: list[dict[str, Any]],
                      consumed_notifications: list[dict[str, Any]] | None = None) -> None:
        trace = task.setdefault("context", {}).setdefault("agentLoopTrace", [])
        trace.append({"turn": turn, "thoughtSummary": decision.get("thoughtSummary", ""),
                      "final": bool(decision.get("final")), "stepCount": len(steps),
                      "recentSteps": steps[-5:], "consumedBackgroundNotifications": consumed_notifications or [], "time": now_iso()})

    @staticmethod
    def _result(status: str, answer: str, reason: str, turns: int, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {"status": status, "finalAnswer": answer, "stopReason": reason, "turns": turns,
                "trace": [{"turnNo": item["turnNo"], "toolName": item["toolName"],
                           "permission": item["permissionDecision"]["status"],
                           "toolStatus": item["toolResult"]["status"], "summary": item["toolResult"]["summary"],
                           "outputPreview": str(item["toolResult"].get("output", ""))[:600]} for item in steps],
                "steps": steps}


class RepairAgentLoopService(AgentLoopService):
    ALLOWED_TOOLS = {"repo.read_file_snippet", "repo.exact_replace", "repo.maven",
                     "repo.maven_background", "task.background_status"}

    async def run(self, request: dict[str, Any], model_client) -> dict[str, Any]:
        async def guarded(req: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
            decision = await model_client(req, steps)
            denied = [call.get("toolName") for call in decision.get("toolCalls", []) if call.get("toolName") not in self.ALLOWED_TOOLS]
            if denied:
                return {"toolCalls": [{"toolName": denied[0], "arguments": {}}],
                        "thoughtSummary": "repair loop denied a non-repair tool"}
            return decision
        result = await super().run({**request, "maxTurns": min(int(request.get("maxTurns") or 8), 16)}, guarded)
        steps = result["steps"]
        edit = next((item for item in reversed(steps) if item["toolName"] == "repo.exact_replace"), None)
        compile_step = next((item for item in reversed(steps) if item["toolName"] == "repo.maven"), None)
        recovered = bool(edit and edit["toolResult"]["success"] and (not compile_step or compile_step["toolResult"]["success"]))
        result["patchAttempt"] = {"skillId": "repair_agent_loop", "editMethod": "exactReplace" if edit else "none",
                                  "filesRead": list(dict.fromkeys(item["arguments"].get("filePath") for item in steps
                                                                  if item["toolName"] == "repo.read_file_snippet" and item["arguments"].get("filePath"))),
                                  "scopeDecision": {"source": "repair_agent_loop", "allowedTools": sorted(self.ALLOWED_TOOLS)},
                                  "applyResult": edit["toolResult"] if edit else {},
                                  "compileResult": compile_step["toolResult"] if compile_step else {}, "recovered": recovered}
        if result["status"] == "COMPLETED" and not recovered:
            result["status"] = "FAILED"
        return result


class CodeOpsSecurityGovernance:
    def __init__(self, runtime: ToolRuntime, policy: SecurityPolicy | None = None):
        self.runtime, self.policy = runtime, policy or SecurityPolicy()

    def global_summary(self) -> dict[str, Any]:
        recent = self.runtime.list_recent(100)
        return {"permissionPolicy": self.policy.governance_summary(), "toolAudit": self._audit(recent),
                "enterpriseControls": ["API token and rate-limit guard for ops endpoints", "tool gateway call audit",
                    "command allowlist and deny patterns", "repository-scoped write policy", "patch scope guard before apply",
                    "sandbox workspace and rollback", "compile/test gates", "high-risk human approval",
                    "secret redaction in tool traces"],
                "recentDeniedTools": [self._compact(item) for item in recent if item.get("status") == "DENIED"][:10]}

    def task_summary(self, task: dict[str, Any], approval: dict[str, Any] | None = None) -> dict[str, Any]:
        context = task.get("context", {})
        traces = context.get("toolRuntimeTrace", [])
        sandbox = context.get("sandboxResult", {})
        analysis = sandbox.get("diffAnalysis", {})
        guardrails = {"patchScopeGuardPassed": sandbox.get("scopeGuard", {}).get("passed"),
                      "patchValidationPassed": sandbox.get("validation", {}).get("valid"),
                      "patchStaticSafetyPassed": analysis.get("staticSafetyPassed"),
                      "patchSandboxIsolated": bool(sandbox.get("sandbox")),
                      "minimalChangeScore": analysis.get("minimalChangeScore")}
        approval_data = approval or task.get("approval", {})
        audit = self._audit(traces)
        pending = approval_data.get("status") == "PENDING" or task.get("status") == "WAITING_APPROVAL"
        level = "HIGH" if pending or audit["deniedCount"] else (
            "MEDIUM" if sandbox and (not analysis.get("staticSafetyPassed", False) or not sandbox.get("sandbox")) else "LOW")
        return {"taskId": task.get("taskId", ""), "status": task.get("status", ""),
                "permissionPolicy": {**self.policy.governance_summary(), "repository": task.get("repository", ""),
                                     "taskType": task.get("taskType", ""), "severity": context.get("severity", "")},
                "guardrails": guardrails, "approval": approval_data, "toolAudit": {**audit, "recent": traces[-20:]},
                "riskPosture": {"level": level, "approvalPending": pending,
                                "staticSafetyPassed": guardrails["patchStaticSafetyPassed"],
                                "sandboxIsolated": guardrails["patchSandboxIsolated"], "deniedToolCalls": audit["deniedCount"]}}

    @staticmethod
    def _audit(records: list[dict[str, Any]]) -> dict[str, Any]:
        by_tool: dict[str, int] = {}
        for item in records:
            by_tool[item.get("toolName", "")] = by_tool.get(item.get("toolName", ""), 0) + 1
        return {"recentCount": len(records), "total": len(records),
                "deniedCount": sum(item.get("status") == "DENIED" for item in records),
                "failedCount": sum(item.get("status") == "FAILED" for item in records), "byTool": by_tool}

    @staticmethod
    def _compact(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in ("toolCallId", "taskId", "toolName", "status", "errorType", "costMillis")}


@dataclass
class ModelDecision:
    model: str
    tier: str
    max_tokens: int
    reason: str


class ModelRouter:
    def __init__(self, flash_model: str, pro_model: str | None = None, escalation_enabled: bool = True):
        self.flash_model = flash_model
        self.pro_model = pro_model or flash_model
        self.escalation_enabled = escalation_enabled
        self.calls = {"FLASH": 0, "PRO": 0}
        self.escalations = 0

    def route(self, skill: str, context: dict[str, Any], round_no: int) -> ModelDecision:
        scope = str((context.get("repairScope") or {}).get("scopeType", ""))
        goal = str(context.get("goal") or context.get("problem") or "").lower()
        previous_failures = int(context.get("previousFlashFailures", 0))
        complex_terms = ("concurency", "concurrency", "并发", "竞态", "race", "idempotent", "幂等",
                         "transaction", "事务", "deadlock", "死锁")
        reason = "STRICT_SINGLE_METHOD or simple incident — flash sufficient"
        use_pro = False
        if self.escalation_enabled:
            if previous_failures >= 2:
                use_pro, reason = True, f"flash failed {previous_failures} rounds, escalating to pro"
                self.escalations += 1
            elif round_no > 1:
                use_pro, reason = True, f"reflection round {round_no - 1}, using pro for error recovery"
            elif scope == "FULL_FILE":
                use_pro, reason = True, "FULL_FILE scope — pro needed for broad incident analysis"
            elif scope == "MULTI_METHOD":
                use_pro, reason = True, "MULTI_METHOD scope — pro for coordinated multi-method fix"
            elif any(term in goal for term in complex_terms):
                use_pro, reason = True, "complex incident keywords detected — routing to pro"
            elif scope == "NO_CODE_FIX":
                reason = "NO_CODE_FIX — flash sufficient for runtime/config diagnosis"
        else:
            reason = "pro escalation disabled; using flash for all repair routing"
        tier = "PRO" if use_pro else "FLASH"
        self.calls[tier] += 1
        return ModelDecision(self.pro_model if use_pro else self.flash_model, tier, 16384 if use_pro else 8192,
                             reason)

    def stats(self) -> dict[str, Any]:
        total = self.calls["FLASH"] + self.calls["PRO"]
        return {"flashModel": self.flash_model, "proModel": self.pro_model,
                "proEscalationEnabled": self.escalation_enabled, "calls": dict(self.calls),
                "flashCalls": self.calls["FLASH"], "proCalls": self.calls["PRO"],
                "escalations": self.escalations, "totalCalls": total,
                "flashRatio": f"{self.calls['FLASH'] * 100 // total}%" if total else "N/A",
                "proRatio": f"{self.calls['PRO'] * 100 // total}%" if total else "N/A"}


class LlmCostControl:
    def __init__(self, flash_input: float = 0.001, flash_output: float = 0.002,
                 pro_input: float = 0.01, pro_output: float = 0.02, soft_limit: float = 1.0):
        self.rates = {"FLASH": (flash_input, flash_output), "PRO": (pro_input, pro_output)}
        self.soft_limit = soft_limit
        self.total_calls = 0
        self.total_estimated_tokens = 0
        self.total_estimated_cost = 0.0
        self.last_call: dict[str, Any] = {}

    def estimate(self, tier: str, model: str, prompt: str, response: str,
                 agent_or_skill: str = "") -> dict[str, Any]:
        input_tokens = 0 if not prompt or not prompt.strip() else max(1, (len(prompt) + 3) // 4)
        output_tokens = 0 if not response or not response.strip() else max(1, (len(response) + 3) // 4)
        input_rate, output_rate = self.rates.get(tier, self.rates["FLASH"])
        input_cost = round(input_tokens / 1000 * input_rate, 6)
        output_cost = round(output_tokens / 1000 * output_rate, 6)
        cost = round(input_cost + output_cost, 6)
        usage = {"agentOrSkill": agent_or_skill, "model": model, "modelTier": tier.lower(),
                "estimatedInputTokens": input_tokens,
                "estimatedOutputTokens": output_tokens, "estimatedTotalTokens": input_tokens + output_tokens,
                "estimatedInputCostCny": input_cost, "estimatedOutputCostCny": output_cost,
                "estimatedTotalCostCny": cost, "singleCallSoftLimitCny": self.soft_limit,
                "overSoftLimit": cost > self.soft_limit, "recordedAt": now_iso()}
        self.total_calls += 1
        self.total_estimated_tokens += input_tokens + output_tokens
        self.total_estimated_cost += cost
        self.last_call = usage
        return usage

    def global_summary(self) -> dict[str, Any]:
        return {"totalLlmCalls": self.total_calls, "totalEstimatedTokens": self.total_estimated_tokens,
                "totalEstimatedCostCny": round(self.total_estimated_cost, 4),
                "singleCallSoftLimitCny": self.soft_limit, "lastCall": self.last_call,
                "pricingNote": "Estimated by character/4 token approximation; provider billing may differ."}


class ContextCompactor:
    def __init__(self, max_tool_output: int = 8000, max_reflection: int = 3000):
        self.max_tool_output, self.max_reflection = max_tool_output, max_reflection

    def compact(self, context: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in context.items():
            if key in {"repositoryFiles"} and isinstance(value, list):
                result[key] = value[:500]
            elif key in {"verificationOutput", "lastRecommendation"} and isinstance(value, str):
                limit = self.max_reflection if key == "lastRecommendation" else self.max_tool_output
                result[key] = value if len(value) <= limit else value[:limit] + "...truncated..."
            else:
                serialized = json.dumps(value, ensure_ascii=False, default=str)
                result[key] = value if len(serialized) <= self.max_tool_output else self._shrink(value, self.max_tool_output)
        result["contextCompacted"] = True
        return result

    def _shrink(self, value: Any, limit: int) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + "...truncated..."
        if isinstance(value, list):
            return [self._shrink(item, max(200, limit // max(1, min(len(value), 20)))) for item in value[:20]]
        if isinstance(value, dict):
            per_item = max(200, limit // max(1, min(len(value), 20)))
            return {str(key): self._shrink(item, per_item) for key, item in list(value.items())[:20]}
        return value


class IncidentMemoryService:
    def __init__(self, store: Store):
        self.store = store

    async def recall(self, task_type: str, goal: str, limit: int = 5) -> list[dict[str, Any]]:
        query = set(re.findall(r"[A-Za-z0-9_.-]{3,}", goal.lower()))
        items = await self.store.find("memories", lambda m: m.get("memoryType") == "CODEOPS" and m.get("taskType") == task_type)
        for item in items:
            terms = set(re.findall(r"[A-Za-z0-9_.-]{3,}", json.dumps(item, ensure_ascii=False).lower()))
            item["similarityScore"] = len(query & terms)
        return sorted(items, key=lambda item: (-item["similarityScore"], item.get("updateTime", "")))[:limit]

    async def remember(self, task: dict[str, Any]) -> dict[str, Any] | None:
        if task.get("status") not in {"COMPLETED", "REJECTED"}:
            return None
        memory = {"memoryId": f"codeops-memory-{uuid.uuid4()}", "memoryType": "CODEOPS",
                  "taskId": task.get("taskId"), "taskType": task.get("taskType"), "goal": task.get("goal"),
                  "repository": task.get("repository"), "finalSummary": task.get("finalSummary"),
                  "steps": task.get("steps", [])[-6:], "createTime": now_iso(), "updateTime": now_iso()}
        await self.store.put("memories", memory["memoryId"], memory, memory["updateTime"])
        return memory


class FailureDiagnosticParser:
    PATTERNS = [
        ("JAVA_COMPILE", re.compile(r"\[ERROR].*?([^\s:]+\.java):\[(\d+),")),
        ("PYTHON_TEST", re.compile(r"FAILED\s+([^\s:]+)::([^\s]+)")),
        ("ASSERTION", re.compile(r"(?:AssertionError|expected:).*", re.IGNORECASE)),
        ("TIMEOUT", re.compile(r"timeout|timed out", re.IGNORECASE)),
    ]

    def parse(self, output: str) -> dict[str, Any]:
        for kind, pattern in self.PATTERNS:
            match = pattern.search(output or "")
            if match:
                return {"failureType": kind, "summary": match.group(0)[:1000], "groups": list(match.groups()),
                        "recoverable": kind != "TIMEOUT"}
        return {"failureType": "UNKNOWN", "summary": (output or "")[-1000:], "groups": [], "recoverable": True}


class ErrorRecoveryPolicy:
    def decide(self, diagnostic: dict[str, Any], round_no: int, max_rounds: int, tool_calls: int, max_tools: int) -> dict[str, Any]:
        if round_no >= max_rounds:
            return {"action": "STOP", "reason": "round budget exhausted"}
        if tool_calls >= max_tools:
            return {"action": "STOP", "reason": "tool budget exhausted"}
        if diagnostic.get("recoverable"):
            return {"action": "RETRY", "reason": f"recoverable {diagnostic.get('failureType')} failure"}
        return {"action": "ESCALATE", "reason": "non-recoverable failure requires human review"}


class BackgroundTaskService:
    def __init__(self):
        self.tasks: dict[str, dict[str, Any]] = {}

    def submit(self, name: str, operation: Awaitable[Any]) -> dict[str, Any]:
        task_id = f"background-{uuid.uuid4()}"
        record = {"taskId": task_id, "name": name, "status": "RUNNING", "result": None,
                  "error": "", "createTime": now_iso(), "updateTime": now_iso()}
        self.tasks[task_id] = record
        task = asyncio.create_task(self._run(task_id, operation))
        record["asyncioTask"] = task
        return {key: value for key, value in record.items() if key != "asyncioTask"}

    async def _run(self, task_id: str, operation: Awaitable[Any]) -> None:
        record = self.tasks[task_id]
        try:
            record.update(status="COMPLETED", result=await operation, updateTime=now_iso())
        except Exception as exc:
            record.update(status="FAILED", error=str(exc), updateTime=now_iso())

    def status(self, task_id: str) -> dict[str, Any] | None:
        record = self.tasks.get(task_id)
        return {key: value for key, value in record.items() if key != "asyncioTask"} if record else None


class IncidentScheduler:
    PRIORITY = {"CRITICAL": 0, "HIGH": 30, "WARNING": 60, "MEDIUM": 60}

    SEVERITY_SCORE = {"CRITICAL": 100, "P1": 100, "HIGH": 70, "P2": 70, "WARNING": 40,
                      "MEDIUM": 40, "P3": 40, "LOW": 10}

    def __init__(self, handler: Callable[[dict[str, Any]], Awaitable[Any]], max_concurrent: int = 4,
                 max_per_service: int = 1, queue_file: str | Path = "data/incident-queue/queue.json",
                 dedup_window_seconds: int = 300):
        self.handler = handler
        self.max_concurrent, self.max_per_service = max_concurrent, max_per_service
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.running = False
        self.workers: list[asyncio.Task] = []
        self.active_by_service: dict[str, int] = {}
        self.sequence = 0
        self.queue_file = Path(queue_file)
        self.dedup_window_seconds = dedup_window_seconds
        self.seen_fingerprints: dict[str, float] = {}
        self.active_incidents: dict[str, dict[str, Any]] = {}
        self.total_enqueued = 0
        self.total_dispatched = 0

    async def ingest(self, fingerprint: str, alert_name: str, service: str, severity: str,
                     summary: str, endpoint: str = "", **metadata: Any) -> dict[str, Any] | None:
        now = time.time()
        last_seen = self.seen_fingerprints.get(fingerprint)
        if last_seen is not None and now - last_seen < self.dedup_window_seconds:
            return None
        self.seen_fingerprints[fingerprint] = now
        group_key = f"{service}|{alert_name}"
        incident = self.active_incidents.setdefault(group_key, {
            "groupKey": group_key, "serviceName": service, "service": service, "alertName": alert_name,
            "severity": "LOW", "highestSeverity": "LOW", "alertCount": 0, "summary": "",
            "latestSummary": "", "affectedEndpoints": [], "firstSeen": int(now * 1000),
        })
        incident["alertCount"] += 1
        if self.SEVERITY_SCORE.get(severity.upper(), 10) > self.SEVERITY_SCORE.get(incident["severity"].upper(), 10):
            incident["severity"] = incident["highestSeverity"] = severity
        if endpoint and endpoint not in incident["affectedEndpoints"]:
            incident["affectedEndpoints"].append(endpoint)
        incident.update(summary=summary, latestSummary=summary, lastUpdate=int(now * 1000), fingerprint=fingerprint,
                        **metadata)
        if incident["alertCount"] == 1:
            await self.enqueue(dict(incident))
            return dict(incident)
        return None

    async def enqueue(self, incident: dict[str, Any]) -> None:
        self.sequence += 1
        incident = {**incident, "status": "QUEUED", "enqueuedAt": int(time.time() * 1000)}
        priority = self.PRIORITY.get(str(incident.get("severity", "LOW")).upper(), 90)
        await self.queue.put((priority, self.sequence, incident))
        self.total_enqueued += 1
        self._persist()

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.workers = [asyncio.create_task(self._worker()) for _ in range(self.max_concurrent)]

    async def stop(self) -> None:
        self.running = False
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def _worker(self) -> None:
        while self.running:
            priority, sequence, incident = await self.queue.get()
            service = incident.get("serviceName", "unknown-service")
            if self.active_by_service.get(service, 0) >= self.max_per_service:
                await self.queue.put((priority + 1, sequence, incident))
                self.queue.task_done()
                await asyncio.sleep(0.05)
                continue
            self.active_by_service[service] = self.active_by_service.get(service, 0) + 1
            try:
                await self.handler(incident)
            finally:
                self.active_by_service[service] -= 1
                self.total_dispatched += 1
                group_key = incident.get("groupKey")
                if group_key:
                    self.active_incidents.pop(group_key, None)
                self.queue.task_done()
                self._persist()

    def status(self) -> dict[str, Any]:
        raw_queued = [dict(item[2]) for item in sorted(list(self.queue._queue))[:50]]
        queued = [{"groupKey": item.get("groupKey"), "service": item.get("service"),
                   "severity": item.get("severity"), "alertCount": item.get("alertCount"),
                   "status": item.get("status", "QUEUED"), "enqueuedAt": item.get("enqueuedAt")}
                  for item in raw_queued]
        active = [{"groupKey": item.get("groupKey"), "service": item.get("service"),
                   "alertName": item.get("alertName"), "severity": item.get("highestSeverity"),
                   "alertCount": item.get("alertCount"), "summary": item.get("latestSummary"),
                   "endpoints": item.get("affectedEndpoints", []), "firstSeen": item.get("firstSeen"),
                   "lastUpdate": item.get("lastUpdate"), "status": "AGGREGATING"}
                  for item in self.active_incidents.values()]
        return {"running": self.running, "queuedIncidents": queued,
                "runningSlots": sum(self.active_by_service.values()), "activeByService": dict(self.active_by_service),
                "maxConcurrent": self.max_concurrent, "maxPerService": self.max_per_service,
                "availableSlots": max(0, self.max_concurrent - sum(self.active_by_service.values())),
                "activeIncidents": len(self.active_incidents), "activeIncidentItems": active,
                "queueStats": {"queueSize": self.queue.qsize(), "totalEnqueued": self.total_enqueued,
                               "totalDispatched": self.total_dispatched,
                               "top3": [{key: item.get(key) for key in
                                         ("groupKey", "service", "severity", "alertCount", "status")}
                                        for item in queued[:3]]}}

    def cleanup(self) -> None:
        cutoff = time.time() - self.dedup_window_seconds * 2
        self.seen_fingerprints = {key: value for key, value in self.seen_fingerprints.items() if value >= cutoff}

    def _persist(self) -> None:
        try:
            self.queue_file.parent.mkdir(parents=True, exist_ok=True)
            state = {"totalEnqueued": self.total_enqueued, "totalDispatched": self.total_dispatched,
                     "queue": [dict(item[2]) for item in list(self.queue._queue)]}
            temporary = self.queue_file.with_suffix(self.queue_file.suffix + ".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.queue_file)
        except OSError:
            pass
