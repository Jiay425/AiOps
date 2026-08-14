from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import Settings


class McpHttpClient:
    def __init__(self, url: str, timeout: float = 15.0):
        self.url, self.timeout = url, timeout

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            init = await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ops-autoagent", "version": "2.0.0"}}})
            init.raise_for_status()
            session = init.headers.get("mcp-session-id", "")
            if session:
                headers["mcp-session-id"] = session
            await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "id": 2,
                                         "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            response.raise_for_status()
            payload = self._payload(response)
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            result = payload.get("result", {})
            if result.get("isError"):
                raise RuntimeError(str(result.get("content", "MCP tool error")))
            return result

    async def list_tools(self) -> list[dict[str, Any]]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            init = await client.post(self.url, headers=headers, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ops-autoagent", "version": "2.0.0"}}})
            init.raise_for_status()
            session = init.headers.get("mcp-session-id", "")
            if session:
                headers["mcp-session-id"] = session
            await client.post(self.url, headers=headers,
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = await client.post(self.url, headers=headers,
                                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            response.raise_for_status()
            payload = self._payload(response)
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            return list(payload.get("result", {}).get("tools", []))

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return response.json()
        events = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        if not events:
            raise RuntimeError("MCP server returned an empty event stream")
        return json.loads(events[-1])


class McpLegacySseClient:
    """MCP 2024-11-05 HTTP+SSE transport used by the legacy Spring MCP client."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url, self.timeout = url, timeout

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._exchange("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._exchange("tools/list", {})
        return list(result.get("tools", []))

    async def _exchange(self, method: str, params: dict[str, Any]) -> Any:
        timeout = httpx.Timeout(self.timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", self.url, headers={"Accept": "text/event-stream"}) as stream:
                stream.raise_for_status()
                lines = stream.aiter_lines()
                endpoint = await self._next_sse_data(lines, expected_event="endpoint")
                message_url = urljoin(str(stream.url), endpoint)
                await client.post(message_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "ops-autoagent", "version": "2.0.0"}}})
                initialized = json.loads(await self._next_sse_data(lines))
                if initialized.get("error"):
                    raise RuntimeError(str(initialized["error"]))
                await client.post(message_url,
                                  json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                await client.post(message_url,
                                  json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
                payload = json.loads(await self._next_sse_data(lines))
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                result = payload.get("result", {})
                if result.get("isError"):
                    raise RuntimeError(str(result.get("content", "MCP tool error")))
                return result

    @staticmethod
    async def _next_sse_data(lines: Any, expected_event: str | None = None) -> str:
        event = None
        async for line in lines:
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and (expected_event is None or event == expected_event):
                return line[5:].strip()
        raise RuntimeError("MCP SSE stream ended before a response was received")


class McpStdioClient:
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 timeout: float = 30.0):
        self.command = shutil.which(command) or command
        self.args, self.env, self.timeout = args or [], env or {}, timeout

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._exchange("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._exchange("tools/list", {})
        return list(result.get("tools", []))

    async def _exchange(self, method: str, params: dict[str, Any]) -> Any:
        process = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ, **{str(k): str(v) for k, v in self.env.items()}},
        )
        try:
            await self._send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "ops-autoagent", "version": "2.0.0"}}})
            initialized = await self._receive(process, 1)
            if initialized.get("error"):
                raise RuntimeError(str(initialized["error"]))
            await self._send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            await self._send(process, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
            payload = await self._receive(process, 2)
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            result = payload.get("result", {})
            if result.get("isError"):
                raise RuntimeError(str(result.get("content", "MCP tool error")))
            return result
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    @staticmethod
    async def _send(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise RuntimeError("MCP stdio process has no stdin")
        process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    async def _receive(self, process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
        if process.stdout is None:
            raise RuntimeError("MCP stdio process has no stdout")
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), self.timeout)
            if not line:
                error = await process.stderr.read() if process.stderr else b""
                raise RuntimeError(f"MCP stdio process ended: {error.decode(errors='replace')}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == request_id:
                return payload


def mcp_client_from_config(config: dict[str, Any]) -> McpHttpClient | McpLegacySseClient | McpStdioClient:
    transport = str(config.get("transportType") or "").lower()
    detail = config.get("transportConfig") or {}
    timeout = max(1, int(config.get("requestTimeout") or 30))
    if transport == "sse":
        base_uri = str(detail.get("baseUri") or "")
        endpoint = str(detail.get("sseEndpoint") or "/sse")
        url = base_uri if "sse" in base_uri.rsplit("/", 1)[-1] else base_uri.rstrip("/") + "/" + endpoint.lstrip("/")
        return McpLegacySseClient(url, timeout * 60)
    if transport == "stdio":
        entry = detail.get(str(config.get("mcpName"))) or detail
        return McpStdioClient(str(entry.get("command") or ""), list(entry.get("args") or []),
                              dict(entry.get("env") or {}), timeout)
    if transport in {"streamable_http", "http"}:
        return McpHttpClient(str(detail.get("url") or detail.get("baseUri") or ""), timeout)
    raise ValueError(f"Unsupported MCP transport type: {transport}")


class ObservabilityTools:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.settings.integration_timeout_seconds,
                             connect=self.settings.integration_connect_timeout_seconds)

    async def prometheus(self, service: str, start: str, end: str, fixture_case_id: str = "",
                         endpoint: str = "", problem: str = "") -> dict[str, Any]:
        if fixture_case_id and self.settings.ops_fixture_fallback:
            fixture = self._fixture_or_error(
                service, "prometheus.json", RuntimeError("explicit evaluation fixture"), fixture_case_id)
            if fixture.get("available"):
                return self._prometheus_fixture_evidence(fixture)
        endpoint_path = self._endpoint_path(endpoint, problem)
        queries = self._prometheus_queries(service, endpoint_path)
        observations: list[str] = []
        raw_lines: list[str] = []
        raw_responses: dict[str, Any] = {}
        first_error: Exception | None = None
        for metric_name, promql in queries:
            try:
                payload = await self._prometheus_query(promql, start, end)
                raw_responses[metric_name] = payload
                snapshot = self._metric_snapshot(metric_name, payload)
                observations.append(snapshot["observation"])
                raw_lines.append(snapshot["evidence"])
            except Exception as exc:
                first_error = first_error or exc
                observations.append(f"{metric_name} query failed: {exc}")
        if first_error and len(raw_responses) == 0:
            fallback = self._fixture_or_error(service, "prometheus.json", first_error, fixture_case_id)
            if fallback.get("available"):
                return self._prometheus_fixture_evidence(fallback)
        return {
            "source": "prometheus", "available": True,
            "summary": "Collected Prometheus metrics and converted them into readable observations for traffic, "
                       "5xx, latency, CPU, JVM, thread, GC, Tomcat/executor, and Hikari pool dimensions.",
            "observations": observations, "rawData": "\n".join(raw_lines)[:12000], "raw": raw_responses,
            "sourceMetadata": {"sourceType": "PROMETHEUS",
                               "sourceMode": "REAL_MCP_OR_HTTP" if self.settings.ops_mcp_prefer else "REAL_HTTP",
                               "baseUrl": self.settings.prometheus_base_url or "", "mcpEnabled": bool(
                                   self.settings.ops_mcp_prefer and self.settings.ops_mcp_grafana_url),
                               "endpoint": endpoint, "endpointPath": endpoint_path,
                               "timeWindow": f"{start} ~ {end}",
                               "queries": [{"metricName": name, "promQl": query} for name, query in queries],
                               "fixtureFallback": False},
        }

    async def _prometheus_query(self, promql: str, start: str, end: str) -> Any:
        if self.settings.ops_mcp_prefer and self.settings.ops_mcp_grafana_url:
            try:
                arguments = {"query": promql, "start": start, "end": end}
                if self.settings.ops_mcp_grafana_datasource_uid:
                    arguments["datasource"] = self.settings.ops_mcp_grafana_datasource_uid
                result = await McpHttpClient(
                    self.settings.ops_mcp_grafana_url, self.settings.integration_timeout_seconds
                ).call_tool(self.settings.ops_mcp_grafana_query_tool_name, arguments)
                return self._mcp_json_content(result)
            except Exception:
                if not self.settings.ops_mcp_fallback_http or not self.settings.prometheus_base_url:
                    raise
        if not self.settings.prometheus_base_url:
            raise RuntimeError("ops.integrations.prometheus.base-url is blank")
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.get(
                f"{self.settings.prometheus_base_url.rstrip('/')}/api/v1/query_range",
                params={"query": promql, "start": self._epoch(start), "end": self._epoch(end), "step": 30},
                auth=(self.settings.prometheus_username, self.settings.prometheus_password)
                if self.settings.prometheus_username else None,
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _mcp_json_content(result: Any) -> Any:
        if isinstance(result, dict) and "data" in result:
            return result
        content = result.get("content", []) if isinstance(result, dict) else []
        text = "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        return json.loads(text) if text.strip() else result

    @staticmethod
    def _metric_snapshot(metric_name: str, payload: Any) -> dict[str, str]:
        try:
            result = payload.get("data", {}).get("result", [])
            values: list[float] = []
            for series in result:
                points = series.get("values") or ([series.get("value")] if series.get("value") else [])
                for point in points:
                    if not point or len(point) < 2:
                        continue
                    try:
                        value = float(point[1])
                    except (TypeError, ValueError):
                        value = 0.0
                    if value not in {float("inf"), float("-inf")} and value == value:
                        values.append(value)
            if not values:
                return {"observation": f"NO_DATA: {metric_name} has no Prometheus series in this window.",
                        "evidence": f"{metric_name}: no_data"}
            latest, minimum, maximum, average = values[-1], min(values), max(values), sum(values) / len(values)
            level = ObservabilityTools._metric_level(metric_name, maximum)
            suffix = ObservabilityTools._metric_advice(metric_name, level)
            fmt = ObservabilityTools._metric_format
            return {"observation": f"{level}: {metric_name} latest={fmt(latest)}, max={fmt(maximum)}, "
                                   f"avg={fmt(average)}, points={len(values)}{suffix}",
                    "evidence": f"{metric_name}: latest={fmt(latest)}, min={fmt(minimum)}, "
                                f"max={fmt(maximum)}, avg={fmt(average)}, points={len(values)}, level={level}"}
        except Exception as exc:
            return {"observation": f"UNKNOWN: {metric_name} returned data but could not be parsed: {exc}",
                    "evidence": f"{metric_name}: parse_error={exc}"}

    @staticmethod
    def _metric_level(name: str, maximum: float) -> str:
        thresholds = {
            "http_5xx_qps": (0, "http_5xx_detected"),
            "http_5xx_rate_percent": (1, "http_5xx_rate_high"),
            "http_avg_latency_seconds": (1, "avg_latency_high"),
            "http_p95_latency_seconds": (1, "p95_latency_high"),
            "http_p99_latency_seconds": (1, "p99_latency_high"),
            "process_cpu_usage": (0.8, "cpu_usage_high"), "system_cpu_usage": (0.8, "cpu_usage_high"),
            "jvm_gc_pause_avg_seconds": (0.2, "gc_pause_high"),
            "hikari_connections_usage_percent": (80, "hikari_pool_high_usage"),
            "hikari_connections_pending": (0, "hikari_pending_connections"),
            "hikari_connection_timeout_total": (0, "hikari_connection_timeout"),
        }
        if name not in thresholds:
            return "OK"
        threshold, label = thresholds[name]
        anomalous = maximum > threshold if threshold == 0 else maximum >= threshold
        return f"ANOMALY: {label}" if anomalous else "OK"

    @staticmethod
    def _metric_advice(name: str, level: str) -> str:
        if not level.startswith("ANOMALY"):
            return ""
        if name in {"http_5xx_qps", "http_5xx_rate_percent"}:
            return ", evidence=接口 5xx 在窗口内出现"
        if name in {"http_avg_latency_seconds", "http_p95_latency_seconds", "http_p99_latency_seconds"}:
            return ", evidence=接口耗时在窗口内升高"
        if name.startswith("hikari_"):
            return ", evidence=数据库连接池出现压力信号"
        return ", evidence=资源指标出现异常"

    @staticmethod
    def _metric_format(value: float) -> str:
        if abs(value) >= 1000:
            return f"{value:.0f}"
        if abs(value) >= 10:
            return f"{value:.2f}"
        return f"{value:.4f}"

    @staticmethod
    def _prometheus_queries(service: str, endpoint: str) -> list[tuple[str, str]]:
        escaped = service.replace("\\", "\\\\").replace('"', '\\"')

        def selector(label: str, extra: str = "", http: bool = False) -> str:
            labels = [] if not label else [f'{label}="{escaped}"']
            if http and endpoint:
                labels.append(f'uri="{endpoint.replace(chr(34), chr(92) + chr(34))}"')
            if extra:
                labels.append(extra)
            return "{" + ",".join(labels) + "}"

        def join_metric(metric: str, http: bool = False, extra: str = "") -> str:
            return " or ".join(metric + selector(label, extra, http)
                               for label in ("application", "job", "service", ""))

        qps = " or ".join(f"sum(rate(http_server_requests_seconds_count{selector(label, http=True)}[1m]))"
                          for label in ("application", "job", "service", ""))
        status_filter = 'status=~"5.."'
        error_qps = " or ".join(
            f"sum(rate(http_server_requests_seconds_count{selector(label, status_filter, True)}[1m]))"
            for label in ("application", "job", "service", ""))
        latency_sum = " or ".join(
            f"sum(rate(http_server_requests_seconds_sum{selector(label, http=True)}[1m]))"
            for label in ("application", "job", "service", ""))

        def quantile(value: str) -> str:
            return " or ".join(
                f"histogram_quantile({value}, sum(rate(http_server_requests_seconds_bucket"
                f"{selector(label, http=True)}[1m])) by (le))"
                for label in ("application", "job", "service", ""))

        gc_sum = " or ".join(
            f"sum(rate(jvm_gc_pause_seconds_sum{selector(label)}[1m]))"
            for label in ("application", "job", "service", ""))
        gc_count = " or ".join(
            f"sum(rate(jvm_gc_pause_seconds_count{selector(label)}[1m]))"
            for label in ("application", "job", "service", ""))
        hikari_active = f"{join_metric('hikaricp_connections_active')} or {join_metric('hikari_connections_active')}"
        hikari_max = f"{join_metric('hikaricp_connections_max')} or {join_metric('hikari_connections_max')}"
        return [
            ("traffic_qps", qps), ("http_5xx_qps", error_qps),
            ("http_5xx_rate_percent", f"100 * ({error_qps}) / clamp_min(({qps}), 0.001)"),
            ("http_avg_latency_seconds", f"({latency_sum}) / clamp_min(({qps}), 0.001)"),
            ("http_p95_latency_seconds", quantile("0.95")),
            ("http_p99_latency_seconds", quantile("0.99")),
            ("process_cpu_usage", join_metric("process_cpu_usage")),
            ("system_cpu_usage", join_metric("system_cpu_usage")),
            ("jvm_memory_used_bytes", f"sum({join_metric('jvm_memory_used_bytes')})"),
            ("jvm_threads_live", join_metric("jvm_threads_live_threads")),
            ("jvm_gc_pause_avg_seconds", f"({gc_sum}) / clamp_min(({gc_count}), 0.001)"),
            ("tomcat_threads_busy", join_metric("tomcat_threads_busy_threads")),
            ("executor_active_threads", f"{join_metric('executor_active_threads')} or "
                                        f"{join_metric('executor_pool_active_threads')}"),
            ("hikari_connections_active", hikari_active),
            ("hikari_connections_max", hikari_max),
            ("hikari_connections_usage_percent", f"100 * ({hikari_active}) / clamp_min(({hikari_max}), 1)"),
            ("hikari_connections_pending", f"{join_metric('hikaricp_connections_pending')} or "
                                           f"{join_metric('hikari_connections_pending')}"),
            ("hikari_connection_timeout_total", f"{join_metric('hikaricp_connections_timeout_total')} or "
                                                 f"{join_metric('hikari_connections_timeout_total')}"),
        ]

    @staticmethod
    def _endpoint_path(endpoint: str, problem: str) -> str:
        value = (endpoint or "").strip()
        if not value and "Affected endpoints:" in (problem or ""):
            value = problem.split("Affected endpoints:", 1)[1].strip().split(",", 1)[0].strip()
        if " " in value:
            value = value.split(" ", 1)[1].strip()
        return value

    @staticmethod
    def _epoch(value: str) -> int:
        for candidate in (value, value.replace(" ", "T", 1)):
            try:
                return int(datetime.fromisoformat(candidate.replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        return int(datetime.now().timestamp())

    def _prometheus_fixture_evidence(self, fallback: dict[str, Any]) -> dict[str, Any]:
        raw = fallback.get("raw") or {}
        observations, raw_lines = [], []
        aliases = {
            "hikaricp_connections_pending": "hikari_connections_pending",
            "hikaricp_connections_timeout_total": "hikari_connection_timeout_total",
        }
        for metric in raw.get("metrics", []):
            name = aliases.get(str(metric.get("name")), str(metric.get("name")))
            value = float(metric.get("value") or 0)
            level = self._metric_level(name, value)
            observations.append(f"{level}: {name} latest={self._metric_format(value)}, "
                                f"max={self._metric_format(value)}, avg={self._metric_format(value)}, points=1"
                                f"{self._metric_advice(name, level)}")
            raw_lines.append(f"{name}: latest={self._metric_format(value)}, min={self._metric_format(value)}, "
                             f"max={self._metric_format(value)}, avg={self._metric_format(value)}, points=1, level={level}")
        return {**fallback, "source": "FIXTURE", "summary": "Loaded deterministic Prometheus fixture evidence.",
                "observations": observations, "rawData": "\n".join(raw_lines),
                "sourceMetadata": {"sourceType": "PROMETHEUS", "sourceMode": "FIXTURE_FALLBACK",
                                   "fixtureFallback": True}}

    async def elk(self, service: str, start: str, end: str, problem: str, fixture_case_id: str = "") -> dict[str, Any]:
        if fixture_case_id and self.settings.ops_fixture_fallback:
            fixture = self._fixture_or_error(service, "es-logs.json", RuntimeError("explicit evaluation fixture"),
                                              fixture_case_id)
            if fixture.get("available"):
                return self._elk_fixture_evidence(fixture)
        body = {
            "size": 10, "sort": [{"@timestamp": {"order": "desc"}}],
            "_source": ["@timestamp", "serviceName", "application", "traceId", "level", "message",
                        "exception", "stack_trace"],
            "query": {"bool": {"filter": [{"range": {"@timestamp": {
                "gte": start, "lte": end, "time_zone": "+08:00",
                "format": "yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH:mm||strict_date_optional_time||epoch_millis"}}}],
                "must": [{"bool": {"should": [
                    {"match_phrase": {"serviceName": service}}, {"match_phrase": {"application": service}},
                    {"match_phrase": {"app": service}}], "minimum_should_match": 1}},
                    {"bool": {"should": [{"match_phrase": {"level": "ERROR"}},
                                           {"match_phrase": {"message": "Exception"}},
                                           {"match_phrase": {"message": "error"}},
                                           {"match_phrase": {"message": problem}}],
                              "minimum_should_match": 1}}]}},
        }
        if self.settings.ops_mcp_prefer and self.settings.ops_mcp_elasticsearch_url:
            try:
                raw = await McpHttpClient(self.settings.ops_mcp_elasticsearch_url, self.settings.integration_timeout_seconds).call_tool(
                    self.settings.ops_mcp_elasticsearch_search_tool_name,
                    {"index": self.settings.elk_index_pattern, "query": body, "body": body})
                return self._elk_evidence(self._mcp_json_content(raw), body, start, end, "REAL_MCP_OR_HTTP")
            except Exception as exc:
                if not self.settings.ops_mcp_fallback_http:
                    return self._elk_failure(exc)
        if not self.settings.elk_base_url:
            fallback = self._fixture_or_error(service, "es-logs.json", RuntimeError("ELK_BASE_URL is blank"),
                                              fixture_case_id)
            return self._elk_fixture_evidence(fallback) if fallback.get("available") else {
                "source": "elk", "available": False,
                "summary": "ELK/Elasticsearch base-url is not configured; live log query is skipped.",
                "errorSamples": ["Configure ops.integrations.elk.base-url and ops.integrations.elk.index-pattern.",
                                 "Log search will use serviceName, incident window, ERROR/Exception, and problem keywords."],
                "rawData": "", "raw": {}, "sourceMetadata": {"sourceType": "ELASTICSEARCH",
                    "sourceMode": "UNCONFIGURED", "baseUrl": "", "indexPattern": self.settings.elk_index_pattern,
                    "mcpEnabled": False, "fixtureFallback": False}}
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(
                    f"{self.settings.elk_base_url.rstrip('/')}/{self.settings.elk_index_pattern}/_search", json=body,
                    auth=(self.settings.elk_username, self.settings.elk_password) if self.settings.elk_username else None,
                )
                response.raise_for_status()
            return self._elk_evidence(response.json(), body, start, end, "REAL_HTTP")
        except Exception as exc:
            fallback = self._fixture_or_error(service, "es-logs.json", exc, fixture_case_id)
            return self._elk_fixture_evidence(fallback) if fallback.get("available") else self._elk_failure(exc)

    def _elk_evidence(self, raw: Any, body: dict[str, Any], start: str, end: str, mode: str) -> dict[str, Any]:
        hits = raw.get("hits", {}).get("hits", []) if isinstance(raw, dict) else []
        total_value = raw.get("hits", {}).get("total", 0) if isinstance(raw, dict) else 0
        total = int(total_value.get("value", 0) if isinstance(total_value, dict) else total_value or 0)
        samples = []
        for hit in hits:
            source = hit.get("_source") or {}
            timestamp = source.get("@timestamp") or ""
            level = source.get("level") or source.get("log_level") or ""
            trace_id = source.get("traceId") or source.get("trace_id") or ""
            message = source.get("message") or source.get("exception") or source.get("stack_trace") or ""
            samples.append(f"[{timestamp}] [{level}] traceId={trace_id} {message}"[:1600])
        if not samples:
            samples = ["ELK real query succeeded but returned zero matching incident log samples."]
        summary = (f"Collected {total} ERROR/Exception log samples from real Elasticsearch for the incident window."
                   if total > 0 else
                   "Elasticsearch query succeeded, but no matching ERROR/Exception log was found in the incident window.")
        return {"source": "elk", "available": True, "summary": summary, "errorSamples": samples,
                "rawData": json.dumps(raw, ensure_ascii=False)[:6000], "raw": raw,
                "sourceMetadata": {"sourceType": "ELASTICSEARCH", "sourceMode": mode,
                                   "baseUrl": self.settings.elk_base_url or "",
                                   "indexPattern": self.settings.elk_index_pattern, "query": body,
                                   "timeWindow": f"{start} ~ {end}", "totalHits": total,
                                   "fixtureFallback": False}}

    def _elk_failure(self, exc: Exception) -> dict[str, Any]:
        return {"source": "elk", "available": False, "summary": f"ELK query failed: {exc}",
                "errorSamples": ["Check Elasticsearch base-url, index pattern, auth, and @timestamp field."],
                "rawData": "", "raw": {}, "sourceMetadata": {"sourceType": "ELASTICSEARCH",
                    "sourceMode": "REAL_QUERY_FAILED", "baseUrl": self.settings.elk_base_url or "",
                    "indexPattern": self.settings.elk_index_pattern, "error": str(exc), "fixtureFallback": False}}

    def _elk_fixture_evidence(self, fallback: dict[str, Any]) -> dict[str, Any]:
        raw = fallback.get("raw") or {}
        rows = raw.get("logs") or raw.get("hits") or []
        samples = [f"[{row.get('timestamp', '')}] [{row.get('level', '')}] "
                   f"traceId={row.get('traceId') or row.get('trace_id') or ''} {row.get('message', '')}"[:1600]
                   for row in rows]
        return {**fallback, "source": "FIXTURE", "summary": f"Loaded {len(rows)} deterministic log samples.",
                "errorSamples": samples or ["Fixture contains zero matching incident log samples."],
                "rawData": json.dumps(raw, ensure_ascii=False)[:6000],
                "sourceMetadata": {"sourceType": "ELASTICSEARCH", "sourceMode": "FIXTURE_FALLBACK",
                                   "fixtureFallback": True}}

    async def skywalking(self, service: str, trace_id: str | None, start: str, end: str,
                         fixture_case_id: str = "", endpoint: str = "", problem: str = "") -> dict[str, Any]:
        if fixture_case_id and self.settings.ops_fixture_fallback:
            fixture = self._fixture_or_error(service, "skywalking-trace.json",
                                              RuntimeError("explicit evaluation fixture"), fixture_case_id)
            if fixture.get("available"):
                return self._skywalking_fixture_evidence(fixture)
        if not self.settings.skywalking_graphql_url:
            fallback = self._fixture_or_error(service, "skywalking-trace.json",
                                              RuntimeError("SKYWALKING_GRAPHQL_URL is blank"), fixture_case_id)
            return self._skywalking_fixture_evidence(fallback) if fallback.get("available") else {
                "source": "skywalking", "available": False,
                "summary": "SkyWalking GraphQL url is not configured; live trace query is skipped.",
                "spans": ["Configure ops.integrations.skywalking.graphql-url, for example "
                          "http://127.0.0.1:12800/graphql.",
                          "SkyWalking will collect traceId details, service metrics, error trace samples, and slow trace samples."],
                "rawData": "", "raw": {}, "sourceMetadata": {"sourceType": "SKYWALKING",
                    "sourceMode": "UNCONFIGURED", "graphqlUrl": "", "traceId": trace_id or "",
                    "endpoint": problem, "fixtureFallback": False}}
        spans: list[str] = []
        raw_sections: dict[str, Any] = {}
        if trace_id:
            await self._skywalking_run("trace_detail", self._trace_detail_query(trace_id), spans, raw_sections)
        else:
            spans.append("traceId is not provided; precise single-trace span query is skipped.")
        service_id = "0"
        endpoint_id = ""
        if service:
            service_id = await self._resolve_skywalking_service(service, start, end, spans, raw_sections)
            endpoint_path = self._endpoint_path(endpoint, problem)
            endpoint_id = await self._resolve_skywalking_endpoint(
                service_id, endpoint_path, spans, raw_sections) if endpoint_path else ""
            await self._skywalking_run("service_metrics", self._service_metrics_query(service, start, end),
                                       spans, raw_sections)
            await self._skywalking_run("error_trace_samples", self._basic_trace_query(
                service_id, endpoint_id, start, end, "ERROR", "BY_START_TIME"), spans, raw_sections)
            await self._skywalking_run("slow_trace_samples", self._basic_trace_query(
                service_id, endpoint_id, start, end, "ALL", "BY_DURATION"), spans, raw_sections)
        else:
            spans.append("serviceName is not provided; service-specific queries (service metadata, metrics, "
                         "error trace samples, slow trace samples) are skipped.")
        return {"source": "skywalking", "available": bool(raw_sections),
                "summary": "Collected SkyWalking evidence for trace detail, service metrics, error traces, and "
                           "slow traces in the incident window.",
                "spans": spans, "rawData": json.dumps(raw_sections, ensure_ascii=False)[:12000],
                "raw": raw_sections, "sourceMetadata": {"sourceType": "SKYWALKING", "sourceMode": "REAL_HTTP",
                    "graphqlUrl": self.settings.skywalking_graphql_url, "traceId": trace_id or "",
                    "serviceName": service, "endpoint": endpoint or self._endpoint_path("", problem),
                    "endpointPath": self._endpoint_path(endpoint, problem), "timeWindow": f"{start} ~ {end}",
                    "queriedSections": ["trace_detail", "service_metadata", "service_metrics",
                                        "error_trace_samples", "slow_trace_samples"], "fixtureFallback": False}}

    async def _skywalking_post(self, query: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            response = await client.post(self.settings.skywalking_graphql_url, json={"query": query},
                auth=(self.settings.skywalking_username, self.settings.skywalking_password)
                if self.settings.skywalking_username else None)
            response.raise_for_status()
            return response.json()

    async def _skywalking_run(self, name: str, query: str, spans: list[str], raw: dict[str, Any]) -> None:
        try:
            raw[name] = await self._skywalking_post(query)
            spans.append(f"{name} query succeeded.")
        except Exception as exc:
            spans.append(f"{name} query failed: {exc}")

    async def _resolve_skywalking_service(self, service: str, start: str, end: str,
                                          spans: list[str], raw: dict[str, Any]) -> str:
        try:
            response = await self._skywalking_post(self._search_service_query(service, start, end))
            raw["service_metadata"] = response
            services = response.get("data", {}).get("searchServices") or []
            if not services:
                spans.append("service_metadata query returned empty; fallback to serviceId=0.")
                return "0"
            match = next((item for item in services if item.get("name") == service), services[0])
            service_id = str(match.get("id") or "0")
            qualifier = "" if match.get("name") == service else " with fuzzy match"
            spans.append(f"service_metadata query succeeded{qualifier}. serviceId={service_id}")
            return service_id
        except Exception as exc:
            spans.append(f"service_metadata query failed: {exc}; fallback to serviceId=0.")
            return "0"

    async def _resolve_skywalking_endpoint(self, service_id: str, endpoint: str,
                                           spans: list[str], raw: dict[str, Any]) -> str:
        if not endpoint or service_id == "0":
            spans.append("endpoint_metadata query skipped; endpoint or serviceId is blank.")
            return ""
        try:
            response = await self._skywalking_post(self._search_endpoint_query(service_id, endpoint))
            raw["endpoint_metadata"] = response
            endpoints = response.get("data", {}).get("searchEndpoint") or []
            if not endpoints:
                spans.append(f"endpoint_metadata query returned empty for endpoint={endpoint}; "
                             "fallback to service-level trace samples.")
                return ""
            match = next((item for item in endpoints if item.get("name") == endpoint or
                          endpoint in str(item.get("name") or "")), endpoints[0])
            endpoint_id = str(match.get("id") or "")
            qualifier = "" if match.get("name") == endpoint else " with fuzzy match"
            spans.append(f"endpoint_metadata query succeeded{qualifier}. endpoint={endpoint}, "
                         f"endpointId={endpoint_id}")
            return endpoint_id
        except Exception as exc:
            spans.append(f"endpoint_metadata query failed: {exc}; fallback to service-level trace samples.")
            return ""

    @staticmethod
    def _skywalking_time(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H%M")
        except ValueError:
            return value or ""

    @staticmethod
    def _graphql_escape(value: str) -> str:
        return (value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")

    @classmethod
    def _search_service_query(cls, service: str, start: str, end: str) -> str:
        return ("query searchService { searchServices(duration: {"
                f'start: "{cls._skywalking_time(start)}", end: "{cls._skywalking_time(end)}", step: MINUTE'
                f'}}, keyword: "{cls._graphql_escape(service)}") {{ id name }} }}')

    @classmethod
    def _search_endpoint_query(cls, service_id: str, endpoint: str) -> str:
        return ("query searchEndpoint { searchEndpoint("
                f'keyword: "{cls._graphql_escape(endpoint)}", serviceId: "{cls._graphql_escape(service_id)}", '
                "limit: 10) { id name } }")

    @classmethod
    def _trace_detail_query(cls, trace_id: str) -> str:
        return (f'query queryTrace {{ queryTrace(traceId: "{cls._graphql_escape(trace_id)}") {{ spans {{ '
                "traceId segmentId spanId parentSpanId refs { traceId parentSegmentId parentSpanId type } "
                "serviceCode serviceInstanceName endpointName startTime endTime type peer isError layer component "
                "tags { key value } logs { time data { key value } } } } }")

    @classmethod
    def _service_metrics_query(cls, service: str, start: str, end: str) -> str:
        service_name = cls._graphql_escape(service)
        duration = f'start: "{cls._skywalking_time(start)}", end: "{cls._skywalking_time(end)}", step: MINUTE'
        sections = []
        for alias, metric in (("serviceCpm", "service_cpm"), ("serviceRespTime", "service_resp_time"),
                              ("serviceSla", "service_sla"), ("serviceApdex", "service_apdex")):
            sections.append(f'{alias}: readMetricsValues(condition: {{name: "{metric}", entity: '
                            f'{{scope: Service, serviceName: "{service_name}", normal: true}}}}, '
                            f'duration: {{{duration}}}) {{ label values {{ values {{ id value isEmptyValue }} }} }}')
        return "query serviceMetrics { " + " ".join(sections) + " }"

    @classmethod
    def _basic_trace_query(cls, service_id: str, endpoint_id: str, start: str, end: str,
                           trace_state: str, query_order: str) -> str:
        endpoint_clause = f'endpointId: "{cls._graphql_escape(endpoint_id)}",' if endpoint_id else ""
        return ("query basicTraces { queryBasicTraces(condition: {"
                f'serviceId: "{cls._graphql_escape(service_id)}", {endpoint_clause} queryDuration: '
                f'{{start: "{cls._skywalking_time(start)}", end: "{cls._skywalking_time(end)}", step: MINUTE}}, '
                f"traceState: {trace_state}, queryOrder: {query_order}, paging: {{pageNum: 1, pageSize: 10}}"
                "}) { traces { segmentId endpointNames duration start isError traceIds } } }")

    @staticmethod
    def _skywalking_fixture_evidence(fallback: dict[str, Any]) -> dict[str, Any]:
        raw = fallback.get("raw") or {}
        spans = []
        for item in raw.get("spans", []):
            spans.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return {**fallback, "source": "FIXTURE", "summary": "Loaded deterministic SkyWalking fixture evidence.",
                "spans": spans or ["Fixture contains zero trace spans."],
                "rawData": json.dumps(raw, ensure_ascii=False)[:12000],
                "sourceMetadata": {"sourceType": "SKYWALKING", "sourceMode": "FIXTURE_FALLBACK",
                                   "traceId": raw.get("traceId", ""), "endpoint": raw.get("endpoint", ""),
                                   "fixtureFallback": True}}

    def runbooks(self, terms: list[str], limit: int = 4) -> list[dict[str, Any]]:
        base = self.settings.ops_runbook_path
        if not base.exists():
            return []
        scored: list[tuple[int, Path, str]] = []
        normalized = [term.lower() for term in terms if term]
        for path in base.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            haystack = f"{path.stem} {text}".lower()
            score = sum(haystack.count(term) for term in normalized)
            if score:
                scored.append((score, path, text))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [{"name": p.stem, "score": score, "content": text[:5000]} for score, p, text in scored[:limit]]

    def _fixture_or_error(self, service: str, filename: str, exc: Exception, fixture_case_id: str = "") -> dict[str, Any]:
        if self.settings.ops_fixture_fallback:
            candidates: list[Path] = []
            if fixture_case_id and Path("fixtures/incident").exists():
                for directory in Path("fixtures/incident").iterdir():
                    try:
                        case = json.loads((directory / "eval-case.json").read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if fixture_case_id in {directory.name, case.get("caseId")}:
                        candidates.append(directory / filename)
            if not candidates:
                candidates = sorted(Path("fixtures/incident").glob(f"*/{filename}"))
            for path in candidates:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    return {"available": True, "source": "FIXTURE", "fixture": str(path), "raw": raw,
                            "fallbackReason": str(exc), "service": service}
                except Exception:
                    continue
        return {"available": False, "source": "UNAVAILABLE", "error": str(exc), "service": service}
