from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import httpx

from ..config import Settings


class OpsDemoDataAutoSeeder:
    """Non-blocking port of the legacy ApplicationReadyEvent demo evidence seeder."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def seed(self) -> dict[str, Any]:
        await asyncio.sleep(max(0, self.settings.ops_demo_auto_seed_start_delay_seconds))
        trace_id = self.settings.ops_demo_auto_seed_trace_id or f"trace-ops-auto-{uuid.uuid4()}"
        seeded = await self._seed_elasticsearch(trace_id)
        await self._generate_fault_traffic()
        return {"serviceName": self.settings.ops_demo_auto_seed_service_name, "traceId": trace_id,
                "elasticsearchIndex": self.settings.ops_demo_auto_seed_elasticsearch_index,
                "elasticsearchSeeded": seeded}

    async def _seed_elasticsearch(self, trace_id: str) -> bool:
        if not self.settings.elk_base_url:
            return False
        attempts = max(1, self.settings.ops_demo_auto_seed_elasticsearch_max_attempts)
        for attempt in range(attempts):
            if await self._seed_once(trace_id):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(max(1, self.settings.ops_demo_auto_seed_elasticsearch_retry_interval_seconds))
        return False

    async def _seed_once(self, trace_id: str) -> bool:
        root = self.settings.elk_base_url.rstrip("/")
        index = self.settings.ops_demo_auto_seed_elasticsearch_index
        mapping = {"mappings": {"properties": {
            "@timestamp": {"type": "date", "format": "yyyy-MM-dd HH:mm:ss||strict_date_optional_time||epoch_millis"},
            "serviceName": {"type": "keyword"}, "application": {"type": "keyword"},
            "traceId": {"type": "keyword"}, "level": {"type": "keyword"},
            "exception": {"type": "keyword"}, "message": {"type": "text"}, "stack_trace": {"type": "text"}}}}
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base = {"@timestamp": timestamp, "serviceName": self.settings.ops_demo_auto_seed_service_name,
                "application": self.settings.ops_demo_auto_seed_service_name, "traceId": trace_id, "level": "ERROR"}
        documents = [
            {**base, "message": "SQLTimeoutException: HikariPool-1 - Connection is not available, request timed out after 30000ms",
             "exception": "java.sql.SQLTimeoutException",
             "stack_trace": "java.sql.SQLTimeoutException: Connection is not available\n at com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:200)"},
            {**base, "message": "Mock order create failed: database connection timeout",
             "exception": "java.lang.IllegalStateException",
             "stack_trace": "java.lang.IllegalStateException: database connection timeout\n at com.opsautoagent.trigger.http.OpsMockFaultController.createOrder"},
        ]
        auth = ((self.settings.elk_username, self.settings.elk_password)
                if self.settings.elk_username else None)
        try:
            async with httpx.AsyncClient(timeout=5, auth=auth) as client:
                created = await client.put(f"{root}/{index}", json=mapping)
                if created.status_code not in {200, 201} and "resource_already_exists_exception" not in created.text:
                    return False
                responses = [await client.post(f"{root}/{index}/_doc", json=document) for document in documents]
                refreshed = await client.post(f"{root}/{index}/_refresh")
            return all(response.status_code < 300 for response in responses) and refreshed.status_code < 300
        except httpx.HTTPError:
            return False

    async def _generate_fault_traffic(self) -> None:
        root = self.settings.ops_demo_auto_seed_app_base_url.rstrip("/")
        requests = (["mode=error"] * max(0, self.settings.ops_demo_auto_seed_error_count)
                    + ["mode=slow&sleepMillis=1600"] * max(0, self.settings.ops_demo_auto_seed_slow_count)
                    + ["mode=db&holdSeconds=3"] * max(0, self.settings.ops_demo_auto_seed_db_count))
        async with httpx.AsyncClient(timeout=15) as client:
            for query in requests:
                try:
                    await client.get(f"{root}/api/v1/ops/mock/order/create?{query}")
                except httpx.HTTPError:
                    pass
