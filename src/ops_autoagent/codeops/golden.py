"""Stable, chain-level observations used by CodeOps golden tests.

The projection deliberately excludes generated ids, timestamps, model prose and absolute paths.  It keeps the
observable contract that must remain equal for the same input: task/status transitions, emitted hooks and
notifications, patch/test side effects, and classified failures.
"""
from __future__ import annotations

import json
from typing import Any


def chain_contract(state: dict[str, Any]) -> dict[str, Any]:
    context = _mapping(state.get("context") or _mapping(state.get("task")).get("context"))
    raws = _raw_outputs(state.get("steps"))
    bugfix, verification, release = raws.get("bug_fix", {}), raws.get("test_verification", {}), raws.get("release_risk_analysis", {})
    notifications = _list(context.get("taskNotifications") or verification.get("taskNotifications"))
    observations = _list(context.get("repairObservations") or verification.get("repairObservations"))
    return {
        "taskStatus": state.get("status") or _mapping(state.get("task")).get("status", ""),
        "steps": [{"skill": step.get("selectedSkill"), "status": step.get("status")} for step in _list(state.get("steps"))],
        "bugFix": {"phase": bugfix.get("phase", ""), "scopeGuardPassed": _mapping(bugfix.get("patchScopeGuard")).get("passed"),
                   "patchApplied": _mapping(bugfix.get("patchApply")).get("applied"),
                   "compilePassed": _mapping(bugfix.get("compileGate")).get("success"),
                   "patchRolledBack": bugfix.get("patchRolledBack", False)},
        "testVerification": {"phase": verification.get("phase", ""), "status": _step_status(state, "test_verification"),
                             "testsPassed": verification.get("testsPassed"), "failureType": verification.get("testFailureType", ""),
                             "patchRolledBack": verification.get("testPatchRolledBack", False),
                             "backgroundStatuses": [item.get("status") for item in _list(verification.get("backgroundToolTasks"))]},
        "releaseRisk": {"phase": release.get("phase", ""), "riskLevel": _mapping(release.get("releaseRiskReport")).get("riskLevel", ""),
                        "reviewVerdict": release.get("reviewVerdict", ""),
                        "manualTakeoverRequired": release.get("manualTakeoverRequired", False),
                        "redLines": _list(_mapping(release.get("patchFacts")).get("redLines"))},
        "events": [item.get("action") for item in observations],
        "notifications": [{"type": item.get("type"), "status": item.get("status"), "consumed": item.get("consumed")}
                          for item in notifications],
    }


def assert_golden_contract(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Raise an actionable assertion when a stable chain observation has changed."""
    missing = _diff(expected, actual)
    if missing:
        raise AssertionError("CodeOps golden contract mismatch:\n" + json.dumps(missing, ensure_ascii=False, indent=2))


def _raw_outputs(steps: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for step in _list(steps):
        skill = str(step.get("selectedSkill") or "") if isinstance(step, dict) else ""
        raw = step.get("rawEvidenceJson") if isinstance(step, dict) else None
        if isinstance(raw, dict):
            result[skill] = raw
        elif isinstance(raw, str):
            try:
                value = json.loads(raw)
                result[skill] = value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                result[skill] = {}
    return result


def _step_status(state: dict[str, Any], skill: str) -> str:
    for step in reversed(_list(state.get("steps"))):
        if isinstance(step, dict) and step.get("selectedSkill") == skill:
            return str(step.get("status") or "")
    return ""


def _diff(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(expected, dict):
        source = _mapping(actual)
        return [issue for key, value in expected.items() for issue in _diff(value, source.get(key), f"{path}.{key}")]
    if isinstance(expected, list):
        return [] if expected == actual else [{"path": path, "expected": expected, "actual": actual}]
    return [] if expected == actual else [{"path": path, "expected": expected, "actual": actual}]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []
