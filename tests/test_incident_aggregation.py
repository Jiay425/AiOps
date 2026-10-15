import asyncio
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
    assert not await coordinator.claim_fingerprint(_alert("changed-fingerprint", "P2", "order-1"))
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
async def test_periodic_tick_groups_fresh_alerts_without_per_group_wait(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url="", ops_incident_aggregation_window_seconds=60))
    first, second = _alert("one", "P2", "order-1"), _alert("two", "P1", "order-2")
    first_seen = now_iso()
    key = coordinator.incident_key(first)
    for index, alert in enumerate((first, second)):
        alert.update(alertId=f"alert-{index}", incidentKey=key, ingressStatus="PENDING_AGGREGATION",
                     createTime=first_seen, updateTime=first_seen)
        await store.put("alert_events", alert["alertId"], alert, first_seen)

    await coordinator.publish_due_incidents()
    await coordinator.publish_due_incidents()
    incidents = await store.recent("incidents", 10)
    assert len(incidents) == 1
    assert incidents[0]["status"] == "READY" and incidents[0]["alertCount"] == 2
    assert incidents[0]["severity"] == "P1"
    assert incidents[0]["instances"] == ["order-1", "order-2"]
    assert incidents[0]["aggregationMode"] == "PERIODIC_BATCH"
    assert len(await store.recent("outbox_events", 10)) == 1
    events = await store.recent("alert_events", 10)
    assert {item["incidentId"] for item in events} == {incidents[0]["incidentId"]}


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


async def _pending(store: Store, coordinator: IncidentAggregationCoordinator, alert_id: str,
                   service: str = "order-service") -> dict:
    alert = {**_alert(alert_id, instance=alert_id), "serviceName": service, "alertId": alert_id,
             "ingressStatus": "PENDING_AGGREGATION", "createTime": now_iso(), "updateTime": now_iso()}
    alert["incidentKey"] = coordinator.incident_key(alert)
    await store.put("alert_events", alert_id, alert, alert["updateTime"])
    return alert


@pytest.mark.asyncio
async def test_tick_handles_multiple_groups_and_leaves_new_arrivals_for_next_tick(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    await _pending(store, coordinator, "order-1")
    await _pending(store, coordinator, "payment-1", "payment-service")
    original = coordinator._flush_group
    inserted = False

    async def flush_with_arrival(key, alerts):
        nonlocal inserted
        if not inserted:
            inserted = True
            await _pending(store, coordinator, "order-late")
        await original(key, alerts)

    monkeypatch.setattr(coordinator, "_flush_group", flush_with_arrival)
    await coordinator.publish_due_incidents()
    assert len(await store.recent("incidents", 10)) == 2
    assert [a["alertId"] for a in await store.pending_alert_events()] == ["order-late"]
    await coordinator.publish_due_incidents()
    assert len(await store.recent("incidents", 10)) == 3
    assert await store.pending_alert_events() == []


@pytest.mark.asyncio
async def test_database_conditional_claim_prevents_duplicate_batches_without_redis_lock(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    first = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    second = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    alert = await _pending(store, first, "order-1")
    key = alert["incidentKey"]
    await asyncio.gather(first._flush_group(key, [alert]), second._flush_group(key, [alert]))
    assert len(await store.recent("incidents", 10)) == 1
    assert len(await store.recent("outbox_events", 10)) == 1
    assert await store.pending_alert_events() == []


@pytest.mark.asyncio
async def test_incident_outbox_and_alert_status_roll_back_together(tmp_path: Path):
    import aiosqlite

    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    await _pending(store, coordinator, "order-1")
    async with aiosqlite.connect(store.path) as db:
        await db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox_events "
                         "BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END")
        await db.commit()
    await coordinator.publish_due_incidents()
    assert len(await store.pending_alert_events()) == 1
    assert await store.recent("incidents", 10) == []
    assert await store.recent("outbox_events", 10) == []
    async with aiosqlite.connect(store.path) as db:
        await db.execute("DROP TRIGGER fail_outbox")
        await db.commit()
    await coordinator.publish_due_incidents()
    assert await store.pending_alert_events() == []
    assert len(await store.recent("incidents", 10)) == 1
    assert len(await store.recent("outbox_events", 10)) == 1


@pytest.mark.asyncio
async def test_timer_waits_for_interval_and_survives_a_failed_tick(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    assert coordinator._aggregate_seconds() == 60
    coordinator.settings.ops_incident_aggregation_interval_seconds = 1
    completed = asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database outage")
        completed.set()

    monkeypatch.setattr(coordinator, "publish_due_incidents", tick)
    await coordinator.start()
    try:
        await asyncio.sleep(0.05)
        assert calls == 0
        await asyncio.wait_for(completed.wait(), timeout=4)
        assert calls == 2
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_durable_dedup_receipt_survives_lost_redis_state_and_concurrent_claims(tmp_path: Path):
    store = Store(tmp_path / "aggregation.db")
    await store.initialize()
    coordinator = IncidentAggregationCoordinator(store, Settings(redis_url=""))
    first = {**_alert("one"), "alertId": "alert-1", "ingressStatus": "PENDING_AGGREGATION",
             "createTime": now_iso(), "updateTime": now_iso()}
    second = {**first, "alertId": "alert-2"}
    results = await asyncio.gather(store.accept_alert("fingerprint-one", first, 600),
                                   store.accept_alert("fingerprint-one", second, 600))
    assert sorted(item[0] for item in results) == [False, True]
    assert len(await store.recent("alert_events", 10)) == 1
    # Redis can be empty after a restart; the SQL receipt still prevents a new
    # alert row for a redelivery within the dedup interval.
    assert await coordinator.reserve_fingerprint(first) == "CLAIMED"
    assert await coordinator.reserve_fingerprint(second) == "BUSY"
    accepted, accepted_id = await store.accept_alert("fingerprint-one", second, 600)
    assert accepted is False
    await coordinator.confirm_fingerprint(first, accepted_id)
    assert await coordinator.reserve_fingerprint(second) == "DUPLICATE"


@pytest.mark.asyncio
async def test_expired_ingress_owner_cannot_release_a_new_claim(tmp_path: Path):
    coordinator = IncidentAggregationCoordinator(Store(tmp_path / "unused.db"), Settings(redis_url=""))
    first = {**_alert("one"), "alertId": "old"}
    second = {**first, "alertId": "new"}
    assert await coordinator.reserve_fingerprint(first) == "CLAIMED"
    key = coordinator.fingerprint_key(first)
    coordinator._memory_reservations[key] = ("pending:old", 0)
    assert await coordinator.reserve_fingerprint(second) == "CLAIMED"
    await coordinator.release_fingerprint(first, "pending:old")
    assert await coordinator.reserve_fingerprint(first) == "BUSY"
