from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.eventing import AIOpsEventType, EventEnvelope
from ops_autoagent.incident_aggregation import IncidentAggregationCoordinator
from ops_autoagent.schemas import now_iso
from ops_autoagent.store import Store


def _alert(fingerprint: str, severity: str = "P2", instance: str = "order-1") -> dict:
    return {
        "fingerprint": fingerprint,
        "serviceName": "order-service",
        "alertName": "OrderErrorRate",
        "endpoint": "/orders/submit",
        "severity": severity,
        "summary": "5xx rate exceeds threshold",
        "labels": {"environment": "production", "instance": instance},
    }


@pytest.mark.asyncio
async def test_fingerprint_dedup_and_incident_aggregation_are_separate(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(
        store, Settings(redis_url="", ops_alert_dedup_window_minutes=10, ops_incident_aggregation_window_seconds=30)
    )

    first = _alert("same-alert", "P2", "order-1")
    second = _alert("different-instance", "P1", "order-2")
    assert await coordinator.claim_fingerprint(first)
    assert not await coordinator.claim_fingerprint(first)
    assert await coordinator.claim_fingerprint(second)

    one = await coordinator.aggregate(first, "incident-one")
    two = await coordinator.aggregate(second, "incident-two")
    assert one["isNew"] is True
    assert two["isNew"] is False
    assert two["incidentId"] == "incident-one"
    assert two["alertCount"] == 2
    assert two["highestSeverity"] == "P1"
    assert set(two["fingerprints"]) == {"same-alert", "different-instance"}
    assert set(two["instances"]) == {"order-1", "order-2"}


@pytest.mark.asyncio
async def test_aggregation_window_emits_exactly_one_incident_event(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    aggregate = await coordinator.aggregate(_alert("one"), "incident-one")
    incident = {**aggregate, "status": "COLLECTING", "source": "alertmanager",
                "createTime": now_iso(), "updateTime": now_iso(),
                "aggregateUntil": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    await store.put("incidents", incident["incidentId"], incident, incident["updateTime"])

    await coordinator.publish_due_incidents()
    await coordinator.publish_due_incidents()

    persisted = await store.get("incidents", "incident-one")
    outbox = await store.recent("outbox_events", 10)
    assert persisted["status"] == "READY"
    assert len(outbox) == 1
    envelope = EventEnvelope.model_validate(outbox[0]["envelope"])
    assert envelope.event_type == AIOpsEventType.INCIDENT
    assert envelope.payload["incident"]["incidentId"] == "incident-one"


@pytest.mark.asyncio
async def test_production_aggregation_requires_redis(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    with pytest.raises(RuntimeError, match="Redis is required"):
        await coordinator.start(required=True)
