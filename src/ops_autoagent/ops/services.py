from __future__ import annotations

import asyncio
import json
import re
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from ..schemas import Alert, AlertmanagerWebhook, now_iso
from ..store import Store


class SensitiveMasker:
    RULES = [
        (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1***"),
        (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"), r"\1***"),
        (re.compile(r"\b1[3-9]\d{9}\b"), "***PHONE***"),
        (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "***EMAIL***"),
        (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "***CARD***"),
    ]

    def mask(self, value: str | None) -> str:
        text = value or ""
        for pattern, replacement in self.RULES:
            text = pattern.sub(replacement, text)
        return text

    def sanitize(self, value: Any, key: str = "") -> Any:
        if value is None:
            return None
        if any(secret in key.lower() for secret in ("key", "token", "secret", "password", "authorization")):
            return "***"
        if isinstance(value, dict):
            return {str(k): self.sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(item, key) for item in value]
        return self.mask(value) if isinstance(value, str) else value


@dataclass
class InvestigationStep:
    step_id: str
    tool: str
    objective: str
    required: bool
    status: str = "PENDING"
    reason: str = ""


class InvestigationPlanner:
    def build(self, request: dict[str, Any], max_steps: int = 10) -> dict[str, Any]:
        steps = [
            InvestigationStep("intent", "internal.intent", "Normalize incident intent and time window", True),
            InvestigationStep("metrics", "query_prometheus", "Confirm error, latency and resource anomalies", True),
            InvestigationStep("logs", "query_elasticsearch", "Find exceptions and correlated trace IDs", True),
        ]
        if request.get("traceId"):
            steps.append(InvestigationStep("traces", "query_skywalking_trace", "Inspect the supplied trace", True))
        else:
            steps.append(InvestigationStep("traces", "query_skywalking_trace", "Inspect traces discovered from logs", False,
                                           reason="Trace ID may be discovered during log analysis"))
        steps.extend([
            InvestigationStep("correlation", "internal.correlate", "Correlate cross-source evidence", True),
            InvestigationStep("runbook", "search_runbook", "Retrieve operational procedures", True),
            InvestigationStep("review", "llm_evidence_reviewer", "Audit sufficiency and contradictions", True),
            InvestigationStep("report", "llm_report_writer", "Produce final evidence-grounded report", True),
        ])
        selected = steps[:max(1, min(max_steps, len(steps)))]
        return {"planId": f"plan-{uuid.uuid4()}", "status": "RUNNING", "maxSteps": max_steps,
                "steps": [{"stepId": step.step_id, "tool": step.tool, "objective": step.objective,
                           "required": step.required, "status": step.status, "reason": step.reason}
                          for step in selected], "createTime": now_iso(), "updateTime": now_iso()}


class EvidenceSignalExtractor:
    def extract(self, metrics: dict[str, Any], logs: dict[str, Any], traces: dict[str, Any],
                command: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        self._extract_metrics(metrics, command, signals)
        self._extract_collection(logs, command, signals, "elasticsearch", "log", "errorSamples", "log_sample")
        self._extract_collection(traces, command, signals, "skywalking", "trace", "spans", "trace_span")
        return signals

    def _extract_metrics(self, evidence: dict[str, Any], command: dict[str, Any] | None,
                         signals: list[dict[str, Any]]) -> None:
        observations = evidence.get("observations") or []
        if not observations:
            signals.append(self._signal(command, "prometheus", "metric", "prometheus_collection",
                                        "NO_ANOMALY" if evidence.get("available") else "UNAVAILABLE", "unknown",
                                        str(evidence.get("summary") or ""), str(evidence.get("rawData") or "")))
            return
        for observation in observations:
            if not str(observation).strip():
                continue
            value = str(observation).strip()
            lower = value.lower()
            status = ("ANOMALY" if lower.startswith("anomaly:") else
                      "NO_DATA" if lower.startswith("no_data:") else
                      "UNKNOWN" if lower.startswith("unknown:") else
                      "OK" if lower.startswith("ok:") else "OBSERVED")
            remainder = value.split(":", 1)[1].strip() if ":" in value else value
            name = remainder.split(" ", 1)[0] or "metric_observation"
            signals.append(self._signal(command, "prometheus", "metric", name, status,
                                        "high" if status == "ANOMALY" else "informational", value, value))

    def _extract_collection(self, evidence: dict[str, Any], command: dict[str, Any] | None,
                            signals: list[dict[str, Any]], source: str, evidence_type: str,
                            collection_key: str, name_prefix: str) -> None:
        values = evidence.get(collection_key) or []
        if not values:
            signals.append(self._signal(command, source, evidence_type, f"{evidence_type}_collection",
                                        "NO_ANOMALY" if evidence.get("available") else "UNAVAILABLE", "unknown",
                                        str(evidence.get("summary") or ""), str(evidence.get("rawData") or "")))
            return
        index = 1
        for value in values:
            if not str(value).strip():
                continue
            text = str(value)
            signals.append(self._signal(command, source, evidence_type, f"{name_prefix}_{index}",
                                        "OBSERVED", "medium", text, text))
            index += 1

    @staticmethod
    def _signal(command: dict[str, Any] | None, source: str, evidence_type: str, name: str,
                status: str, severity: str, summary: str, raw_evidence: str) -> dict[str, Any]:
        command = command or {}
        return {"signalId": f"signal-{uuid.uuid4()}", "source": source, "evidenceType": evidence_type,
                "name": name or f"{evidence_type}_evidence", "status": status or "OBSERVED",
                "entity": command.get("serviceName", ""), "severity": severity or "unknown",
                "timeWindow": f"{command.get('startTime', '')} ~ {command.get('endTime', '')}",
                "summary": summary or "", "rawEvidence": raw_evidence or ""}


class EvidenceReviewer:
    SOURCE_SPECS = (
        ("Prometheus metrics", "query_prometheus"),
        ("ELK logs", "query_elasticsearch"),
        ("SkyWalking traces", "query_skywalking_trace"),
    )

    def review(self, metrics: dict[str, Any], logs: dict[str, Any], traces: dict[str, Any],
               runbooks: list[dict[str, Any]], round_number: int, max_rounds: int,
               command: dict[str, Any], enabled: bool = True, min_confidence: int = 75) -> dict[str, Any]:
        if not enabled:
            return {"status": "BYPASSED", "round": round_number, "sufficient": True,
                    "confidenceScore": 100,
                    "confirmedFacts": ["Evidence Reviewer Agent is disabled by configuration."],
                    "weakEvidence": [], "missingEvidence": [], "requiredTools": [],
                    "reportConstraints": ["Use the existing evidence chain and explicitly state unavailable data sources."],
                    "sufficiency": self._sufficiency(True, True, True, True, [], [],
                                                     "Reviewer disabled by configuration.",
                                                     root_cause_support=True,
                                                     root_cause_specific=True),
                    "rationale": "Reviewer disabled."}
        confirmed, weak, missing, required = [], [], [], []
        evidence_values = (metrics, logs, traces)
        for (source_name, tool), evidence in zip(self.SOURCE_SPECS, evidence_values, strict=True):
            if evidence.get("available"):
                confirmed.append(f"{source_name} available: {evidence.get('summary') or 'summary is empty'}")
            else:
                missing.append(f"{source_name} is unavailable or was skipped, so it should be collected before "
                               "finalizing a high-confidence conclusion.")
                required.append(tool)
        if runbooks:
            confirmed.append(f"Runbook patterns available: matched patterns={len(runbooks)}")
        else:
            missing.append("Runbook patterns is unavailable or was skipped, so it should be collected before "
                           "finalizing a high-confidence conclusion.")
            required.append("query_runbook")
            weak.append("No Runbook/RAG context is available for root-cause analysis.")
        available_sources = sum(bool(item.get("available")) for item in evidence_values)
        if available_sources < 2:
            weak.append("Fewer than two external evidence sources are available.")
        missing_critical = list(missing)
        if available_sources < 2:
            missing_critical.append("At least two independent telemetry sources are required before finalizing a root cause.")
        if not runbooks:
            missing_critical.append("Runbook/RAG context is required before finalizing a root cause.")
        sufficiency = self._sufficiency(
            available_sources > 0, available_sources >= 2, available_sources >= 2,
            bool(command.get("serviceName")) and available_sources > 0, self._dedupe(missing_critical), [],
            f"Rule baseline only checks evidence-source coverage. It does not finalize root cause. weakEvidence={weak}",
            runbook_support=bool(runbooks), collectable_gap=bool(missing))
        can_supplement = round_number < max_rounds and bool(required)
        status = "NEED_MORE_EVIDENCE" if can_supplement else "INSUFFICIENT_FINAL"
        if not can_supplement:
            required.clear()
            missing.append("Evidence Reviewer Chat Agent must decide root cause. Rule baseline cannot finalize a root cause by itself.")
        constraints = [
            "Only write confirmed facts that are supported by Prometheus, ELK, SkyWalking, or the structured evidence chain.",
            f"Do not treat user description as confirmed evidence. service={command.get('serviceName') or 'unknown'}",
            "Runbook can only support remediation suggestions, not incident facts.",
            "Because evidence is insufficient, final root cause must be written as a hypothesis with missing evidence listed.",
        ]
        return {"status": status, "round": round_number, "sufficient": False, "confidenceScore": 0,
                "confirmedFacts": confirmed, "weakEvidence": weak, "missingEvidence": self._dedupe(missing),
                "requiredTools": self._dedupe(required), "reportConstraints": constraints,
                "sufficiency": sufficiency, "candidateRootCauses": [],
                "rationale": f"status={status}, availableSources={available_sources}, topConfidence=0, "
                             f"threshold={min_confidence}, round={round_number}/{max_rounds}; "
                             "ruleBaseline=NO_ROOT_CAUSE_DECISION"}

    @staticmethod
    def _sufficiency(direct: bool, multi: bool, coverage: bool, entity: bool,
                     missing: list[str], contradictions: list[str], rationale: str,
                     runbook_support: bool = True, collectable_gap: bool = False,
                     root_cause_support: bool = False,
                     root_cause_specific: bool = False) -> dict[str, Any]:
        return {"directEvidence": direct, "multiSourceSupport": multi, "sourceCoverage": coverage,
                "rootCauseSupport": root_cause_support,
                "rootCauseSpecificEvidence": root_cause_specific,
                "negativeEvidenceConsidered": multi, "collectableEvidenceGap": collectable_gap,
                "temporalAlignment": True, "entityAlignment": entity, "runbookSupport": runbook_support,
                "noContradiction": not contradictions, "missingCriticalEvidence": missing,
                "contradictions": contradictions, "rationale": rationale}

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value and value.strip()))

    def normalize_chat(self, parsed: dict[str, Any], fallback: dict[str, Any],
                       round_number: int, max_rounds: int) -> dict[str, Any]:
        """Normalize and gate the Chat Agent result exactly like OpsEvidenceReviewerService."""
        status = str(parsed.get("status") or "").strip()
        if not status:
            return fallback
        result = {
            "status": status,
            "round": parsed.get("round", round_number),
            "sufficient": parsed.get("sufficient", fallback["sufficient"]),
            "confidenceScore": parsed.get("confidenceScore", fallback["confidenceScore"]),
            "confirmedFacts": self._string_list(parsed.get("confirmedFacts")),
            "weakEvidence": self._string_list(parsed.get("weakEvidence")),
            "missingEvidence": self._string_list(parsed.get("missingEvidence")),
            "requiredTools": self._string_list(parsed.get("requiredTools")),
            "reportConstraints": self._string_list(parsed.get("reportConstraints")),
            "evidenceSemantics": self._object_list(parsed.get("evidenceSemantics")),
            "sufficiency": self._parse_sufficiency(parsed.get("sufficiency"), fallback.get("sufficiency", {})),
            "conclusionType": parsed.get("conclusionType") or self._infer_conclusion(status, parsed.get("rootCause")),
            "rootCause": parsed.get("rootCause"),
            "rootCauseCategory": parsed.get("rootCauseCategory"),
            "rootCauseConfidence": parsed.get("rootCauseConfidence", parsed.get("confidenceScore",
                                                                                 fallback["confidenceScore"])),
            "rootCauseRationale": parsed.get("rootCauseRationale"),
            "candidateRootCauses": self._string_list(parsed.get("candidateRootCauses")),
            "rationale": parsed.get("rationale") or "Chat Agent reviewer result.",
        }
        try:
            result["confidenceScore"] = int(result["confidenceScore"] or 0)
            result["rootCauseConfidence"] = int(result["rootCauseConfidence"] or 0)
            result["round"] = int(result["round"] or round_number)
        except (TypeError, ValueError):
            return fallback
        return self._enforce_sufficiency(result, fallback, round_number, max_rounds)

    def _enforce_sufficiency(self, result: dict[str, Any], fallback: dict[str, Any],
                             round_number: int, max_rounds: int) -> dict[str, Any]:
        if result.get("status") != "SUFFICIENT" and not result.get("sufficient"):
            return result
        violations = self._sufficiency_violations(result)
        if not violations:
            return result
        required = result.get("requiredTools") or fallback.get("requiredTools") or []
        can_supplement = round_number < max_rounds and bool(required)
        result["status"] = "NEED_MORE_EVIDENCE" if can_supplement else "INSUFFICIENT_FINAL"
        result["sufficient"] = False
        result["requiredTools"] = required
        result["missingEvidence"] = self._dedupe([*result.get("missingEvidence", []), *violations])
        result["reportConstraints"] = self._dedupe([
            *result.get("reportConstraints", []),
            "Evidence sufficiency rubric failed; do not finalize root cause.",
        ])
        result["rationale"] = (str(result.get("rationale") or "")
                               + f"; sufficiencyGate=DOWNGRADED, violations={violations}")
        return result

    def _sufficiency_violations(self, result: dict[str, Any]) -> list[str]:
        violations: list[str] = []
        conclusion = str(result.get("conclusionType") or "")
        if conclusion in {"ROOT_CAUSE_CONFIRMED", "PROBABLE_ROOT_CAUSE"} and not result.get("rootCause"):
            violations.append("Agent marked sufficient but did not provide rootCause.")
        if not conclusion:
            violations.append("Agent marked sufficient but did not provide conclusionType.")
        rubric = result.get("sufficiency")
        if not isinstance(rubric, dict):
            return [*violations, "Agent marked sufficient but did not provide sufficiency rubric."]
        checks = (
            ("directEvidence", "Sufficiency rubric failed: directEvidence=false."),
            ("temporalAlignment", "Sufficiency rubric failed: temporalAlignment=false."),
            ("entityAlignment", "Sufficiency rubric failed: entityAlignment=false."),
            ("runbookSupport", "Sufficiency rubric failed: runbookSupport=false."),
            ("noContradiction", "Sufficiency rubric failed: noContradiction=false."),
        )
        violations.extend(message for key, message in checks if rubric.get(key) is not True)
        if rubric.get("collectableEvidenceGap") is True:
            violations.append("Sufficiency rubric failed: collectableEvidenceGap=true.")
        if conclusion == "ROOT_CAUSE_CONFIRMED":
            if rubric.get("multiSourceSupport") is not True and rubric.get("rootCauseSpecificEvidence") is not True:
                violations.append("Confirmed root cause requires either multiSourceSupport=true or rootCauseSpecificEvidence=true.")
            if rubric.get("rootCauseSupport") is not True:
                violations.append("Confirmed root cause requires rootCauseSupport=true.")
            violations.extend(self._string_list(rubric.get("missingCriticalEvidence")))
        elif conclusion == "PROBABLE_ROOT_CAUSE":
            if not result.get("rootCause"):
                violations.append("Probable root cause requires a useful rootCause candidate.")
            if rubric.get("sourceCoverage") is not True:
                violations.append("Probable root cause requires sourceCoverage=true.")
            if rubric.get("negativeEvidenceConsidered") is not True:
                violations.append("Probable root cause requires negativeEvidenceConsidered=true.")
        elif conclusion == "INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED":
            if rubric.get("sourceCoverage") is not True:
                violations.append("Unresolved final conclusion requires sourceCoverage=true.")
        else:
            violations.append(f"Unsupported conclusionType for SUFFICIENT result: {conclusion}")
        violations.extend(self._string_list(rubric.get("contradictions")))
        return self._dedupe(violations)

    @staticmethod
    def _infer_conclusion(status: str, root_cause: Any) -> str:
        if status != "SUFFICIENT":
            return "NEED_MORE_EVIDENCE" if status == "NEED_MORE_EVIDENCE" else ""
        return "PROBABLE_ROOT_CAUSE" if str(root_cause or "").strip() else "INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _object_list(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _parse_sufficiency(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return fallback
        keys = ("directEvidence", "multiSourceSupport", "sourceCoverage", "rootCauseSupport",
                "rootCauseSpecificEvidence", "negativeEvidenceConsidered", "collectableEvidenceGap",
                "temporalAlignment", "entityAlignment", "runbookSupport", "noContradiction",
                "missingCriticalEvidence", "contradictions", "rationale")
        return {key: value.get(key, fallback.get(key)) for key in keys}


class ToolGovernance:
    def __init__(self, store: Store, disabled_tools: set[str] | None = None, max_repeat: int = 3):
        self.store = store
        self.disabled_tools = disabled_tools or set()
        self.max_repeat = max_repeat

    async def decide(self, diagnosis_id: str, tool: str, trace: list[dict[str, Any]]) -> dict[str, Any]:
        repeats = sum(item.get("tool") == tool for item in trace)
        allowed = tool not in self.disabled_tools and repeats < self.max_repeat
        reason = "allowed" if allowed else ("tool disabled by policy" if tool in self.disabled_tools else "repeat limit exceeded")
        decision = {"decisionId": f"gov-{uuid.uuid4()}", "diagnosisId": diagnosis_id, "tool": tool,
                    "allowed": allowed, "reason": reason, "repeatCount": repeats, "maxRepeat": self.max_repeat,
                    "createTime": now_iso()}
        await self.store.put("audit_logs", decision["decisionId"], decision, decision["createTime"])
        return decision


class HistoricalMemoryService:
    def __init__(self, store: Store):
        self.store = store

    async def recall(self, service: str, problem: str, limit: int = 5) -> list[dict[str, Any]]:
        query_terms = set(re.findall(r"[a-zA-Z0-9_.-]{3,}", problem.lower()))
        memories = await self.store.find("memories", lambda item: item.get("serviceName") == service, 500)
        for memory in memories:
            text = json.dumps(memory, ensure_ascii=False).lower()
            memory["similarityScore"] = len(query_terms & set(re.findall(r"[a-zA-Z0-9_.-]{3,}", text)))
        return sorted(memories, key=lambda item: (-item["similarityScore"], item.get("updateTime", "")))[:limit]

    async def remember(self, diagnosis: dict[str, Any], category: str, confidence: int) -> dict[str, Any] | None:
        if diagnosis.get("status") != "SUCCESS" or confidence < 60:
            return None
        memory = {"memoryId": f"memory-{uuid.uuid4()}", "diagnosisId": diagnosis.get("diagnosisId"),
                  "serviceName": diagnosis.get("serviceName"), "problem": diagnosis.get("problem"),
                  "category": category, "confidence": confidence, "report": diagnosis.get("report", "")[:12000],
                  "createTime": now_iso(), "updateTime": now_iso()}
        await self.store.put("memories", memory["memoryId"], memory, memory["updateTime"])
        return memory


class AlertNormalizer:
    def normalize(self, webhook: AlertmanagerWebhook) -> list[dict[str, Any]]:
        return [self._normalize_alert(webhook, alert) for alert in (webhook.alerts or [])]

    def _normalize_alert(self, webhook: AlertmanagerWebhook, alert: Alert) -> dict[str, Any]:
        labels = {**(webhook.commonLabels or {}), **(alert.labels or {})}
        annotations = {**(webhook.commonAnnotations or {}), **(alert.annotations or {})}
        service = (labels.get("serviceName") or labels.get("service") or labels.get("application")
                   or labels.get("app") or labels.get("job") or annotations.get("serviceName")
                   or annotations.get("service") or "unknown-service").strip()
        alert_name = (labels.get("alertname") or labels.get("rule") or "unknown-rule").strip()
        raw_severity = labels.get("severity") or labels.get("level") or annotations.get("severity") or "P2"
        normalized = raw_severity.upper()
        severity = normalized if normalized.startswith("P") else {
            "CRITICAL": "P1", "FATAL": "P1", "EMERGENCY": "P1", "WARNING": "P2", "WARN": "P2",
            "ERROR": "P2", "HIGH": "P2", "INFO": "P3", "NOTICE": "P3", "MEDIUM": "P3",
        }.get(normalized, normalized)
        raw_status = (alert.status or webhook.status or "firing").strip().lower()
        status = "resolved" if raw_status == "resolved" else "firing"
        raw_payload = webhook.model_dump(by_alias=True, exclude_none=False)
        return {"alertId": f"alert-{uuid.uuid4()}", "fingerprint": alert.fingerprint, "groupKey": webhook.groupKey or "",
                "source": "alertmanager", "serviceName": service, "alertName": alert_name,
                "alertRule": alert_name, "severity": severity, "status": status,
                "summary": annotations.get("summary") or annotations.get("description") or alert_name,
                "description": annotations.get("description", ""), "endpoint": labels.get("endpoint", ""),
                "traceId": labels.get("trace_id") or labels.get("traceId") or "", "labels": labels,
                "annotations": annotations, "startsAt": self._parse_time(alert.startsAt),
                "endsAt": self._parse_time(alert.endsAt), "rawPayload": json.dumps(raw_payload, ensure_ascii=False),
                "generatorURL": alert.generatorURL, "receivedTime": now_iso(),
                "createTime": now_iso(), "updateTime": now_iso()}

    @staticmethod
    def _parse_time(value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.year < 2000:
                return None
            if parsed.tzinfo:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed.isoformat()
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return parsed.isoformat() if parsed.year >= 2000 else None
            except ValueError:
                return None


class AlertDeduplicator:
    def __init__(self, store: Store, window_minutes: int = 5):
        self.store = store
        self.window = timedelta(minutes=window_minutes)

    async def accept(self, alert: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now()
        await self.store.put("alerts", alert["alertId"], alert, alert["updateTime"])
        dedup_key = "|".join(str(alert.get(key) or "").strip() for key in (
            "serviceName", "alertName", "fingerprint", "severity")).lower()
        reason = ""
        if not str(alert.get("serviceName") or "").strip() or str(alert.get("serviceName")).lower() == "unknown-service":
            reason = "serviceName is missing"
        elif str(alert.get("status") or "").lower() != "firing":
            reason = "alert status is not firing"
        else:
            running = await self.store.find("dispatches", lambda item: item.get("serviceName") == alert.get("serviceName")
                                            and item.get("dispatchStatus") in {"NEW", "RUNNING"}, 500)
            if running:
                reason = "service already has running diagnosis"
            else:
                recent = await self.store.find("dispatches", lambda item: item.get("dedupKey") == dedup_key, 500)
                latest = max(recent, key=lambda item: item.get("createTime", ""), default=None)
                if latest and self._within(latest.get("createTime"), now):
                    reason = f"duplicated alert within {max(1, int(self.window.total_seconds() / 60))} minutes"
        return {"accepted": not reason, "reason": reason, "dedupKey": dedup_key, "alert": alert}

    def _within(self, value: str | None, now: datetime) -> bool:
        try:
            parsed = datetime.fromisoformat(value or "")
            if parsed.tzinfo:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return now - parsed <= self.window
        except ValueError:
            return False


class ServiceOwnerService:
    def __init__(self, store: Store):
        self.store = store

    async def query(self, service_name: str) -> dict[str, Any] | None:
        legacy = await self.store.query_legacy_service_owner(service_name)
        if legacy and legacy.get("enabled"):
            return legacy
        direct = await self.store.get("service_owners", service_name)
        if direct:
            return direct
        matches = await self.store.find("service_owners", lambda item: item.get("serviceName") == service_name, 1)
        return matches[0] if matches else None


class NotificationTemplateService:
    def __init__(self, subject_prefix: str = "[AutoAgent]", app_base_url: str = "http://127.0.0.1:8099"):
        self.subject_prefix, self.app_base_url = subject_prefix, app_base_url.rstrip("/")

    def build(self, alert: dict[str, Any], command: dict[str, Any], diagnosis: dict[str, Any] | None) -> tuple[str, str]:
        service = alert.get("serviceName") or "unknown-service"
        severity = alert.get("severity") or "P2"
        subject = f"{self.subject_prefix} [{severity}] {service} 自动诊断结果"
        lines = [f"服务名称: {service}", f"告警规则: {alert.get('alertName') or alert.get('alertRule') or 'UNKNOWN_ALERT'}",
                 f"严重级别: {severity}", f"诊断ID: {command.get('diagnosisId', '-')}",
                 f"会话ID: {command.get('sessionId', '-')}",
                 f"时间窗口: {command.get('startTime', '-')} ~ {command.get('endTime', '-')}",
                 f"问题描述: {command.get('problem', '-')}"]
        if diagnosis:
            lines.append(f"诊断状态: {diagnosis.get('status', 'UNKNOWN')}")
            if diagnosis.get("errorMessage"):
                lines.append(f"失败原因: {diagnosis['errorMessage']}")
            if diagnosis.get("report"):
                lines.extend(["", "诊断报告摘要:", str(diagnosis["report"])[:4000]])
        else:
            lines.extend(["诊断状态: UNKNOWN", "说明: AutoAgent 已触发，但暂未查询到诊断落库记录。"])
        lines.extend(["", f"诊断记录查询地址: {self.app_base_url}/api/v1/ops/incident/record/{command.get('diagnosisId', '-')}"])
        return subject, "\n".join(lines) + "\n"


class NotificationService:
    def __init__(self, store: Store, host: str = "", port: int = 25, username: str = "", password: str = "",
                 auth: bool = True, starttls: bool = True, timeout_seconds: float = 10.0):
        self.store, self.host, self.port, self.username, self.password = store, host, port, username, password
        self.auth, self.starttls, self.timeout_seconds = auth, starttls, timeout_seconds

    async def send(self, recipients: list[str], subject: str, body: str, diagnosis_id: str,
                   *, service_name: str = "unknown-service", severity: str = "") -> dict[str, Any]:
        record = {"notificationId": f"notify-{uuid.uuid4()}", "diagnosisId": diagnosis_id, "recipients": recipients,
                  "serviceName": service_name, "channel": "EMAIL", "receiver": ",".join(recipients),
                  "severity": severity, "subject": subject, "status": "PENDING", "sendStatus": "PENDING",
                  "retryCount": 0, "errorMessage": "", "createTime": now_iso(), "updateTime": now_iso()}
        if not self.host or not recipients:
            record.update(status="SKIPPED", sendStatus="SKIPPED", errorMessage="SMTP host or recipient is not configured", updateTime=now_iso())
        else:
            try:
                await asyncio.to_thread(self._smtp_send, recipients, subject, body)
                record.update(status="SUCCESS", sendStatus="SUCCESS", sendTime=now_iso(), updateTime=now_iso())
            except Exception as exc:
                record.update(status="FAILED", sendStatus="FAILED", errorMessage=str(exc)[:1000], sendTime=now_iso(), updateTime=now_iso())
        await self.store.put("notifications", record["notificationId"], record, record["updateTime"])
        return record

    async def skipped(self, diagnosis_id: str, service_name: str, severity: str, reason: str) -> dict[str, Any]:
        record = {"notificationId": f"notify-{uuid.uuid4()}", "diagnosisId": diagnosis_id,
                  "serviceName": service_name, "channel": "EMAIL", "receiver": "", "recipients": [],
                  "severity": severity, "subject": "", "status": "SKIPPED", "sendStatus": "SKIPPED",
                  "retryCount": 0, "errorMessage": reason, "sendTime": now_iso(),
                  "createTime": now_iso(), "updateTime": now_iso()}
        await self.store.put("notifications", record["notificationId"], record, record["updateTime"])
        return record

    def _smtp_send(self, recipients: list[str], subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, self.username or "ops-autoagent@localhost", ", ".join(recipients)
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.auth and self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
