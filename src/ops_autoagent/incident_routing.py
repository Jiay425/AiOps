"""Resolve an aggregated alert into a bounded, evidence-led CodeOps request."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .schemas import CodeOpsTaskRequest


class IncidentRepositoryUnmapped(ValueError):
    """No operator-approved repository mapping exists for this service."""


def resolve_incident_repository(incident: dict[str, Any], settings: Settings) -> str:
    service = str(incident.get("serviceName") or incident.get("service") or "").strip()
    try:
        mapping = json.loads(settings.ops_service_repositories_json or "{}")
    except (TypeError, ValueError) as exc:
        raise IncidentRepositoryUnmapped("OPS_SERVICE_REPOSITORIES_JSON is invalid") from exc
    if not isinstance(mapping, dict):
        raise IncidentRepositoryUnmapped("OPS_SERVICE_REPOSITORIES_JSON must be an object")
    configured = mapping.get(service)
    # Demo tasks may use an explicit alert label; the production-like local
    # chain requires an operator-owned mapping rather than trusting a webhook.
    if not configured and settings.ops_runtime_mode.strip().lower() != "production":
        configured = incident.get("repository")
    if not isinstance(configured, str) or not configured.strip():
        raise IncidentRepositoryUnmapped(f"No repository mapping for service {service or '<missing>'}")
    root = settings.ops_repository_root.resolve()
    candidate = (root / configured).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise IncidentRepositoryUnmapped(f"Repository for {service} is outside the allowed root") from exc
    if not candidate.is_dir():
        raise IncidentRepositoryUnmapped(f"Repository for {service} does not exist")
    return str(candidate)


def build_scheduled_incident_request(incident: dict[str, Any], repository: str) -> CodeOpsTaskRequest:
    service = str(incident.get("serviceName") or incident.get("service") or "unknown-service")
    severity = str(incident.get("highestSeverity") or incident.get("severity") or "UNKNOWN")
    summary = str(incident.get("latestSummary") or incident.get("summary") or "")
    endpoints = incident.get("affectedEndpoints") if isinstance(incident.get("affectedEndpoints"), list) else []
    first_seen = str(incident.get("firstSeen") or "")
    try:
        first = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        # Existing alert rows use naive host-local timestamps.  astimezone()
        # interprets those as local time; attaching UTC would shift the window.
        first = first.astimezone(timezone.utc)
    except ValueError:
        first = datetime.now(timezone.utc)
    start_time = (first - timedelta(minutes=10)).isoformat()
    end_time = datetime.now(timezone.utc).isoformat()
    endpoint_suffix = f" Affected endpoints: {', '.join(str(item) for item in endpoints)}" if endpoints else ""
    return CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX",
        goal=(f"{service} {incident.get('alertName', 'unknown')} severity={severity}. "
              f"Aggregated from {incident.get('alertCount', 0)} alerts. {summary}{endpoint_suffix}"),
        repository=repository,
        focusAreas=["incident", "code_location", "release_risk"],
        context={"serviceName": service, "severity": severity, "alertCount": incident.get("alertCount", 0),
                 "alertName": incident.get("alertName", "unknown"), "affectedEndpoints": endpoints,
                 "endpoint": str(endpoints[0]) if endpoints else "",
                 "traceId": str(incident.get("traceId") or ""),
                 "startTime": start_time, "endTime": end_time,
                 "source": "alertmanager", "scheduledBy": "IncidentScheduler", "autonomousTriage": True,
                 "incidentId": incident.get("incidentId"), "incidentKey": incident.get("incidentKey"),
                 "fingerprints": incident.get("fingerprints", []), "instances": incident.get("instances", []),
                 "evidenceMode": "LIVE", "fixtureFallbackAllowed": False,
                 "allowPatchApply": True, "allowTestPatchApply": True},
        maxRounds=8, maxToolCalls=50,
    )
