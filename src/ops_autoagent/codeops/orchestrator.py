from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OrchestratorDecision:
    decision: str
    selected_skill: str = ""
    reason: str = ""

    @property
    def should_stop(self) -> bool:
        return self.decision == "STOP"


class IncidentFixOrchestratorPolicy:
    """Behavioral port of the Java IncidentFixOrchestratorPolicy decision order."""

    OPS = "ops_diagnosis"
    AGENT_LOOP = "agent_loop_investigation"
    REPO = "repo_understanding"
    KNOWLEDGE = "engineering_knowledge_rag"
    BUG_FIX = "bug_fix"
    TEST = "test_verification"
    RELEASE = "release_risk_analysis"
    REVIEW = "pr_review"

    def decide(self, task_type: str, memory: dict[str, Any], executed: list[str], context: dict[str, Any],
               focus_areas: list[str] | None = None) -> OrchestratorDecision:
        normalized = (task_type or "CODE_REVIEW").upper()
        if normalized == "INCIDENT_TO_FIX":
            return self._incident(memory, executed, context, focus_areas or [])
        if normalized == "ISSUE_TO_PATCH":
            return self._issue(memory, executed, context, focus_areas or [])
        if normalized == "RELEASE_RISK":
            return self._release(memory, executed, context)
        return self._review(memory, executed, context)

    def _incident(self, m, done, ctx, focus):
        if ctx.get("incidentFixReflectionExhausted"):
            return self._call(self.RELEASE, "测试验证连续失败已达到 3 轮反思上限，进入失败态发布风险分析，输出当前 patch 可信度、失败日志、人工接管点、上线观察和回滚建议。") if not m.get("releaseRisk") and self.RELEASE not in done else self._stop("测试验证连续失败已达到 3 轮反思上限，已生成失败态发布风险分析，等待人工查看失败日志和当前 patch。")
        if not m.get("opsEvidence") and self.OPS not in done:
            return self._call(self.OPS, "线上告警修复任务需要先形成运维证据，供后续代码定位和修复 Agent 使用。")
        if self._needs_agent_loop(m, done, ctx, incident=True):
            return self._call(self.AGENT_LOOP, "已有运维证据，先通过模型驱动工具循环做一次只读仓库调查，为后续代码定位和修复策略提供上下文。")
        if self._localization_blocking(m) and self.REPO not in done:
            return self._call(self.REPO, "Agent loop 定位质量门阻断自动修复，先由仓库理解 Agent 补充根因文件、方法边界和缺失证据。")
        if ((not m.get("codeLocalization") and self.REPO not in done)
                or self._needs_repo_after_loop(m, done)):
            return self._call(self.REPO, "Agent loop 调查结果仍需补强，下一步由 Incident Triage Agent 判断是否该改代码，并在需要时定位可疑文件和方法。")
        if self._localization_blocking(m):
            return self._call(self.RELEASE, "代码定位质量门仍阻断自动修复，输出缺失证据、人工补证建议、运行时处置和上线观察项。") if not m.get("releaseRisk") and self.RELEASE not in done else self._stop("代码定位质量门阻断自动修复：根因文件/方法或支撑证据不足，任务停止等待人工补证。")
        if not self._should_repair(m, ctx, focus, done):
            return self._call(self.RELEASE, "Incident Triage 判断当前不应自动改代码，生成运行时/配置/容量处置建议、观察指标和人工确认点。") if not m.get("releaseRisk") and self.RELEASE not in done else self._stop("Incident Triage 判断当前事故不应进入自动代码修复，已输出处置建议和风险观察项。")
        if not m.get("engineeringKnowledge") and self.KNOWLEDGE not in done:
            return self._call(self.KNOWLEDGE, "需要补充工程知识和 Runbook 背景，作为修复生成 Agent 的参考证据。")
        if ctx.get("incidentFixReflectionRound", 0) and not m.get("patchGeneration"):
            return self._call(self.BUG_FIX, "测试验证失败后进入反思修复轮，将失败日志回灌给修复生成 Agent，重新生成或调整 patch。")
        if ctx.get("incidentFixReflectionRound", 0) and not m.get("testVerification"):
            return self._call(self.TEST, "反思修复轮已有新的修复上下文，重新生成测试补丁并执行验证。")
        if ctx.get("incidentFixReflectionRound", 0) and not m.get("releaseRisk"):
            return self._call(self.RELEASE, "反思修复轮测试已完成，重新评估发布风险和上线观察项。")
        if not m.get("patchGeneration") and self.BUG_FIX not in done:
            return self._call(self.BUG_FIX, "已有故障证据、代码候选和知识上下文，下一步由修复生成 Agent 产出最小 patch。")
        if self._no_code_fix(m.get("patchGeneration", {})):
            return self._call(self.RELEASE, "修复生成 Agent 判断当前事故不需要代码补丁，跳过测试验证，直接输出运行时处置建议和上线观察项。") if not m.get("releaseRisk") and self.RELEASE not in done else self._stop("当前事故已判定为非代码修复场景，已输出运行时处置建议和风险观察项。")
        if not m.get("testVerification") and self.TEST not in done:
            return self._call(self.TEST, "已有修复产物或已尝试修复生成，下一步由测试验证 Agent 决定并执行相关验证。")
        if not m.get("releaseRisk") and self.RELEASE not in done:
            return self._call(self.RELEASE, "已有测试验证上下文，下一步由发布风险 Agent 评估上线观察项、回滚点和剩余风险。")
        return self._stop("Incident-to-Fix 所需的运维证据、代码定位、知识补充、修复、测试和发布风险阶段均已完成或已尝试。")

    def _issue(self, m, done, ctx, focus):
        if self._needs_agent_loop(m, done, ctx):
            return self._call(self.AGENT_LOOP, "需求到修复任务先通过模型驱动工具循环做只读仓库调查，定位候选代码和测试文件。")
        if self._localization_blocking(m) and self.REPO not in done:
            return self._call(self.REPO, "Agent loop 定位质量门阻断自动修复，继续补充根因文件、方法边界和缺失证据。")
        if ((not m.get("codeLocalization") and self.REPO not in done)
                or self._needs_repo_after_loop(m, done)):
            return self._call(self.REPO, "Agent loop 未形成足够稳定的候选代码位置，继续由仓库理解 Agent 补充定位。")
        if self._localization_blocking(m):
            return self._stop("代码定位质量门阻断自动修复：根因文件/方法或支撑证据不足，任务停止等待人工补证。")
        if not self._should_repair(m, ctx, focus, done):
            return self._stop("Agent loop / 代码定位判断当前不应进入自动代码修复，任务停止等待人工确认或补充证据。")
        for key, skill, reason in (("engineeringKnowledge", self.KNOWLEDGE, "需要补充工程知识，避免修复建议脱离项目约束。"),
                                   ("patchGeneration", self.BUG_FIX, "已有代码上下文和知识上下文，下一步生成修复 patch。"),
                                   ("testVerification", self.TEST, "已有修复产物，下一步生成并执行测试验证计划。"),
                                   ("releaseRisk", self.RELEASE, "已有修复和测试上下文，最后评估发布风险、回归重点和人工确认点。")):
            if not m.get(key) and skill not in done:
                return self._call(skill, reason)
        return self._stop("Issue-to-Patch 的代码定位、知识补充、修复、测试和发布风险阶段均已完成或已尝试。")

    def _release(self, m, done, ctx):
        if self._needs_agent_loop(m, done, ctx):
            return self._call(self.AGENT_LOOP, "发布风险评估先通过模型驱动工具循环做只读仓库调查，补充变更相关代码和测试上下文。")
        if ((not m.get("codeLocalization") and self.REPO not in done)
                or self._needs_repo_after_loop(m, done)):
            return self._call(self.REPO, "Agent loop 调查结果还不足以支撑发布风险判断，继续补充变更涉及的代码区域。")
        for key, skill, reason in (
                                   ("engineeringKnowledge", self.KNOWLEDGE, "需要补充发布规范、Runbook 或工程知识。"),
                                   ("releaseRisk", self.RELEASE, "已有代码和知识上下文，下一步评估发布风险。"),
                                   ("testVerification", self.TEST, "发布风险评估后需要补充验证计划和测试结果。")):
            if not m.get(key) and skill not in done:
                return self._call(skill, reason)
        return self._stop("Release-Risk 的代码理解、知识补充、风险评估和测试验证阶段均已完成或已尝试。")

    def _review(self, m, done, ctx):
        if self._needs_agent_loop(m, done, ctx):
            return self._call(self.AGENT_LOOP, "代码审查任务先通过模型驱动工具循环做只读仓库调查，补充候选文件、测试和风险上下文。")
        if ((not m.get("codeLocalization") and self.REPO not in done)
                or self._needs_repo_after_loop(m, done)):
            return self._call(self.REPO, "Agent loop 调查结果还不足以支撑代码审查，继续补充变更和仓库上下文。")
        for key, skill, reason in (("engineeringKnowledge", self.KNOWLEDGE, "需要补充工程知识，提升审查判断的项目贴合度。"),):
            if not m.get(key) and skill not in done:
                return self._call(skill, reason)
        if self.REVIEW not in done:
            return self._call(self.REVIEW, "已有代码上下文和知识上下文，下一步由代码审查 Agent 分析风险。")
        if not m.get("testVerification") and self.TEST not in done:
            return self._call(self.TEST, "代码审查后需要补充验证计划。")
        if not m.get("releaseRisk") and self.RELEASE not in done:
            return self._call(self.RELEASE, "只读审查与基线验证已完成，交由独立 Reviewer 输出风险结论和人工复核点。")
        return self._stop("Code-Review 的代码理解、知识补充、审查和测试验证阶段均已完成或已尝试。")

    def _needs_agent_loop(self, memory, done, context, incident=False):
        if self.AGENT_LOOP in done or context.get("agentLoopInvestigationEnabled") is False:
            return False
        return bool(memory.get("opsEvidence")) and not memory.get("codeLocalization") if incident else not memory.get("codeLocalization")

    @staticmethod
    def _localization_blocking(memory):
        localization, strategy = memory.get("codeLocalization") or {}, memory.get("fixStrategy") or {}
        reflection = localization.get("localizationReflection", {})
        strategy_reflection = strategy.get("localizationReflection", {})
        return bool(localization.get("localizationBlocking") or strategy.get("localizationBlocking") or
                    strategy.get("strategyType") == "NEED_MORE_EVIDENCE" or reflection.get("blocking") or
                    strategy_reflection.get("blocking") or
                    ((localization.get("localizationSuccess") is False or localization.get("localizationFallback") is True)
                     and localization.get("missingEvidence")))

    @staticmethod
    def _should_repair(memory, context, focus, executed):
        if context.get("evaluationExpectedNoCodePatch") is True:
            return False
        strategy = memory.get("fixStrategy", {})
        strategy_type = strategy.get("strategyType", strategy.get("fixStrategy"))
        if IncidentFixOrchestratorPolicy._localization_blocking(memory):
            return False
        if str(strategy_type or "").upper() == "NEED_MORE_EVIDENCE":
            return False
        explicitly_authorized = (context.get("allowPatchApply") is True or
                                 context.get("allowTestPatchApply") is True or
                                 {str(item).lower() for item in focus} & {"bug_fix", "test_verification"})
        localization = memory.get("codeLocalization") or {}
        if explicitly_authorized:
            # The agent loop may conservatively return false while it still asks for
            # source confirmation.  That is not a NO_CODE_FIX decision for an
            # explicitly code-fix case.  The repository investigation must have
            # run and must have produced concrete candidates before this override.
            if not localization:
                return True
            if IncidentFixOrchestratorPolicy.REPO not in executed:
                return False
            if not localization.get("targetFiles") or localization.get("localizationBlocking") is True:
                return False
            return True
        if strategy.get("shouldEnterCodeRepair") is False:
            return False
        text = (str(memory.get("opsEvidence", {})) + "\n" + str(memory.get("codeHints", {})) + "\n"
                + str(memory.get("codeLocalization", {}))).lower()
        if ((".java" in text or "controller." in text)
                and any(term in text for term in ("exception", "stack", "at com.", "negative", "duplicate"))):
            return True
        if (IncidentFixOrchestratorPolicy.AGENT_LOOP in executed
                and str(localization.get("localizationConfidence", "")).upper() == "LOW"
                and not localization.get("targetFiles")):
            return False
        if not strategy:
            return True
        value = strategy.get("shouldEnterCodeRepair")
        return value if isinstance(value, bool) else str(value).lower() == "true"

    @staticmethod
    def _no_code_fix(patch):
        if patch.get("phase") == "BUG_FIX_SKIPPED_NO_CODE_FIX":
            return True
        scope = patch.get("repairScope")
        return isinstance(scope, dict) and scope.get("scopeType") == "NO_CODE_FIX"

    @staticmethod
    def _needs_repo_after_loop(memory: dict[str, Any], done: list[str]) -> bool:
        if (IncidentFixOrchestratorPolicy.AGENT_LOOP not in done
                or IncidentFixOrchestratorPolicy.REPO in done):
            return False
        localization = memory.get("codeLocalization") or {}
        missing = localization.get("missingEvidence") or []
        strategy = memory.get("fixStrategy") or {}
        missing = [*missing, *(strategy.get("missingEvidence") or [])]
        return bool(localization) and (str(localization.get("localizationConfidence", "")).upper() == "LOW"
                                       or not localization.get("targetFiles") or bool(missing))

    @staticmethod
    def _call(skill, reason):
        return OrchestratorDecision("CALL_SKILL", skill, reason)

    @staticmethod
    def _stop(reason):
        return OrchestratorDecision("STOP", "", reason)
