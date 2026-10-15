import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.eventing import AIOpsEventType, EventEnvelope, KafkaConsumerWorker, serialize_envelope
from ops_autoagent.outbox import OutboxRelay, consume_once
from ops_autoagent.store import Store


def test_event_envelope_is_versioned_and_idempotent():
    envelope = EventEnvelope.from_task_event({"eventId": "event-1", "taskId": "task-1", "runId": "run-1"})
    payload = serialize_envelope(envelope)
    assert payload["eventType"] == AIOpsEventType.AUDIT
    assert payload["schemaVersion"] == 1
    assert payload["idempotencyKey"] == "aiops.audit:event-1"


@pytest.mark.asyncio
async def test_outbox_and_consumer_deduplication_are_durable(tmp_path: Path):
    store = Store(tmp_path / "outbox.db")
    await store.initialize()
    envelope = EventEnvelope(eventType=AIOpsEventType.EVIDENCE, idempotencyKey="task-1:evidence-1", taskId="task-1")
    record = OutboxRelay.pending_record(envelope)
    await store.put_many([("outbox_events", record["outboxId"], record, record["updateTime"])])
    assert (await store.get("outbox_events", envelope.event_id))["status"] == "PENDING"
    handled: list[str] = []

    async def handler(value):
        handled.append(value.event_id)

    assert await consume_once(store, envelope, handler) is True
    assert await consume_once(store, envelope, handler) is False
    assert handled == [envelope.event_id]


@pytest.mark.asyncio
async def test_only_one_consumer_claims_and_expired_claim_can_be_recovered(tmp_path: Path):
    store = Store(tmp_path / "claims.db")
    await store.initialize()
    claims = await asyncio.gather(*(
        store.claim_processed_event("incident-ready:one", "event-one") for _ in range(2)))
    assert sorted(result[0] for result in claims) == ["BUSY", "CLAIMED"]
    claimed_owner = next(owner for status, owner in claims if status == "CLAIMED")
    record = await store.get("processed_events", "incident-ready:one")
    assert record["owner"] == claimed_owner and record["status"] == "RECEIVED"

    record["leaseUntil"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await store.put("processed_events", "incident-ready:one", record, record["leaseUntil"])
    status, recovered_owner = await store.claim_processed_event("incident-ready:one", "event-one")
    assert status == "CLAIMED" and recovered_owner != claimed_owner
    assert await store.claim_processed_event("incident-ready:one", "event-one") == ("BUSY", None)


@pytest.mark.asyncio
async def test_incident_queue_and_consumer_receipt_commit_together(tmp_path: Path):
    store = Store(tmp_path / "handoff.db")
    await store.initialize()
    key = "incident-ready:one"
    status, owner = await store.claim_processed_event(key, "event-one")
    assert status == "CLAIMED" and owner
    incident = {"incidentId": "one", "status": "QUEUED", "updateTime": datetime.now(timezone.utc).isoformat()}
    with pytest.raises(RuntimeError, match="ownership was lost"):
        await store.complete_incident_handoff(key, "wrong-owner", incident)
    assert await store.get("incidents", "one") is None
    assert (await store.get("processed_events", key))["status"] == "RECEIVED"

    await store.complete_incident_handoff(key, owner, incident)
    assert (await store.get("processed_events", key))["status"] == "QUEUED"
    assert (await store.queued_incidents())[0]["incidentId"] == "one"
    assert await store.claim_processed_event(key, "event-one") == ("QUEUED", None)
    running = {**incident, "status": "RUNNING"}
    assert sorted(await asyncio.gather(*(store.claim_queued_incident(running) for _ in range(2)))) == [False, True]


@pytest.mark.asyncio
async def test_consumer_does_not_commit_when_dead_letter_publish_fails(tmp_path: Path):
    store = Store(tmp_path / "consumer.db")
    await store.initialize()
    settings = Settings(kafka_consumer_max_attempts=1, kafka_consumer_poll_timeout_ms=50)

    class Message:
        def topic(self):
            return "aiops.incidents.v1"

        def partition(self):
            return 0

        def offset(self):
            return 7

        def value(self):
            return b"not valid JSON"

        def error(self):
            return None

    class Consumer:
        commits = 0
        seeks = 0

        def poll(self, timeout):
            return Message()

        def seek(self, position):
            self.seeks += 1

        def commit(self, **kwargs):
            self.commits += 1
            worker._stopped.set()

    class Publisher:
        attempts = 0

        async def publish(self, topic, envelope):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("DLQ unavailable")

    publisher, consumer = Publisher(), Consumer()
    worker = KafkaConsumerWorker(settings, store, publisher, lambda _: None)
    worker._consumer = consumer
    await asyncio.wait_for(worker._run(), timeout=5)
    assert publisher.attempts == 2
    assert consumer.seeks == 1
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_outbox_finds_old_pending_rows_behind_many_published_rows(tmp_path: Path):
    store = Store(tmp_path / "outbox.db")
    await store.initialize()
    envelope = EventEnvelope(eventType=AIOpsEventType.INCIDENT, idempotencyKey="incident-ready:old",
                             incidentId="old")
    pending = OutboxRelay.pending_record(envelope)
    await store.put("outbox_events", pending["outboxId"], pending, "2026-01-01T00:00:00Z")
    for index in range(75):
        record = {**pending, "outboxId": f"published-{index}", "status": "PUBLISHED"}
        await store.put("outbox_events", record["outboxId"], record, "2026-01-02T00:00:00Z")
    assert [r["outboxId"] for r in await store.pending_outbox_events(50)] == [pending["outboxId"]]


@pytest.mark.asyncio
async def test_outbox_retries_scan_and_ack_before_database_update_failure(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "outbox.db")
    await store.initialize()
    envelope = EventEnvelope(eventType=AIOpsEventType.INCIDENT, idempotencyKey="incident-ready:retry",
                             incidentId="retry")
    record = OutboxRelay.pending_record(envelope)
    await store.put("outbox_events", record["outboxId"], record, record["updateTime"])
    sent: list[EventEnvelope] = []

    class Publisher:
        async def publish(self, topic, message):
            sent.append(message)
            if len(sent) == 2:
                relay._stopped.set()

    relay = OutboxRelay(store, Publisher(), Settings(kafka_outbox_poll_interval_ms=50))
    scan, put = store.pending_outbox_events, store.put
    scans, writes = 0, 0

    async def flaky_scan(*args):
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("database offline")
        return await scan(*args)

    async def flaky_put(*args):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("database offline after Kafka ack")
        return await put(*args)

    monkeypatch.setattr(store, "pending_outbox_events", flaky_scan)
    monkeypatch.setattr(store, "put", flaky_put)
    await asyncio.wait_for(relay._run(), timeout=3)
    assert len(sent) == 2
    assert sent[0].event_id == sent[1].event_id == envelope.event_id
    assert sent[0].idempotency_key == sent[1].idempotency_key
    assert (await store.get("outbox_events", record["outboxId"]))["status"] == "PUBLISHED"
