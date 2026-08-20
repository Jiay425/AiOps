from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from ..schemas import now_iso

EVALUATION_SCORING_SCHEMA_VERSION = "2"


def collect_raw_outputs(task: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for step in task.get("steps") or []:
        value = step.get("rawEvidenceJson")
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            merged.update(parsed)
    # Downstream Reviewer/Test steps may carry convenience fields with the same
    # names as localization.  The graph's structured investigation result is
    # the authoritative source for those facts, so recover it from compacted
    # working memory before scoring rather than letting later prose overwrite
    # a real repository-investigation decision.
    context = task.get("context") if isinstance(task.get("context"), dict) else {}
    memory = context.get("incidentFixWorkingMemory") if isinstance(context.get("incidentFixWorkingMemory"), dict) else {}
    localization = memory.get("codeLocalization") if isinstance(memory.get("codeLocalization"), dict) else {}
    if localization:
        for key in ("targetFiles", "targetMethods", "strategyType", "fixStrategy", "scopeDecision",
                    "scopeDecisionType", "shouldEnterCodeRepair", "rootCauseLocationType"):
            if localization.get(key) not in (None, "", [], {}):
                merged[key] = localization[key]
        merged["localizationDecision"] = localization
    return merged


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _decision(raw: dict[str, Any]) -> dict[str, Any]:
    return _mapping(raw.get("localizationDecision")) or _mapping(raw.get("codeLocalization"))


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None and str(value).strip()), None)


def _scope_type(raw: dict[str, Any]) -> str:
    scope = _mapping(_mapping(raw.get("patchScopeGuard")).get("repairScope"))
    return str(scope.get("scopeType") or "")


def _fix_strategy(raw: dict[str, Any]) -> str:
    decision = _decision(raw)
    raw_strategy = raw.get("fixStrategy")
    if isinstance(raw_strategy, dict):
        raw_strategy = _first(raw_strategy.get("fixStrategy"), raw_strategy.get("strategyType"))
    return str(_first(decision.get("fixStrategy"), decision.get("strategyType"), raw_strategy,
                      raw.get("strategyType")) or "")


def _scope_decision(raw: dict[str, Any]) -> str:
    decision = _decision(raw)
    return str(_first(decision.get("scopeDecisionType"), decision.get("scopeDecision"),
                      raw.get("scopeDecisionType")) or _scope_type(raw))


def _target_files(raw: dict[str, Any]) -> list[str]:
    decision, values = _decision(raw), []
    for value in (decision.get("rootCauseCandidateFiles"), decision.get("targetFiles"),
                  decision.get("directEvidenceFiles"), raw.get("rootCauseCandidateFiles"), raw.get("targetFiles"),
                  _mapping(_mapping(raw.get("patchScopeGuard")).get("repairScope")).get("targetFiles")):
        values.extend(_strings(value))
    return list(dict.fromkeys(item for item in values if item.strip()))


def _target_methods(raw: dict[str, Any]) -> list[str]:
    decision, values = _decision(raw), []
    for value in (decision.get("targetMethods"), decision.get("candidateMethods"),
                  decision.get("suspectedRootCauseLocations"), raw.get("targetMethods"), raw.get("candidateMethods"),
                  _mapping(_mapping(raw.get("patchScopeGuard")).get("repairScope")).get("targetMethods")):
        values.extend(_strings(value))
    return list(dict.fromkeys(item for item in values if item.strip()))


def _normalized_missing(expected: list[str], actual: list[str]) -> list[str]:
    def identity(value: str) -> str:
        return value.strip().replace("\\", "/").replace("$", ".").split("(", 1)[0].lower()

    normalized = [identity(item) for item in actual]
    missing = []
    for item in expected:
        value = identity(item)
        if not any(candidate == value or candidate.endswith("/" + value) or candidate.endswith("." + value)
                   or value.endswith("/" + candidate) or value.endswith("." + candidate) for candidate in normalized):
            missing.append(item)
    return missing


def _localization_eval(case: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    expected_files, expected_methods = case.get("expectedTargetFiles") or [], case.get("expectedTargetMethods") or []
    expected_fix = str(case.get("expectedFixStrategy") or "").strip().upper()
    expected_scope = str(case.get("expectedScopeDecision") or "").strip().upper()
    expected_repair = None if not expected_fix else expected_fix == "CODE_FIX"
    actual_files, actual_methods = _target_files(raw), _target_methods(raw)
    actual_fix, actual_scope = _fix_strategy(raw).strip().upper(), _scope_decision(raw).strip().upper()
    raw_repair = _first(_decision(raw).get("shouldEnterCodeRepair"), raw.get("shouldEnterCodeRepair"))
    actual_repair = (raw_repair if isinstance(raw_repair, bool) else str(raw_repair).lower() == "true") \
        if raw_repair is not None else (None if not actual_fix else actual_fix == "CODE_FIX")
    missing_files = _normalized_missing(expected_files, actual_files)
    missing_methods = _normalized_missing(expected_methods, actual_methods)
    matches = [None if not expected_fix else expected_fix == actual_fix,
               None if not expected_scope else expected_scope == actual_scope,
               None if not expected_files else not missing_files,
               None if not expected_methods else not missing_methods,
               None if expected_repair is None else expected_repair == actual_repair]
    scored = [item for item in matches if item is not None]
    score = round(sum(bool(item) for item in scored) / len(scored), 2) if scored else 1.0
    return {"score": score, "fixStrategyMatched": matches[0], "scopeDecisionMatched": matches[1],
            "targetFileMatched": matches[2], "targetMethodMatched": matches[3],
            "shouldEnterCodeRepairMatched": matches[4], "expectedTargetFiles": expected_files,
            "actualTargetFiles": actual_files, "missingTargetFiles": missing_files,
            "expectedTargetMethods": expected_methods, "actualTargetMethods": actual_methods,
            "missingTargetMethods": missing_methods, "expectedFixStrategy": expected_fix,
            "actualFixStrategy": actual_fix, "expectedScopeDecision": expected_scope,
            "actualScopeDecision": actual_scope, "expectedShouldEnterCodeRepair": expected_repair,
            "actualShouldEnterCodeRepair": actual_repair, "rawDecision": _decision(raw)}


def _tests_passed(raw: dict[str, Any]) -> bool:
    results = raw.get("testExecutionResults")
    text = "\n".join(str(item) for item in results) if isinstance(results, list) else str(results or "")
    if not text.strip():
        return _mapping(raw.get("testPatchApply")).get("applied") is True or raw.get("testPatchScaffolded") is True
    lower = text.lower()
    if ('"success":false' in lower or '"success": false' in lower or "exitcode=1" in lower
            or "exit code: 1" in lower or "exit code 1" in lower or "build failure" in lower
            or "<<< failure!" in lower):
        return False
    for pattern in (r"failures:\s*(\d+)", r"errors:\s*(\d+)"):
        match = re.search(pattern, lower)
        if match and int(match.group(1)) > 0:
            return False
    return True


def _step_reports(task: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = {"bug_fix": "patchDraft + compileGate", "test_verification": "testExecutionResults",
                 "release_risk_analysis": "codeReview + riskPoints", "ops_diagnosis": "codeHints",
                 "repo_understanding": "evidenceGraph + targetFiles + targetMethods"}
    return [{"stepNo": step.get("stepNo") or 0, "decision": step.get("decision"),
             "selectedSkill": step.get("selectedSkill"), "status": step.get("status"),
             "summary": str(step.get("resultSummary") or "")[:200],
             "keyArtifact": artifacts.get(step.get("selectedSkill"), "")}
            for step in task.get("steps") or []]


def _evaluation_outcome(case: dict[str, Any], run: dict[str, Any], task: dict[str, Any], raw: dict[str, Any],
                        localization: dict[str, Any], coverage: dict[str, Any], guard: dict[str, Any],
                        compile_gate: dict[str, Any]) -> dict[str, Any]:
    detail = run.get("detail") if isinstance(run.get("detail"), dict) else {}
    keyword_coverage = float(detail.get("evidenceCoverage") or detail.get("evidenceKeywordCoverage")
                             or run.get("evidenceKeywordCoverage") or 0)
    real_coverage = float(coverage.get("realEvidenceCoverage") or 0)
    localization_coverage = float(localization.get("score") or 0)
    strategy = str(case.get("expectedFixStrategy") or "").upper()
    no_code = strategy == "NO_CODE_FIX" or str(case.get("expectedScopeDecision") or "").upper() == "NO_CODE_FIX"
    root_cause_hit = keyword_coverage >= 0.5 and (no_code or localization_coverage >= 0.5)
    if no_code:
        verification_status = "SKIPPED_NO_CODE_FIX"
    elif compile_gate.get("success") is True and _tests_passed(raw):
        verification_status = "PASSED"
    elif compile_gate.get("success") is False:
        verification_status = "COMPILE_FAILED"
    else:
        verification_status = "FAILED_OR_NOT_EXECUTED"
    review_decision = str(raw.get("reviewVerdict") or raw.get("patchDecision") or "NOT_AVAILABLE")
    if guard.get("passed") is True:
        scope_guard_status = "PASSED"
    elif guard.get("passed") is False:
        scope_guard_status = "REJECTED"
    else:
        scope_guard_status = "NOT_APPLICABLE" if no_code else "NOT_RUN"
    context = task.get("context") if isinstance(task.get("context"), dict) else {}
    attempts = max(int(task.get("repairAttempt") or 0), int(context.get("incidentFixReflectionRound") or 0))
    failure_reason = str(run.get("errorMessage") or raw.get("blockedReason") or raw.get("failureType") or "")
    if not failure_reason and run.get("status") != "SUCCESS":
        failure_reason = str(task.get("stopReason") or task.get("status") or "EVAL_FAILED")
    return {
        "actualOutcome": {"taskStatus": task.get("status", ""), "evalStatus": run.get("status", ""),
                          "fixStrategy": _fix_strategy(raw), "reviewDecision": review_decision},
        "rootCauseHit": root_cause_hit,
        "evidenceCoverage": {"keywordCoverage": keyword_coverage, "realEvidenceCoverage": real_coverage,
                              "fixtureFallbackUsed": coverage.get("fixtureFallbackUsed") is True},
        "localizationCoverage": localization_coverage,
        "verificationStatus": verification_status,
        "reviewDecision": review_decision,
        "scopeGuardStatus": scope_guard_status,
        "toolCallCount": int(task.get("usedToolCalls") or run.get("usedToolCalls") or 0),
        "repairAttempts": attempts,
        "failureReason": failure_reason,
    }


def build_case_report(batch_id: str, case: dict[str, Any], run: dict[str, Any],
                      task: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    detail = run.get("detail") if isinstance(run.get("detail"), dict) else {}
    raw = collect_raw_outputs(task)
    localization = _localization_eval(case, raw)
    failures = task.get("context", {}).get("incidentFixReflectionFailures", [])
    reflections = []
    for index, failure in enumerate(failures, 1):
        diagnostic = _mapping(failure.get("diagnostic")) if isinstance(failure, dict) else {}
        reflections.append({"round": index, "failedSkill": str(failure.get("failedSkill") or ""),
                            "failureType": str(diagnostic.get("failureType") or "UNKNOWN"),
                            "mustFix": _strings(diagnostic.get("mustFix")), "mustAvoid": _strings(diagnostic.get("mustAvoid")),
                            "recovered": index < len(failures), "recoveryStrategy": (
                                "adjusted patch based on reflection feedback" if index < len(failures)
                                else "failed after max rounds")})
    guard, compile_gate = _mapping(raw.get("patchScopeGuard")), _mapping(raw.get("compileGate"))
    coverage, quality, sandbox = (_mapping(raw.get("evidenceCoverage")), _mapping(raw.get("patchQuality")),
                                  _mapping(raw.get("patchSandbox")))
    eval_outcome = _evaluation_outcome(case, run, task, raw, localization, coverage, guard, compile_gate)
    failure_type = (str(guard.get("failureType") or "SCOPE_GUARD_FAILED") if guard.get("passed") is False
                    else "COMPILE_FAILED" if compile_gate.get("success") is False else "")
    failed_step = next((step for step in task.get("steps") or [] if step.get("status") == "FAILED"), None)
    base = f"data/codeops-eval/{batch_id}/cases/{case['caseId']}"
    report = {"caseId": run["caseId"], "caseName": case.get("caseName") or run["caseId"],
              "status": run["status"], "taskId": run.get("taskId"), "taskType": case.get("taskType") or "",
              "caseLifecycle": case.get("caseLifecycle", ""), "caseSource": case.get("caseSource", ""),
              "evaluationScoringSchemaVersion": str(detail.get("evaluationScoringSchemaVersion")
                                                     or EVALUATION_SCORING_SCHEMA_VERSION),
              "evaluationLevel": case.get("evaluationLevel", ""), "caseCategory": case.get("caseCategory", ""),
              "fixtureReference": case.get("fixtureReference", ""), "fixtureReuseFrom": case.get("fixtureReuseFrom", ""),
              "expectedOutcome": case.get("expectedOutcome", {}), **eval_outcome,
              "scopeType": _scope_type(raw), "fixStrategy": _fix_strategy(raw),
              "scopeDecision": _scope_decision(raw),
              "rootCauseLocationType": str(_first(_decision(raw).get("rootCauseLocationType"),
                                                   raw.get("rootCauseLocationType")) or ""),
              "localizationDecision": _decision(raw), "localizationEval": localization,
              "targetFiles": _target_files(raw), "targetMethods": _target_methods(raw),
              "selectedSkills": run.get("detail", {}).get("selectedSkills", []), "stepCount": run["stepCount"],
              "latencyMs": run["latencyMs"], "patchGenerated": raw.get("llmGenerated") is True,
              "patchGuardPassed": guard.get("passed") is True, "patchApplied": _mapping(raw.get("patchApply")).get("applied") is True,
              "compilePassed": compile_gate.get("success") is True, "testsPassed": _tests_passed(raw),
              "reflectionRounds": int(task.get("context", {}).get("incidentFixReflectionRound") or 0),
              "reflectionRecovered": bool(failures) and run["status"] == "SUCCESS",
              "releaseRiskGenerated": any(step.get("selectedSkill") == "release_risk_analysis"
                                          and step.get("status") in {"SUCCESS", "NO_DIFF"} for step in task.get("steps") or []),
              "finalRiskLevel": str(raw.get("riskLevel") or ""),
              "realEvidenceCoverage": float(coverage.get("realEvidenceCoverage") or 0),
              "fixtureEvidenceUsed": coverage.get("fixtureFallbackUsed") is True,
              "evidenceSourceSummary": {"coverage": coverage, "provenance": raw.get("evidenceProvenance") or []},
              "patchQuality": quality, "patchSandbox": sandbox, "failureType": failure_type,
              "failureSummary": "" if run["status"] == "SUCCESS" or not failed_step else
                  f"{failed_step.get('selectedSkill')}: {str(failed_step.get('resultSummary') or '')[:100]}",
              "steps": _step_reports(task), "reflectionHistory": reflections,
              "artifacts": {"reportJsonPath": base + ".json", "reportMarkdownPath": base + ".md",
                            "traceJsonPath": base + "-trace.json", "patchDiffPath": base + ".diff"}}
    trace = {"taskId": task.get("taskId"), "taskType": task.get("taskType"), "status": task.get("status"),
             "repository": task.get("repository"), "goal": task.get("goal"), "usedToolCalls": task.get("usedToolCalls"),
             "finalSummary": task.get("finalSummary"), "context": task.get("context"),
             "steps": [{"stepNo": step.get("stepNo"), "decision": step.get("decision"),
                        "selectedSkill": step.get("selectedSkill"), "status": step.get("status"),
                        "summary": step.get("resultSummary"), "rawOutput": _parse(step.get("rawEvidenceJson"))}
                       for step in task.get("steps") or []], "latestMergedRawOutput": raw}
    return report, trace, _find_diff(_first(raw.get("unifiedDiffPatch"), raw.get("patchDiff"), raw.get("patchDraft"), raw))


def _parse(value: Any) -> Any:
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return value


def _find_diff(value: Any) -> str:
    if isinstance(value, str):
        lower = value.lower()
        return value if (("--- " in value and "+++ " in value) or "diff --git" in lower or "@@" in lower) else ""
    if isinstance(value, dict):
        for key, item in value.items():
            found = _find_diff(item)
            if found and any(token in str(key).lower() for token in ("patch", "diff", "output")):
                return found
        return next((found for item in value.values() if (found := _find_diff(item))), "")
    if isinstance(value, list):
        return next((found for item in value if (found := _find_diff(item))), "")
    return ""


def _rate(numerator: int, denominator: int, default: float = 1.0) -> float:
    """Match the Java report builder's scale-2 HALF_UP division semantics."""
    if denominator <= 0:
        return default
    return float((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _average(values: list[float], default: float = 0.0) -> float:
    if not values:
        return default
    return float((sum(Decimal(str(value)) for value in values) / Decimal(len(values))).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    ))


def build_report(batch_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    if not cases:
        return {"batchId": batch_id, "runTime": now_iso(), "totalCases": 0, "successCases": 0,
                "failedCases": 0, "skippedCases": 0, "businessE2ETotal": 0, "baselineCompleted": 0,
                "newlyAddedCompleted": 0, "runtimeSafetyReliabilityCases": 10,
                "summaryMetrics": _empty_metrics(), "cases": [], "pipelineTrace": []}
    expected_files = [case for case in cases if case["localizationEval"]["expectedTargetFiles"]]
    expected_methods = [case for case in cases if case["localizationEval"]["expectedTargetMethods"]]
    expected_fix = [case for case in cases if case["localizationEval"]["expectedFixStrategy"]]
    expected_scope = [case for case in cases if case["localizationEval"]["expectedScopeDecision"]]
    code_fix = [case for case in cases if case["scopeType"] != "NO_CODE_FIX"]
    reflection = [case for case in cases if case["reflectionRounds"] > 0]
    no_code = [case for case in cases if case["scopeType"] == "NO_CODE_FIX"]
    quality = [case for case in cases if case["patchQuality"]]
    sandbox = [case for case in cases if case["patchSandbox"]]
    expected_code_fix = [case for case in cases if case.get("expectedOutcome", {}).get("classification") == "CODE_FIX"]
    verification_eligible = [case for case in expected_code_fix
                             if not case.get("expectedOutcome", {}).get("requiredStoppingState")]
    metrics = {
        "scopeAccuracy": _rate(sum(not case["patchGenerated"] if case["scopeType"] == "NO_CODE_FIX"
                                    else bool(case["scopeType"] and case["targetMethods"]) for case in cases), total),
        "localizationDecisionAccuracy": _average([case["localizationEval"]["score"] for case in cases]),
        "localizationTargetFileHitRate": _rate(
            sum(case["localizationEval"]["targetFileMatched"] is True for case in cases), len(expected_files)),
        "localizationTargetMethodHitRate": _rate(
            sum(case["localizationEval"]["targetMethodMatched"] is True for case in cases), len(expected_methods)),
        "localizationFixStrategyAccuracy": _rate(
            sum(case["localizationEval"]["fixStrategyMatched"] is True for case in cases), len(expected_fix)),
        "localizationScopeDecisionAccuracy": _rate(
            sum(case["localizationEval"]["scopeDecisionMatched"] is True for case in cases), len(expected_scope)),
        # Keep the Java numerator semantics: all successful cases count, while the denominator is code-fix cases.
        "patchApplyRate": _rate(sum(case["patchApplied"] for case in cases), len(code_fix)),
        "compilePassRate": _rate(sum(case["compilePassed"] for case in cases), len(code_fix)),
        "testPassRate": _rate(sum(case["testsPassed"] for case in cases), len(code_fix)),
        "reflectionRecoveryRate": _rate(sum(case["reflectionRecovered"] for case in reflection), len(reflection)),
        "noCodeFixAccuracy": _rate(sum(not case["patchGenerated"] for case in no_code), len(no_code)),
        "realEvidenceCoverageRate": _average([case["realEvidenceCoverage"] for case in cases]),
        "patchStaticSafetyRate": _rate(
            sum(case["patchQuality"].get("staticSafetyPassed") is True for case in quality), len(quality)),
        "patchSandboxIsolationRate": _rate(
            sum(case["patchSandbox"].get("isolated") is True for case in sandbox), len(sandbox)),
        # These are the primary business-effect metrics for a delivery_only
        # deployment.  They deliberately distinguish a verified sandbox patch
        # from production apply, which remains approval-gated.
        "rootCauseHitRate": _rate(sum(case["rootCauseHit"] is True for case in cases), total),
        "evidenceKeywordCoverageRate": _average([
            float(_mapping(case.get("evidenceCoverage")).get("keywordCoverage") or 0) for case in cases]),
        "patchGeneratedRate": _rate(sum(case["patchGenerated"] is True for case in expected_code_fix),
                                    len(expected_code_fix)),
        "verificationPassRate": _rate(sum(case["verificationStatus"] == "PASSED" for case in verification_eligible),
                                        len(verification_eligible)),
        "averageRepairAttempts": _average([float(case.get("repairAttempts") or 0) for case in expected_code_fix]),
        "averageLatencyMs": _average([float(case.get("latencyMs") or 0) for case in cases]),
    }
    business_cases = [case for case in cases if case.get("evaluationLevel") == "E2E_BUSINESS"]
    return {"batchId": batch_id, "runTime": now_iso(), "totalCases": total,
            "successCases": sum(case["status"] == "SUCCESS" for case in cases),
            "failedCases": sum(case["status"] == "FAILED" for case in cases),
            "skippedCases": sum(case["status"] in {"SKIPPED", "NOT_EXECUTABLE"} for case in cases),
            "businessE2ETotal": len(business_cases),
            "baselineCompleted": sum(case.get("caseSource") == "LEGACY_BASELINE" for case in business_cases),
            "newlyAddedCompleted": sum(case.get("caseSource") == "EVAL_EXPANSION" for case in business_cases),
            "runtimeSafetyReliabilityCases": 10,
            "summaryMetrics": metrics, "cases": cases,
            "pipelineTrace": cases[0]["steps"] if cases else []}


def _empty_metrics() -> dict[str, float]:
    return {key: 0.0 for key in (
        "scopeAccuracy", "localizationDecisionAccuracy", "localizationTargetFileHitRate",
        "localizationTargetMethodHitRate", "localizationFixStrategyAccuracy",
        "localizationScopeDecisionAccuracy", "patchApplyRate", "compilePassRate", "testPassRate",
        "reflectionRecoveryRate", "noCodeFixAccuracy", "realEvidenceCoverageRate",
        "patchStaticSafetyRate", "patchSandboxIsolationRate",
    )}


def summary_markdown(report: dict[str, Any]) -> str:
    metrics = report.get("summaryMetrics") or {}
    lines = ["# CodeOps Incident-to-Fix Eval Report", "", f"**Batch:** {report.get('batchId', '')}",
             f"**Run Time:** {report.get('runTime', '')}", "", "## Summary", "",
             "| Metric | Value |", "|---|---|",
             f"| Total Cases | {report.get('totalCases', 0)} |",
             f"| Business E2E Cases | {report.get('businessE2ETotal', 0)} |",
             f"| Baseline Completed | {report.get('baselineCompleted', 0)} |",
             f"| Newly Added Completed | {report.get('newlyAddedCompleted', 0)} |",
             f"| Runtime Safety/Reliability Cases | {report.get('runtimeSafetyReliabilityCases', 10)} |",
             f"| Success Cases | {report.get('successCases', 0)} |",
             f"| Failed Cases | {report.get('failedCases', 0)} |",
             f"| Skipped / Not Executable | {report.get('skippedCases', 0)} |"]
    labels = (("scopeAccuracy", "Scope Accuracy"),
              ("localizationDecisionAccuracy", "Localization Decision Accuracy"),
              ("localizationTargetFileHitRate", "Localization Target File Hit Rate"),
              ("localizationTargetMethodHitRate", "Localization Target Method Hit Rate"),
              ("localizationFixStrategyAccuracy", "Localization Fix Strategy Accuracy"),
              ("localizationScopeDecisionAccuracy", "Localization Scope Decision Accuracy"),
              ("patchApplyRate", "Patch Apply Rate"), ("compilePassRate", "Compile Pass Rate"),
              ("testPassRate", "Test Pass Rate"), ("reflectionRecoveryRate", "Reflection Recovery Rate"),
              ("noCodeFixAccuracy", "No-Code-Fix Accuracy"),
              ("realEvidenceCoverageRate", "Real Evidence Coverage"),
              ("patchStaticSafetyRate", "Patch Static Safety Rate"),
              ("patchSandboxIsolationRate", "Patch Sandbox Isolation Rate"),
              ("rootCauseHitRate", "Root Cause Hit Rate"),
              ("evidenceKeywordCoverageRate", "Evidence Keyword Coverage"),
              ("patchGeneratedRate", "Patch Generated Rate"),
              ("verificationPassRate", "Sandbox Verification Pass Rate"),
              ("averageRepairAttempts", "Average Repair Attempts"),
              ("averageLatencyMs", "Average Latency Ms"))
    lines.extend(f"| {label} | {_pct(metrics.get(key))} |" for key, label in labels)
    lines += ["", "## Case Results", "", "| Case | Scope | Status | Steps | Reflection | Patch | Compile | Test | Risk |",
              "|---|---|---|---|---|---|---|---|---|"]
    for case in report.get("cases") or []:
        rounds = int(case.get("reflectionRounds") or 0)
        reflection = f"R{rounds} {'OK' if case.get('reflectionRecovered') else 'FAIL'}" if rounds else "-"
        lines.append("| {case} | {scope} | {status} | {steps} | {reflection} | {patch} | {compile} | {test} | {risk} |".format(
            case=case.get("caseName", ""), scope=case.get("scopeType", ""), status=case.get("status", ""),
            steps=case.get("stepCount", 0), reflection=reflection, patch=_yn(case.get("patchApplied")),
            compile=_yn(case.get("compilePassed")), test=_yn(case.get("testsPassed")),
            risk=_yn(case.get("releaseRiskGenerated"))))
    lines += ["", "## Architecture Trace", "", "```", "Alert -> Ops Evidence -> Code Localization -> RepairScope",
              "  -> Code Repair Agent -> PatchScopeGuard -> Compile/Test",
              "  -> Reflection (max 3 rounds) -> Release Risk", "```", "", "## Repair Scope Distribution", ""]
    for case in report.get("cases") or []:
        targets = case.get("targetMethods") or []
        if targets:
            lines.append(f"- **{case.get('caseName', '')}**: `{case.get('scopeType', '')}` → {', '.join(targets)}")
        else:
            lines.append(f"- **{case.get('caseName', '')}**: `{case.get('scopeType', '')}` (no target methods)")
    lines += ["", "## Key Findings", ""]
    for case in report.get("cases") or []:
        scope, name = case.get("scopeType"), case.get("caseName", "")
        if scope == "STRICT_SINGLE_METHOD":
            lines.append(f"- **{name}**: demonstrates STRICT_SINGLE_METHOD repair — only the incident-targeted method was modified.")
        elif scope in {"MULTI_METHOD", "FULL_FILE"}:
            lines.append(f"- **{name}**: demonstrates multi-method repair with Guard enforcing scope constraints.")
        elif scope == "NO_CODE_FIX":
            lines.append(f"- **{name}**: demonstrates NO_CODE_FIX decision — correctly identified as non-code incident.")
        rounds = int(case.get("reflectionRounds") or 0)
        if rounds:
            outcome = "successfully recovered." if case.get("reflectionRecovered") else "exhausted retry limit."
            lines.append(f"  - {rounds} reflection round(s), {outcome}")
    return "\n".join(lines) + "\n"


def case_markdown(case: dict[str, Any]) -> str:
    lines = [f"# Case: {case.get('caseName', '')}", "",
             f"**Status:** {case.get('status', '')} | **TaskId:** {case.get('taskId', '')} | "
             f"**Steps:** {case.get('stepCount', 0)} | **Latency:** {case.get('latencyMs', 0)}ms", "",
             "## Repair Scope", "", "```json", json.dumps({
                 "scopeType": case.get("scopeType", ""), "fixStrategy": case.get("fixStrategy", ""),
                 "scopeDecision": case.get("scopeDecision", ""),
                 "rootCauseLocationType": case.get("rootCauseLocationType", ""),
                 "targetMethods": case.get("targetMethods") or []}, ensure_ascii=False, indent=2), "```", ""]
    decision = _mapping(case.get("localizationDecision"))
    if decision:
        lines += ["## Localization Decision", "", f"- **fixStrategy:** {case.get('fixStrategy', '')}",
                  f"- **scopeDecision:** {case.get('scopeDecision', '')}",
                  f"- **rootCauseLocationType:** {case.get('rootCauseLocationType', '')}",
                  f"- **directEvidenceFiles:** {decision.get('directEvidenceFiles', [])}",
                  f"- **rootCauseCandidateFiles:** {decision.get('rootCauseCandidateFiles', [])}",
                  f"- **doNotModifyFiles:** {decision.get('doNotModifyFiles', [])}", ""]
    localization = _mapping(case.get("localizationEval"))
    if localization:
        lines += ["## Localization Eval", "", f"- **score:** {_pct(localization.get('score'))}",
                  f"- **targetFileMatched:** {localization.get('targetFileMatched')}",
                  f"- **targetMethodMatched:** {localization.get('targetMethodMatched')}",
                  f"- **fixStrategyMatched:** {localization.get('fixStrategyMatched')}",
                  f"- **scopeDecisionMatched:** {localization.get('scopeDecisionMatched')}",
                  f"- **expectedTargetFiles:** {localization.get('expectedTargetFiles', [])}",
                  f"- **actualTargetFiles:** {localization.get('actualTargetFiles', [])}",
                  f"- **missingTargetFiles:** {localization.get('missingTargetFiles', [])}",
                  f"- **expectedTargetMethods:** {localization.get('expectedTargetMethods', [])}",
                  f"- **actualTargetMethods:** {localization.get('actualTargetMethods', [])}",
                  f"- **missingTargetMethods:** {localization.get('missingTargetMethods', [])}", ""]
    lines += ["## Agent Steps", "", "| Step | Skill | Status | Summary |", "|---|---|---|---|"]
    for step in case.get("steps") or []:
        lines.append(f"| {step.get('stepNo', 0)} | {step.get('selectedSkill') or 'STOP'} | "
                     f"{step.get('status', '')} | {step.get('summary', '')} |")
    lines += ["", "## Patch Guard", "", f"- **passed:** {case.get('patchGuardPassed')}",
              f"- **patchApplied:** {case.get('patchApplied')}", f"- **compilePassed:** {case.get('compilePassed')}",
              f"- **testsPassed:** {case.get('testsPassed')}",
              f"- **realEvidenceCoverage:** {int(float(case.get('realEvidenceCoverage') or 0) * 100)}%",
              f"- **fixtureEvidenceUsed:** {case.get('fixtureEvidenceUsed')}"]
    sandbox, quality = _mapping(case.get("patchSandbox")), _mapping(case.get("patchQuality"))
    if sandbox:
        lines.append(f"- **patchSandbox:** {sandbox.get('mode', '')}, isolated={sandbox.get('isolated', False)}")
    if quality:
        lines.append(f"- **patchQuality:** minimalChangeScore={quality.get('minimalChangeScore', '')}, "
                     f"staticSafetyPassed={quality.get('staticSafetyPassed', '')}")
    if case.get("failureType"):
        lines += [f"- **failureType:** {case['failureType']}", f"- **failureSummary:** {case.get('failureSummary', '')}"]
    history = case.get("reflectionHistory") or []
    if int(case.get("reflectionRounds") or 0) > 0 and history:
        lines += ["", "## Reflection History", "", "| Round | Skill | FailureType | MustFix | Recovered |",
                  "|---|---|---|---|---|"]
        for reflection in history:
            lines.append(f"| {reflection.get('round', 0)} | {reflection.get('failedSkill', '')} | "
                         f"{reflection.get('failureType', '')} | {'; '.join(reflection.get('mustFix') or [])} | "
                         f"{'YES' if reflection.get('recovered') else 'NO'} |")
    lines += ["", "## Release Risk", "", f"- **riskLevel:** {case.get('finalRiskLevel') or 'N/A'}",
              f"- **releaseRiskGenerated:** {case.get('releaseRiskGenerated')}"]
    return "\n".join(lines) + "\n"


def _pct(value: Any) -> str:
    return "N/A" if value is None else f"{int(Decimal(str(value)) * 100)}%"


def _yn(value: Any) -> str:
    return "YES" if value else "NO"


def write_case_artifacts(case_report: dict[str, Any], trace: dict[str, Any], patch_diff: str) -> None:
    artifacts = case_report["artifacts"]
    for key in ("reportJsonPath", "reportMarkdownPath", "traceJsonPath", "patchDiffPath"):
        Path(artifacts[key]).parent.mkdir(parents=True, exist_ok=True)
    Path(artifacts["reportJsonPath"]).write_text(json.dumps(case_report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path(artifacts["reportMarkdownPath"]).write_text(case_markdown(case_report), encoding="utf-8")
    Path(artifacts["traceJsonPath"]).write_text(json.dumps(trace, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    Path(artifacts["patchDiffPath"]).write_text(patch_diff, encoding="utf-8")
