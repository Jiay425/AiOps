"""Transactional Outbox relay and idempotent consumer bookkeeping."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .eventing import AIOpsEventType, EventEnvelope, KafkaPublisher, serialize_envelope, topic_for
from .schemas import now_iso
from .store import Store


class OutboxRelay:
    def __init__(self, store: Store, publisher: KafkaPublisher, settings: Any):
        self.store, self.publisher, self.settings = store, publisher, settings
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @staticmethod
    def pending_record(envelope: EventEnvelope) -> dict[str, Any]:
        return {"outboxId": envelope.event_id, "status": "PENDING", "attempts": 0,
                "topic": "", "envelope": serialize_envelope(envelope), "createTime": now_iso(), "updateTime": now_iso()}

    async def start(self) -> None:
        if self.publisher.configured and self._task is None:
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="kafka-outbox-relay")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        interval = max(50, int(getattr(self.settings, "kafka_outbox_poll_interval_ms", 500) or 500)) / 1000
        batch = max(1, int(getattr(self.settings, "kafka_outbox_batch_size", 50) or 50))
        while not self._stopped.is_set():
            for record in await self.store.find("outbox_events", lambda item: item.get("status") == "PENDING", batch):
                await self._deliver(record)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _deliver(self, record: dict[str, Any]) -> None:
        try:
            envelope = EventEnvelope.model_validate(record["envelope"])
            topic = topic_for(self.settings, envelope.event_type)
            await self.publisher.publish(topic, envelope)
            updated = {**record, "status": "PUBLISHED", "topic": topic, "publishedAt": now_iso(), "updateTime": now_iso()}
        except Exception as exc:
            attempts = int(record.get("attempts", 0)) + 1
            max_attempts = max(1, int(getattr(self.settings, "kafka_outbox_max_attempts", 8) or 8))
            updated = {**record, "attempts": attempts, "lastError": type(exc).__name__, "updateTime": now_iso()}
            if attempts >= max_attempts:
                try:
                    dlq = EventEnvelope(
                        eventType=AIOpsEventType.DEAD_LETTER,
                        idempotencyKey=f"outbox-dlq:{record['outboxId']}",
                        correlationId=str(record.get("outboxId")),
                        payload={"outboxId": record["outboxId"], "attempts": attempts,
                                 "errorType": type(exc).__name__, "envelope": record.get("envelope", {})},
                    )
                    dlq_topic = topic_for(self.settings, AIOpsEventType.DEAD_LETTER)
                    await self.publisher.publish(dlq_topic, dlq)
                    updated = {**updated, "status": "DEAD_LETTER", "topic": dlq_topic, "deadLetteredAt": now_iso()}
                except Exception:
                    # Preserve PENDING so an unavailable Kafka/DLQ does not lose
                    # the original command.  It will retry on the next relay tick.
                    pass
        await self.store.put("outbox_events", str(record["outboxId"]), updated, updated["updateTime"])


async def consume_once(store: Store, envelope: EventEnvelope,
                       handler: Callable[[EventEnvelope], Awaitable[None]]) -> bool:
    """Return False for a duplicate; only mark processed after handler success."""
    if await store.get("processed_events", envelope.idempotency_key):
        return False
    await handler(envelope)
    record = {"idempotencyKey": envelope.idempotency_key, "eventId": envelope.event_id,
              "eventType": envelope.event_type, "processedAt": now_iso()}
    await store.put("processed_events", envelope.idempotency_key, record, record["processedAt"])
    return True
