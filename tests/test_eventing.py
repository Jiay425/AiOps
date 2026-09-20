from pathlib import Path

import pytest

from ops_autoagent.eventing import AIOpsEventType, EventEnvelope, serialize_envelope
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
