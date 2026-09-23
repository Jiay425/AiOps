"""Durable event contracts and Kafka transport for the CodeOps control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger(__name__)


class AIOpsEventType(StrEnum):
    INCIDENT = "aiops.incident"
    EVIDENCE = "aiops.evidence"
    REPAIR = "aiops.repair"
    REVIEW = "aiops.review"
    APPROVAL = "aiops.approval"
    AUDIT = "aiops.audit"
    DEAD_LETTER = "aiops.dead_letter"


class EventEnvelope(BaseModel):
    """Versioned, idempotent payload shared by Kafka and the durable Outbox."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4()}", alias="eventId")
    event_type: AIOpsEventType = Field(alias="eventType")
    schema_version: int = Field(default=1, alias="schemaVersion", ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), alias="occurredAt")
    producer: str = "ops-autoagent"
    task_id: str | None = Field(default=None, alias="taskId")
    incident_id: str | None = Field(default=None, alias="incidentId")
    trace_id: str | None = Field(default=None, alias="traceId")
    causation_id: str | None = Field(default=None, alias="causationId")
    correlation_id: str | None = Field(default=None, alias="correlationId")
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=191)
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_task_event(cls, task_event: dict[str, Any], *, event_type: AIOpsEventType = AIOpsEventType.AUDIT,
                        producer: str = "ops-autoagent") -> "EventEnvelope":
        event_id = str(task_event.get("eventId") or f"evt-{uuid.uuid4()}")
        task_id = str(task_event.get("taskId") or "") or None
        return cls(eventId=event_id, eventType=event_type, producer=producer, taskId=task_id,
                   traceId=str(task_event.get("runId") or "") or None,
                   correlationId=task_id, idempotencyKey=f"{event_type}:{event_id}", payload=task_event)


class KafkaPublisher:
    """Idempotent Kafka producer. Importing it does not connect or publish."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.enabled = bool(getattr(settings, "kafka_enabled", False))
        self.bootstrap_servers = str(getattr(settings, "kafka_bootstrap_servers", "") or "").strip()
        self.client_id = str(getattr(settings, "kafka_client_id", "ops-autoagent") or "ops-autoagent")
        self._producer: Any | None = None

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bootstrap_servers)

    async def start(self, *, required: bool = False) -> bool:
        if not self.configured:
            if required:
                raise RuntimeError("Kafka is required in production: configure KAFKA_ENABLED and KAFKA_BOOTSTRAP_SERVERS")
            return False
        from confluent_kafka import Producer
        self._producer = Producer({"bootstrap.servers": self.bootstrap_servers, "client.id": self.client_id,
                                   "enable.idempotence": True, "acks": "all", "retries": 8,
                                   "max.in.flight.requests.per.connection": 5})
        metadata = await asyncio.to_thread(self._producer.list_topics, timeout=10)
        if not metadata.brokers:
            raise RuntimeError("Kafka metadata lookup returned no brokers")
        if bool(getattr(self.settings, "kafka_topic_auto_provision", True)):
            await asyncio.to_thread(self._ensure_topics_sync)
        return True

    def _ensure_topics_sync(self) -> None:
        """Create required domain topics before the consumer subscribes.

        This is deliberately an AdminClient operation rather than relying on a
        broker's optional auto-create setting.  Existing topics are left
        untouched; a production platform can set ``KAFKA_TOPIC_AUTO_PROVISION``
        to false and manage them through its own IaC.
        """
        from confluent_kafka import KafkaError, KafkaException
        from confluent_kafka.admin import AdminClient, NewTopic

        partition_count = max(1, int(getattr(self.settings, "kafka_topic_partitions", 1) or 1))
        replication_factor = max(1, int(getattr(self.settings, "kafka_topic_replication_factor", 1) or 1))
        names = sorted({topic_for(self.settings, event_type) for event_type in AIOpsEventType})
        admin = AdminClient({"bootstrap.servers": self.bootstrap_servers, "client.id": f"{self.client_id}-admin"})
        futures = admin.create_topics([NewTopic(name, num_partitions=partition_count,
                                                replication_factor=replication_factor) for name in names])
        for name, future in futures.items():
            try:
                future.result(15)
            except KafkaException as exc:
                error = exc.args[0] if exc.args else None
                if error is None or error.code() != KafkaError.TOPIC_ALREADY_EXISTS:
                    raise RuntimeError(f"unable to provision Kafka topic {name}: {exc}") from exc

    async def publish(self, topic: str, envelope: EventEnvelope) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka publisher is not started")
        payload = envelope.model_dump_json(by_alias=True).encode("utf-8")
        key = (envelope.task_id or envelope.incident_id or envelope.idempotency_key).encode("utf-8")
        await asyncio.to_thread(self._publish_sync, topic, key, payload)

    def _publish_sync(self, topic: str, key: bytes, payload: bytes) -> None:
        assert self._producer is not None
        delivered: list[Exception] = []

        def callback(error: Any, _: Any) -> None:
            if error is not None:
                delivered.append(RuntimeError(str(error)))

        self._producer.produce(topic=topic, key=key, value=payload, on_delivery=callback)
        pending = self._producer.flush(15)
        if pending or delivered:
            raise delivered[0] if delivered else RuntimeError(f"Kafka delivery timed out for topic={topic}")

    async def close(self) -> None:
        if self._producer is not None:
            await asyncio.to_thread(self._producer.flush, 10)
            self._producer = None


class KafkaConsumerWorker:
    """Real Kafka consumer with at-least-once handling and durable deduplication.

    The business handler is responsible for its domain transaction.  Offsets are
    committed only after `consume_once` has persisted the idempotency key.  A
    malformed or repeatedly failing message is published to the DLQ with the
    original payload retained for operator replay.
    """

    def __init__(self, settings: Any, store: Any, publisher: KafkaPublisher, handler: Any):
        self.settings, self.store, self.publisher, self.handler = settings, store, publisher, handler
        self.enabled = bool(getattr(settings, "kafka_consumer_enabled", True))
        self.bootstrap_servers = str(getattr(settings, "kafka_bootstrap_servers", "") or "").strip()
        self.group_id = str(getattr(settings, "kafka_consumer_group", "ops-autoagent-codeops") or "ops-autoagent-codeops")
        self._consumer: Any | None = None
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bootstrap_servers)

    async def start(self, *, required: bool = False) -> bool:
        if not self.configured:
            if required:
                raise RuntimeError("Kafka consumer is required in production: configure Kafka consumer settings")
            return False
        from confluent_kafka import Consumer
        self._consumer = Consumer({
            "bootstrap.servers": self.bootstrap_servers, "group.id": self.group_id,
            "client.id": f"{getattr(self.settings, 'kafka_client_id', 'ops-autoagent')}-consumer",
            "enable.auto.commit": False, "auto.offset.reset": "earliest",
            "isolation.level": "read_committed",
        })
        topics = [topic_for(self.settings, event_type) for event_type in AIOpsEventType if event_type != AIOpsEventType.DEAD_LETTER]
        self._consumer.subscribe(topics)
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="kafka-event-consumer")
        return True

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._consumer is not None:
            await asyncio.to_thread(self._consumer.close)
            self._consumer = None

    async def _run(self) -> None:
        timeout = max(50, int(getattr(self.settings, "kafka_consumer_poll_timeout_ms", 500) or 500)) / 1000
        while not self._stopped.is_set():
            message = await asyncio.to_thread(self._consumer.poll, timeout) if self._consumer else None
            if message is None:
                continue
            if message.error():
                logger.warning("Kafka consumer error: %s", message.error())
                continue
            try:
                envelope = EventEnvelope.model_validate_json(message.value())
                await self._process(envelope)
                await asyncio.to_thread(self._consumer.commit, message=message, asynchronous=False)
            except Exception as exc:
                await self._dead_letter(message, exc)
                await asyncio.to_thread(self._consumer.commit, message=message, asynchronous=False)

    async def _process(self, envelope: EventEnvelope) -> bool:
        from .outbox import consume_once
        return await consume_once(self.store, envelope, self.handler)

    async def _dead_letter(self, message: Any, exc: Exception) -> None:
        raw = message.value().decode("utf-8", errors="replace") if message is not None and message.value() else ""
        envelope = EventEnvelope(
            eventType=AIOpsEventType.DEAD_LETTER, idempotencyKey=f"dlq:{message.topic()}:{message.partition()}:{message.offset()}",
            payload={"sourceTopic": message.topic(), "partition": message.partition(), "offset": message.offset(),
                     "errorType": type(exc).__name__, "error": str(exc)[:1000], "rawPayload": raw[:16000]},
        )
        try:
            await self.publisher.publish(topic_for(self.settings, AIOpsEventType.DEAD_LETTER), envelope)
        except Exception:
            logger.exception("Unable to publish Kafka DLQ event")


def topic_for(settings: Any, event_type: AIOpsEventType) -> str:
    names = {AIOpsEventType.INCIDENT: "kafka_topic_incidents",
             AIOpsEventType.EVIDENCE: "kafka_topic_evidence", AIOpsEventType.REPAIR: "kafka_topic_repair",
             AIOpsEventType.REVIEW: "kafka_topic_review", AIOpsEventType.APPROVAL: "kafka_topic_approval",
             AIOpsEventType.AUDIT: "kafka_topic_audit", AIOpsEventType.DEAD_LETTER: "kafka_topic_dlq"}
    return str(getattr(settings, names[event_type]))


def event_type_for_task_event(task_event: dict[str, Any]) -> AIOpsEventType:
    stage = str(task_event.get("stage") or "").lower()
    if stage in {"ops_diagnosis", "ops_evidence", "topology_correlation"}:
        return AIOpsEventType.EVIDENCE
    if stage in {"bug_fix", "repair_proposal", "test_verification"}:
        return AIOpsEventType.REPAIR
    if stage in {"release_risk_analysis", "independent_review"}:
        return AIOpsEventType.REVIEW
    if stage in {"human_approval", "prepare_approval", "deliver_patch", "apply_approved_patch", "rejected"}:
        return AIOpsEventType.APPROVAL
    return AIOpsEventType.AUDIT


def serialize_envelope(value: EventEnvelope) -> dict[str, Any]:
    """Store-safe form; datetime remains ISO text rather than a driver-specific object."""
    return json.loads(value.model_dump_json(by_alias=True))
