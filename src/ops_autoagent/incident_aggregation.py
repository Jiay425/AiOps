"""Redis-backed Alertmanager de-duplication and incident aggregation.

Redis is the fast coordination layer.  MySQL/SQLite projections remain the
durable audit source and the Outbox is created only after the aggregation
window closes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .eventing import AIOpsEventType, EventEnvelope
from .outbox import OutboxRelay
from .schemas import now_iso
from .store import Store


class IncidentAggregationCoordinator:
    """Owns one Alertmanager ingress path: dedup -> aggregate -> Incident event."""

    _SEVERITY_SCORE = {"P1": 100, "CRITICAL": 100, "P2": 70, "HIGH": 70,
                       "P3": 40, "WARNING": 40, "MEDIUM": 40, "LOW": 10}

    _MERGE_LUA = """
local key = KEYS[1]
local now = ARGV[1]
local incident_id = ARGV[2]
local severity = ARGV[3]
local severity_score = tonumber(ARGV[4])
local endpoint = ARGV[5]
local fingerprint = ARGV[6]
local instance = ARGV[7]
local summary = ARGV[8]
local ttl = tonumber(ARGV[9])
local aggregate_until = ARGV[10]
local is_new = redis.call('EXISTS', key) == 0
if is_new then
  redis.call('HSET', key, 'incidentId', incident_id, 'firstSeen', now,
    'aggregateUntil', aggregate_until, 'highestSeverity', severity,
    'highestSeverityScore', severity_score, 'alertCount', 0)
end
redis.call('HINCRBY', key, 'alertCount', 1)
redis.call('HSET', key, 'lastSeen', now, 'latestSummary', summary)
local old_score = tonumber(redis.call('HGET', key, 'highestSeverityScore') or '0')
if severity_score > old_score then
  redis.call('HSET', key, 'highestSeverity', severity, 'highestSeverityScore', severity_score)
end
if endpoint ~= '' then redis.call('SADD', key .. ':endpoints', endpoint) end
if fingerprint ~= '' then redis.call('SADD', key .. ':fingerprints', fingerprint) end
if instance ~= '' then redis.call('SADD', key .. ':instances', instance) end
redis.call('EXPIRE', key, ttl)
redis.call('EXPIRE', key .. ':endpoints', ttl)
redis.call('EXPIRE', key .. ':fingerprints', ttl)
redis.call('EXPIRE', key .. ':instances', ttl)
return {is_new and '1' or '0', redis.call('HGET', key, 'alertCount'),
  redis.call('HGET', key, 'highestSeverity'), redis.call('HGET', key, 'firstSeen'),
  redis.call('HGET', key, 'lastSeen'), redis.call('HGET', key, 'aggregateUntil')}
"""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.redis: Any | None = None
        self._memory_dedup: dict[str, float] = {}
        self._memory_incidents: dict[str, dict[str, Any]] = {}
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, *, required: bool = False) -> None:
        url = str(self.settings.redis_url or "").strip()
        if required and not url:
            raise RuntimeError("Redis is required in production for Alertmanager fingerprint deduplication and Incident aggregation")
        if url:
            import redis.asyncio as redis
            self.redis = redis.from_url(url, decode_responses=True)
            await self.redis.ping()
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="incident-aggregation-coordinator")

    async def close(self) -> None:
        self._stopped.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None
        self._memory_dedup.clear()
        self._memory_incidents.clear()

    @staticmethod
    def fingerprint_key(alert: dict[str, Any]) -> str:
        fingerprint = str(alert.get("fingerprint") or "").strip()
        if not fingerprint:
            fingerprint = "|".join(str(alert.get(key) or "") for key in
                                   ("serviceName", "alertName", "endpoint", "instance", "summary"))
        return "aiops:alert:dedup:" + hashlib.sha256(fingerprint.encode()).hexdigest()

    @staticmethod
    def incident_key(alert: dict[str, Any]) -> str:
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        environment = str(labels.get("environment") or labels.get("env") or labels.get("namespace") or "default").strip()
        endpoint = str(alert.get("endpoint") or "").strip() or "*"
        value = "|".join((environment, str(alert.get("serviceName") or "").strip(),
                          str(alert.get("alertName") or "").strip(), endpoint)).lower()
        return hashlib.sha256(value.encode()).hexdigest()

    async def claim_fingerprint(self, alert: dict[str, Any]) -> bool:
        key, ttl = self.fingerprint_key(alert), self._dedup_seconds()
        if self.redis is not None:
            return bool(await self.redis.set(key, now_iso(), nx=True, ex=ttl))
        now = asyncio.get_running_loop().time()
        expires = self._memory_dedup.get(key, 0)
        if expires > now:
            return False
        self._memory_dedup[key] = now + ttl
        return True

    async def release_fingerprint(self, alert: dict[str, Any]) -> None:
        key = self.fingerprint_key(alert)
        if self.redis is not None:
            await self.redis.delete(key)
        else:
            self._memory_dedup.pop(key, None)

    async def aggregate(self, alert: dict[str, Any], incident_id: str) -> dict[str, Any]:
        key, now = self.incident_key(alert), now_iso()
        aggregate_until = (datetime.now(timezone.utc) + timedelta(seconds=self._aggregate_seconds())).isoformat()
        severity = str(alert.get("severity") or "P3").upper()
        score = self._SEVERITY_SCORE.get(severity, 10)
        endpoint = str(alert.get("endpoint") or "")
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        instance = str(labels.get("instance") or labels.get("pod") or "")
        fingerprint = str(alert.get("fingerprint") or "")
        if self.redis is not None:
            raw = await self.redis.eval(self._MERGE_LUA, 1, "aiops:incident:" + key, now, incident_id, severity,
                                        score, endpoint, fingerprint, instance, str(alert.get("summary") or ""),
                                        self._incident_ttl_seconds(), aggregate_until)
            return await self._redis_snapshot(key, bool(int(raw[0])), incident_id)
        item = self._memory_incidents.setdefault(key, {
            "incidentId": incident_id, "firstSeen": now, "aggregateUntil": aggregate_until,
            "highestSeverity": severity, "highestSeverityScore": score, "alertCount": 0,
            "fingerprints": set(), "instances": set(), "affectedEndpoints": set(),
        })
        is_new = item["alertCount"] == 0
        item["alertCount"] += 1
        item["lastSeen"], item["latestSummary"] = now, str(alert.get("summary") or "")
        if score > int(item["highestSeverityScore"]):
            item["highestSeverity"], item["highestSeverityScore"] = severity, score
        item["fingerprints"].add(fingerprint)
        if instance:
            item["instances"].add(instance)
        if endpoint:
            item["affectedEndpoints"].add(endpoint)
        return self._memory_snapshot(key, item, is_new)

    async def snapshot(self, incident_key: str) -> dict[str, Any] | None:
        if self.redis is not None:
            data = await self._redis_snapshot(incident_key, False, "")
            return data if data.get("incidentId") else None
        item = self._memory_incidents.get(incident_key)
        return self._memory_snapshot(incident_key, item, False) if item else None

    async def _redis_snapshot(self, key: str, is_new: bool, fallback_id: str) -> dict[str, Any]:
        assert self.redis is not None
        base = await self.redis.hgetall("aiops:incident:" + key)
        return {
            "incidentKey": key, "incidentId": base.get("incidentId") or fallback_id, "isNew": is_new,
            "firstSeen": base.get("firstSeen", ""), "lastSeen": base.get("lastSeen", ""),
            "aggregateUntil": base.get("aggregateUntil", ""), "alertCount": int(base.get("alertCount", 0) or 0),
            "highestSeverity": base.get("highestSeverity", "P3"),
            "fingerprints": sorted(await self.redis.smembers("aiops:incident:" + key + ":fingerprints")),
            "instances": sorted(await self.redis.smembers("aiops:incident:" + key + ":instances")),
            "affectedEndpoints": sorted(await self.redis.smembers("aiops:incident:" + key + ":endpoints")),
            "latestSummary": base.get("latestSummary", ""),
        }

    @staticmethod
    def _memory_snapshot(key: str, item: dict[str, Any], is_new: bool) -> dict[str, Any]:
        return {**{name: value for name, value in item.items() if name not in {"fingerprints", "instances", "affectedEndpoints"}},
                "incidentKey": key, "isNew": is_new, "fingerprints": sorted(item["fingerprints"]),
                "instances": sorted(item["instances"]), "affectedEndpoints": sorted(item["affectedEndpoints"])}

    async def _run(self) -> None:
        while not self._stopped.is_set():
            await self.publish_due_incidents()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def publish_due_incidents(self) -> None:
        now = datetime.now(timezone.utc)
        incidents = await self.store.find("incidents", lambda item: item.get("status") == "COLLECTING", 1000)
        for incident in incidents:
            try:
                due = datetime.fromisoformat(str(incident.get("aggregateUntil") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if due > now:
                continue
            snapshot = await self.snapshot(str(incident.get("incidentKey") or ""))
            if snapshot:
                incident.update({key: value for key, value in snapshot.items() if key != "isNew"})
            incident.update(status="READY", updateTime=now_iso())
            envelope = EventEnvelope(eventId=f"evt-incident-ready:{incident['incidentId']}",
                                     eventType=AIOpsEventType.INCIDENT, incidentId=incident["incidentId"],
                                     correlationId=incident["incidentId"],
                                     idempotencyKey=f"incident-ready:{incident['incidentId']}",
                                     payload={"incident": incident})
            outbox = OutboxRelay.pending_record(envelope)
            await self.store.put_many([
                ("incidents", incident["incidentId"], incident, incident["updateTime"]),
                ("outbox_events", outbox["outboxId"], outbox, outbox["updateTime"]),
            ])

    def _dedup_seconds(self) -> int:
        return max(60, int(self.settings.ops_alert_dedup_window_minutes) * 60)

    def _aggregate_seconds(self) -> int:
        return max(30, min(60, int(getattr(self.settings, "ops_incident_aggregation_window_seconds", 60))))

    def _incident_ttl_seconds(self) -> int:
        return max(self._dedup_seconds() * 2, self._aggregate_seconds() * 4)
