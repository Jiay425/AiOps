"""Small durable observability projection for task, artifact and runtime metrics."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from .schemas import now_iso
from .store import Store


_SECRET_KEY = re.compile(r"(api[_-]?key|token|password|secret|authorization|cookie|prompt)", re.I)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact(value: Any, *, limit: int = 1200) -> Any:
    if isinstance(value, dict):
        return {str(key): "***" if _SECRET_KEY.search(str(key)) else redact(item, limit=limit)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, limit=limit) for item in value[:100]]
    if isinstance(value, str):
        text = _EMAIL.sub("[REDACTED_EMAIL]", _BEARER.sub("Bearer [REDACTED]", value))
        return text[:limit] + "...truncated..." if len(text) > limit else text
    return value


class RuntimeObservability:
    def __init__(self, store: Store | None = None, enabled: bool = True):
        self.store = store
        self.enabled = enabled
        self.counters: dict[str, int] = {}

    async def artifact(self, task_id: str, kind: str, summary: str, payload: Any = None,
                       *, subgraph: str = "", node: str = "", run_id: str = "") -> str:
        digest = hashlib.sha256(json.dumps(payload if payload is not None else summary,
                                           ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        artifact_id = f"artifact-{digest[:24]}"
        if self.store and self.enabled:
            record = {"artifactId": artifact_id, "taskId": task_id, "runId": run_id,
                      "kind": kind, "summary": redact(summary), "digest": digest,
                      "payload": redact(payload) if payload is not None else None,
                      "subgraph": subgraph, "node": node, "createTime": now_iso(), "updateTime": now_iso()}
            try:
                await self.store.put("artifacts", artifact_id, record, record["updateTime"])
            except Exception:
                pass
        return artifact_id

    async def metric(self, task_id: str, name: str, value: float | int | bool, *, run_id: str = "",
                     subgraph: str = "", node: str = "", attempt: int = 0,
                     tags: dict[str, Any] | None = None) -> dict[str, Any]:
        metric_id = f"metric-{uuid.uuid4()}"
        record = {"metricId": metric_id, "taskId": task_id, "runId": run_id, "metricName": name,
                  "value": value, "subgraph": subgraph, "node": node, "attempt": attempt,
                  "tags": redact(tags or {}), "recordedAt": now_iso()}
        self.counters[name] = self.counters.get(name, 0) + 1
        if self.store and self.enabled:
            try:
                await self.store.put("runtime_metrics", metric_id, record, record["recordedAt"])
            except Exception:
                pass
        return record

    async def duration(self, task_id: str, name: str, started: float, **kwargs: Any) -> dict[str, Any]:
        return await self.metric(task_id, name, int((time.perf_counter() - started) * 1000), **kwargs)


__all__ = ["RuntimeObservability", "redact"]
