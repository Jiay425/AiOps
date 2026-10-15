"""Redis-backed Alertmanager de-duplication and incident aggregation.

Redis is the fast coordination layer.  MySQL/SQLite projections remain the
durable source. A periodic job aggregates a snapshot of pending alerts and
commits each Incident with its Outbox record.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .eventing import AIOpsEventType, EventEnvelope
from .outbox import OutboxRelay
from .schemas import now_iso
from .store import Store


logger = logging.getLogger(__name__)


class IncidentAggregationCoordinator:
    """Deduplicate on ingress, then aggregate pending records every 60 seconds."""

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
        self._memory_reservations: dict[str, tuple[str, float]] = {}
        self._memory_incidents: dict[str, dict[str, Any]] = {}
        self._flush_lock = asyncio.Lock()
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
        self._memory_reservations.clear()
        self._memory_incidents.clear()

    @staticmethod
    def fingerprint_key(alert: dict[str, Any]) -> str:
        """Compatibility name: the Redis key now identifies an alerting instance."""
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        environment = str(labels.get("environment") or labels.get("env") or labels.get("namespace") or "default").strip()
        # Without an instance label, retain the Alertmanager fingerprint to
        # avoid incorrectly collapsing different unidentified instances.
        instance = str(labels.get("instance") or labels.get("pod") or alert.get("instance")
                       or alert.get("fingerprint") or "unknown-instance").strip()
        value = "|".join((environment, instance, str(alert.get("serviceName") or "").strip(),
                          str(alert.get("alertName") or "").strip(),
                          str(alert.get("endpoint") or "").strip() or "*")).lower()
        return "aiops:alert:dedup:" + hashlib.sha256(value.encode()).hexdigest()

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

    async def reserve_fingerprint(self, alert: dict[str, Any]) -> str:
        """Short ingress lease; the long dedup TTL starts after SQL commit."""
        key, token = self.fingerprint_key(alert), "pending:" + str(alert["alertId"])
        if self.redis is not None:
            claimed = await self.redis.set(key, token, nx=True, ex=30)
            if claimed:
                return "CLAIMED"
            current = str(await self.redis.get(key) or "")
            return "BUSY" if not current or current.startswith("pending:") else "DUPLICATE"
        now = asyncio.get_running_loop().time()
        if self._memory_dedup.get(key, 0) > now:
            return "DUPLICATE"
        current = self._memory_reservations.get(key)
        if current and current[1] > now:
            return "BUSY" if current[0].startswith("pending:") else "DUPLICATE"
        self._memory_reservations[key] = (token, now + 30)
        return "CLAIMED"

    async def confirm_fingerprint(self, alert: dict[str, Any], accepted_id: str) -> None:
        key, token = self.fingerprint_key(alert), "pending:" + str(alert["alertId"])
        if self.redis is not None:
            await self.redis.eval(
                "if redis.call('GET',KEYS[1]) == ARGV[1] then "
                "return redis.call('SET',KEYS[1],ARGV[2],'EX',ARGV[3]) end return 0",
                1, key, token, "accepted:" + accepted_id, self._dedup_seconds())
        else:
            current = self._memory_reservations.get(key)
            if current and current[0] == token:
                self._memory_reservations[key] = (
                    "accepted:" + accepted_id, asyncio.get_running_loop().time() + self._dedup_seconds())

    async def release_fingerprint(self, alert: dict[str, Any], token: str | None = None) -> None:
        key = self.fingerprint_key(alert)
        if self.redis is not None:
            if token is None:
                await self.redis.delete(key)
            else:
                await self.redis.eval(
                    "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                    1, key, token)
        else:
            if token is None:
                self._memory_dedup.pop(key, None)
                self._memory_reservations.pop(key, None)
            elif (self._memory_reservations.get(key) or (None,))[0] == token:
                self._memory_reservations.pop(key, None)

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
        loop = asyncio.get_running_loop()
        interval = self._aggregate_seconds()
        next_run = loop.time() + interval
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=max(0, next_run - loop.time()))
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.publish_due_incidents()
            except Exception as exc:
                # Retain pending rows and keep the periodic job alive. Never
                # log raw alert payloads or connection strings.
                logger.warning("Incident aggregation tick failed (%s); retrying next tick", type(exc).__name__)
            next_run += interval
            if next_run <= loop.time():
                next_run = loop.time() + interval

    async def publish_due_incidents(self) -> None:
        await self._publish_buffered_alerts()
        now = datetime.now(timezone.utc)
        incidents = await self.store.find("incidents", lambda item: item.get("status") == "COLLECTING", 1000)
        for incident in incidents:
            try:
                due = self._utc_time(str(incident.get("aggregateUntil") or ""))
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

    async def _publish_buffered_alerts(self) -> None:
        if self._flush_lock.locked():
            return
        async with self._flush_lock:
            token, lock_key = str(uuid.uuid4()), "aiops:incident:flush:batch"
            if self.redis is not None and not await self.redis.set(lock_key, token, nx=True, ex=120):
                return
            try:
                # Freeze this tick's IDs once. New arrivals remain pending for
                # the next tick; no per-group age or deadline is maintained.
                pending = await self.store.pending_alert_events()
                groups: dict[str, list[dict[str, Any]]] = {}
                for alert in pending:
                    groups.setdefault(str(alert.get("incidentKey") or self.incident_key(alert)), []).append(alert)
                for key, alerts in groups.items():
                    try:
                        await self._flush_group(key, alerts)
                    except Exception as exc:
                        logger.warning("Incident batch group failed (%s); retained for next tick", type(exc).__name__)
            finally:
                if self.redis is not None:
                    await self.redis.eval(
                        "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
                        1, lock_key, token)

    async def _flush_group(self, key: str, alerts: list[dict[str, Any]]) -> None:
        alerts.sort(key=lambda item: str(item.get("createTime") or ""))
        first, latest = alerts[0], alerts[-1]
        labels = latest.get("labels") if isinstance(latest.get("labels"), dict) else {}
        annotations = latest.get("annotations") if isinstance(latest.get("annotations"), dict) else {}
        incident_id, updated = f"incident-{uuid.uuid4()}", now_iso()
        severity = max((str(item.get("severity") or "P3").upper() for item in alerts),
                       key=lambda value: self._SEVERITY_SCORE.get(value, 10))
        incident = {
            "incidentId": incident_id, "incidentKey": key, "status": "READY", "source": "alertmanager",
            "serviceName": latest.get("serviceName", ""), "alertName": latest.get("alertName", ""),
            "environment": labels.get("environment") or labels.get("env") or labels.get("namespace") or "default",
            "repository": (labels.get("repository") or labels.get("repo") or labels.get("code_repository")
                           or annotations.get("repository") or annotations.get("repo")
                           or annotations.get("code_repository") or ""),
            "severity": severity, "highestSeverity": severity,
            "summary": latest.get("summary", ""), "latestSummary": latest.get("summary", ""),
            "traceId": latest.get("traceId", ""), "alertCount": len(alerts),
            "firstSeen": first.get("createTime", ""), "lastSeen": latest.get("createTime", ""),
            "aggregationMode": "PERIODIC_BATCH", "aggregatedAt": updated,
            "aggregateUntil": updated,
            "instances": sorted({str((a.get("labels") or {}).get("instance") or (a.get("labels") or {}).get("pod"))
                                 for a in alerts if (a.get("labels") or {}).get("instance") or (a.get("labels") or {}).get("pod")}),
            "fingerprints": sorted({str(a.get("fingerprint")) for a in alerts if a.get("fingerprint")}),
            "affectedEndpoints": sorted({str(a.get("endpoint")) for a in alerts if a.get("endpoint")}),
            "createTime": updated, "updateTime": updated,
        }
        envelope = EventEnvelope(eventId=f"evt-incident-ready:{incident_id}",
                                 eventType=AIOpsEventType.INCIDENT, incidentId=incident_id,
                                 correlationId=incident_id, idempotencyKey=f"incident-ready:{incident_id}",
                                 payload={"incident": incident})
        outbox = OutboxRelay.pending_record(envelope)
        aggregated = [{**alert, "ingressStatus": "AGGREGATED", "incidentId": incident_id, "updateTime": updated}
                      for alert in alerts]
        await self.store.commit_alert_batch(incident, outbox, aggregated)

    @staticmethod
    def _utc_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Legacy now_iso() stores naive local time, not UTC.
        return parsed.astimezone(timezone.utc)

    def _dedup_seconds(self) -> int:
        return max(60, int(self.settings.ops_alert_dedup_window_minutes) * 60)

    def _aggregate_seconds(self) -> int:
        interval = getattr(self.settings, "ops_incident_aggregation_interval_seconds", None)
        if interval is None:
            interval = getattr(self.settings, "ops_incident_aggregation_window_seconds", 60)
        return max(1, int(interval))

    def _incident_ttl_seconds(self) -> int:
        return max(self._dedup_seconds() * 2, self._aggregate_seconds() * 4)
