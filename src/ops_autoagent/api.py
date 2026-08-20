from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from .config import get_settings
from .graphs import CodeOpsGraph, OpsDiagnosisGraph
from .llm import OpenAICompatibleClient
from .schemas import (
    AgentLoopRequest, AlertmanagerWebhook, ApiResponse, ApprovalAction, ApprovalDecision,
    ApprovalDecisionContract, CodeOpsTaskRequest,
    IncidentAnalyzeRequest, IncidentFixRequest, now_iso,
)
from .store import Store
from .observability import redact
from .persistence import CheckpointerManager
from .executors import CallerRunsBoundedExecutor
from .tools import ObservabilityTools
from .ops import (AlertDeduplicator, AlertNormalizer, NotificationService, NotificationTemplateService,
                  OpsDemoDataAutoSeeder, RunbookRagService, SensitiveMasker, ServiceOwnerService)
from .codeops import AgentLoopService, CodeOpsSecurityGovernance, EngineeringToolGateway, IncidentScheduler
from .codeops.evaluation import (EVALUATION_SCORING_SCHEMA_VERSION, build_case_report, build_report,
                                 summary_markdown, write_case_artifacts)
from .codeops.eval_cases import (BASELINE_CASE_SOURCE, BUSINESS_EVAL_LEVEL, EXPANSION_CASE_SOURCE,
                                  builtin_codeops_eval_cases,
                                  runtime_reliability_cases)


settings = get_settings()
store = Store(settings.ops_database_path, settings.mysql_url, settings.mysql_username, settings.mysql_password,
              settings.mysql_pool_min_size, settings.mysql_pool_max_size, settings.mysql_pool_recycle_seconds,
              settings.mysql_pool_connect_timeout_seconds)
llm = OpenAICompatibleClient(settings)
ops_graph = OpsDiagnosisGraph(ObservabilityTools(settings), llm, store)
codeops_graph = CodeOpsGraph(llm, store)
evaluation_state: dict[str, Any] = {"lastReport": None, "schedulerRunning": False}
alert_normalizer = AlertNormalizer()
alert_deduplicator = AlertDeduplicator(store, settings.ops_alert_dedup_window_minutes)
runbook_rag = RunbookRagService(settings)
background_jobs: set[asyncio.Task] = set()
incident_scheduler: IncidentScheduler | None = None
checkpoint_manager = CheckpointerManager(settings)
bounded_executor: CallerRunsBoundedExecutor | None = None
engineering_tool_gateway = EngineeringToolGateway()
agent_loop_service = AgentLoopService(engineering_tool_gateway)
security_governance_service = CodeOpsSecurityGovernance(engineering_tool_gateway.runtime)
service_owner_service = ServiceOwnerService(store)
notification_template = NotificationTemplateService(settings.ops_notify_subject_prefix, settings.ops_notify_app_base_url)
notification_service = NotificationService(store, settings.ops_mail_host, settings.ops_mail_port,
                                           settings.ops_mail_username, settings.ops_mail_password,
                                           settings.ops_mail_auth, settings.ops_mail_starttls,
                                           settings.ops_mail_timeout_seconds)
api_guard_counters: dict[str, tuple[float, int]] = {}
api_guard_lock = asyncio.Lock()
api_guard_masker = SensitiveMasker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global incident_scheduler, ops_graph, codeops_graph, bounded_executor
    bounded_executor = CallerRunsBoundedExecutor(settings.thread_pool_max_size, settings.thread_pool_queue_size)
    asyncio.get_running_loop().set_default_executor(bounded_executor)
    await store.initialize()
    checkpointer = await checkpoint_manager.start()
    ops_graph = OpsDiagnosisGraph(ObservabilityTools(settings), llm, store, checkpointer)
    codeops_graph = CodeOpsGraph(llm, store, checkpointer)
    await _reconcile_waiting_approvals()
    if settings.ops_runbook_vector_enabled:
        try:
            await runbook_rag.index()
        except Exception:
            if settings.ops_runbook_vector_fail_fast:
                raise
    incident_scheduler = IncidentScheduler(
        _dispatch_scheduled_incident, max_concurrent=settings.codeops_scheduler_max_concurrent,
        max_per_service=settings.codeops_scheduler_max_per_service,
        queue_file=settings.ops_database_path.parent / "incident-queue" / "queue.json",
    )
    await incident_scheduler.start()
    if settings.ops_demo_auto_seed_enabled:
        seed_job = asyncio.create_task(OpsDemoDataAutoSeeder(settings).seed())
        background_jobs.add(seed_job)
        seed_job.add_done_callback(background_jobs.discard)
    try:
        yield
    finally:
        if incident_scheduler.running:
            await incident_scheduler.stop()
        for job in tuple(background_jobs):
            job.cancel()
        if background_jobs:
            await asyncio.gather(*tuple(background_jobs), return_exceptions=True)
        background_jobs.clear()
        await store.close()
        await checkpoint_manager.close()
        bounded_executor.shutdown(wait=True, cancel_futures=True)
        bounded_executor = None


app = FastAPI(title="Ops AutoAgent", version="2.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _ops_error_event(message: str, session_id: str) -> dict[str, Any]:
    return {"type": "error", "subType": "diagnosis_error", "content": message, "completed": True,
            "timestamp": int(time.time() * 1000), "sessionId": session_id}


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    message = _validation_message(exc)
    if request.url.path == "/api/v1/ops/incident/analyze":
        session_id = str(uuid.uuid4())
        event = _ops_error_event(api_guard_masker.mask(message), session_id)
        return StreamingResponse(iter([f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"]),
                                 media_type="text/event-stream")
    return JSONResponse(status_code=200, content={"code": "0002", "info": message, "data": None})


def _validation_message(exc: RequestValidationError) -> str:
    error = exc.errors()[0] if exc.errors() else {}
    field = str(error.get("loc", ["request"])[-1])
    aliases = {"service_name": "serviceName", "start_time": "startTime", "end_time": "endTime",
               "trace_id": "traceId", "max_step": "maxStep", "task_type": "taskType",
               "max_rounds": "maxRounds", "max_tool_calls": "maxToolCalls"}
    field = aliases.get(field, field)
    kind = str(error.get("type", ""))
    if kind in {"missing", "string_too_short", "value_error"}:
        return f"{field} cannot be blank"
    if field == "problem" and kind == "string_too_long":
        limit = error.get("ctx", {}).get("max_length", 2000)
        return f"problem length must be <= {limit}"
    if field == "goal" and kind == "string_too_long":
        return "goal length must be <= 4000"
    if field == "maxStep":
        return "maxStep must be between 1 and 10"
    if field == "maxRounds":
        return "maxRounds must be between 1 and 12"
    if field == "maxToolCalls":
        return "maxToolCalls must be between 1 and 50"
    return f"invalid parameter: {field}"


def ok(data: Any = None) -> ApiResponse:
    return ApiResponse(data=data)


def fail(info: str, code: str = "0001") -> JSONResponse:
    return JSONResponse(status_code=200, content={"code": code, "info": info, "data": None})


async def _reconcile_waiting_approvals() -> list[dict[str, Any]]:
    """Reconcile API projections without making a decision on behalf of an operator."""
    findings: list[dict[str, Any]] = []
    for task in await store.find("tasks", lambda item: item.get("status") == "WAITING_APPROVAL"):
        task_id = str(task.get("taskId") or "")
        approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
        checkpoint = await codeops_graph.checkpoint_summary(task_id)
        reasons: list[str] = []
        if not checkpoint.get("checkpointPresent"):
            reasons.append("CHECKPOINT_MISSING")
        if checkpoint.get("approvalId") != approval.get("approvalId"):
            reasons.append("APPROVAL_PROJECTION_MISMATCH")
        if not checkpoint.get("interruptPending"):
            reasons.append("INTERRUPT_NOT_PENDING")
        if reasons:
            finding = {"taskId": task_id, "status": "RECONCILIATION_REQUIRED", "reasons": reasons,
                       "checkpoint": checkpoint, "checkedAt": now_iso()}
            task["reconciliation"] = finding
            task["updateTime"] = finding["checkedAt"]
            await store.put("tasks", task_id, task, task["updateTime"])
            await store.put("audit_logs", f"reconcile-{task_id}", {
                "auditId": f"reconcile-{task_id}", "taskId": task_id, "action": "APPROVAL_RECONCILE",
                "result": "REQUIRES_REVIEW", "reason": "; ".join(reasons),
                "checkpoint": checkpoint, "createTime": finding["checkedAt"]}, finding["checkedAt"])
            findings.append(finding)
    return findings


@app.middleware("http")
async def api_guard(request: Request, call_next):
    path = request.url.path
    guarded = path in {"/api/v1/ops/incident/analyze", "/api/v1/ops/alert/webhook/alertmanager"} or path.startswith(
        "/api/v1/ops/incident/record/")
    action, resource, service_name = _guard_request_metadata(path, {})
    body: dict[str, Any] = {}
    if guarded and request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = json.loads((await request.body()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        action, resource, service_name = _guard_request_metadata(path, body)
    if path == "/api/v1/ops/incident/analyze" and _invalid_analyze_body(body):
        return await call_next(request)
    reason = ""
    if guarded and settings.ops_security_enabled:
        if not settings.ops_api_token.strip():
            reason = "ops security is enabled, but ops.security.api-token is blank"
        elif request.headers.get("X-Ops-Token") != settings.ops_api_token:
            reason = "invalid ops api token"
    rate_limited_path = path in {
        "/api/v1/ops/incident/analyze", "/api/v1/ops/alert/webhook/alertmanager",
    }
    if not reason and guarded and rate_limited_path and settings.ops_rate_limit_enabled:
        client_ip = _client_ip(request)
        key = f"{client_ip}:{service_name or 'unknown'}"
        now = time.monotonic()
        window = max(1, settings.ops_rate_limit_window_seconds)
        async with api_guard_lock:
            started, count = api_guard_counters.get(key, (now, 0))
            if now - started >= window:
                started, count = now, 0
            count += 1
            api_guard_counters[key] = (started, count)
        limit = max(1, settings.ops_rate_limit_max_requests)
        if count > limit:
            reason = f"rate limit exceeded, max {limit} requests per {window} seconds"
    session_id = request.headers.get("X-Session-Id")
    await _save_guard_audit(request, session_id, action, resource, body, "DENY" if reason else "ALLOW",
                            reason or "request accepted")
    if reason:
        if path == "/api/v1/ops/incident/analyze":
            event = _ops_error_event(api_guard_masker.mask(reason), session_id or str(uuid.uuid4()))
            return StreamingResponse(iter([f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"]),
                                     media_type="text/event-stream")
        return JSONResponse(status_code=200, content={"code": "0002", "info": reason, "data": None})
    return await call_next(request)


def _invalid_analyze_body(body: dict[str, Any]) -> bool:
    if not isinstance(body, dict):
        return True
    if any(not isinstance(body.get(key), str) or not body.get(key, "").strip()
           for key in ("serviceName", "startTime", "endTime", "problem")):
        return True
    if len(body["problem"]) > 2000:
        return True
    step = body.get("maxStep")
    return step is not None and (not isinstance(step, int) or step < 1 or step > 10)


def _guard_request_metadata(path: str, body: dict[str, Any]) -> tuple[str, str, str]:
    if path == "/api/v1/ops/incident/analyze":
        service = str(body.get("serviceName") or "unknown")
        return "ANALYZE_INCIDENT", f"ops-incident:{service}", service
    if path == "/api/v1/ops/alert/webhook/alertmanager":
        labels = body.get("commonLabels") or {}
        alerts = body.get("alerts") or []
        if not labels and alerts and isinstance(alerts[0], dict):
            labels = alerts[0].get("labels") or {}
        service = next((str(labels.get(key)) for key in ("serviceName", "service", "application", "app", "job")
                        if labels.get(key)), "unknown")
        return "RECEIVE_ALERT_WEBHOOK", f"ops-alert-webhook:{service}", service
    prefix = "/api/v1/ops/incident/record/"
    if path.startswith(prefix):
        diagnosis_id = path.removeprefix(prefix)
        return "QUERY_DIAGNOSIS_RECORD", f"ops-incident-record:{diagnosis_id}", ""
    return request_action(path), path, ""


def request_action(path: str) -> str:
    return "OPS_API_REQUEST:" + path.upper().replace("/", "_").strip("_")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    return forwarded or request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")


async def _save_guard_audit(request: Request, session_id: str | None, action: str, resource: str,
                            body: dict[str, Any], result: str, reason: str) -> None:
    record = {
        "auditId": f"audit-{uuid.uuid4()}", "sessionId": session_id, "diagnosisId": None,
        "operatorId": request.headers.get("X-Ops-User") or "anonymous", "clientIp": _client_ip(request),
        "action": action, "resource": resource,
        "requestJson": json.dumps(api_guard_masker.sanitize(body), ensure_ascii=False)[:6000],
        "result": result, "reason": api_guard_masker.mask(reason)[:1000], "createTime": now_iso(),
    }
    try:
        await store.put("audit_logs", record["auditId"], record, record["createTime"])
    except Exception:
        # Auditing is deliberately non-blocking, matching the legacy guard's fail-open audit sink.
        pass


@app.get("/actuator/health")
async def health() -> dict[str, Any]:
    return {"status": "UP", "components": {"langgraph": {"status": "UP"}, "database": {"status": "UP"}}}


@app.get("/actuator/info")
async def info() -> dict[str, Any]:
    return {"app": "ops-autoagent-diagnosis", "runtime": "python-langgraph", "version": "2.0.0"}


@app.get("/actuator/prometheus")
async def prometheus() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/ops/incident/analyze")
async def analyze_incident(body: IncidentAnalyzeRequest) -> StreamingResponse:
    initial = ops_graph.create_state(body)

    async def stream() -> AsyncIterator[str]:
        async for event, final in ops_graph.stream(initial):
            if final is not None:
                record = _diagnosis_record(final)
                await store.put("diagnoses", final["diagnosis_id"], record, now_iso())
            if event is not None:
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
                await asyncio.sleep(0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.get("/api/v1/ops/incident/record/{diagnosis_id}")
async def diagnosis_record(diagnosis_id: str) -> Any:
    record = await store.get("diagnoses", diagnosis_id)
    return ok(record) if record else fail("diagnosis record not found")


@app.post("/api/v1/ops/alert/webhook/alertmanager")
async def alertmanager(body: AlertmanagerWebhook) -> ApiResponse:
    if not body.alerts:
        return fail("alert webhook payload cannot be empty", "0002")
    alerts = alert_normalizer.normalize(body)
    accepted, skipped = 0, len(body.alerts) - len(alerts)
    for alert, raw_alert in zip(alerts, body.alerts, strict=True):
        decision = await alert_deduplicator.accept(alert)
        if decision["accepted"]:
            accepted += 1
            alert["dedupKey"] = decision["dedupKey"]
            command = _alert_incident_command(alert)
            created = now_iso()
            dispatch = {"dispatchId": f"dispatch-{uuid.uuid4()}", "eventId": alert["alertId"],
                        "diagnosisId": command["diagnosisId"], "serviceName": alert["serviceName"],
                        "dedupKey": decision["dedupKey"], "dispatchStatus": "NEW",
                        "createTime": created, "updateTime": created}
            await store.put("dispatches", dispatch["dispatchId"], dispatch, dispatch["updateTime"])
            for coroutine in (_dispatch_alert(alert, dispatch, command), _trigger_codeops_alert(alert, command)):
                job = asyncio.create_task(coroutine)
                background_jobs.add(job)
                job.add_done_callback(background_jobs.discard)
        else:
            skipped += 1
            dispatch = {"dispatchId": f"dispatch-{uuid.uuid4()}", "eventId": alert["alertId"],
                        "serviceName": alert["serviceName"], "dedupKey": decision["dedupKey"],
                        "dispatchStatus": "SKIPPED", "skipReason": decision["reason"],
                        "createTime": now_iso(), "endTime": now_iso(), "updateTime": now_iso()}
            await store.put("dispatches", dispatch["dispatchId"], dispatch, dispatch["updateTime"])
        raw_labels, raw_annotations = raw_alert.labels or {}, raw_alert.annotations or {}
        if incident_scheduler and raw_alert.fingerprint is not None:
            await incident_scheduler.ingest(
                raw_alert.fingerprint, raw_labels.get("alertname", "unknown"),
                raw_labels.get("service", "unknown"), raw_labels.get("severity", "warning"),
                raw_annotations.get("summary", raw_annotations.get("description", "")),
                raw_labels.get("endpoint", ""))
    return ok({"totalAlerts": len(body.alerts), "acceptedCount": accepted,
               "skippedCount": skipped, "message": "alert webhook accepted"})


@app.post("/api/v1/ops/alert/webhook/alertmanager/incident-to-fix/verify")
async def verify_alert_to_fix(body: AlertmanagerWebhook) -> ApiResponse:
    if not body.alerts:
        return fail("alert webhook payload cannot be empty", "0002")
    alerts = alert_normalizer.normalize(body)
    if not alerts:
        return fail("no firing alert found in payload", "0002")
    if not settings.codeops_incident_to_fix_alert_enabled:
        return fail("Incident-to-Fix alert trigger is disabled")
    alert = alerts[0]
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    repository = (labels.get("repository") or labels.get("repo") or labels.get("code_repository")
                  or annotations.get("repository") or annotations.get("repo")
                  or annotations.get("code_repository") or "")
    goal = (f"{alert['serviceName']} 触发线上告警 [{alert['alertName']}]，问题描述：{alert['summary']}。"
            "请完成 Incident-to-Fix：诊断线上证据，抽取异常类名/接口路径/可疑 Service，定位代码，"
            "生成修复补丁草稿、测试验证建议和发布风险观察项。")
    request = CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX", goal=goal, repository=repository,
        focusAreas=["incident", "code_location", "knowledge_rag", "bug_fix", "test_verification", "release_risk"],
        context={**alert, "source": "alertmanager", "evidenceMode": "LIVE",
                 "fixtureFallbackAllowed": False, "allowPatchApply": str(labels.get(
                     "codeops.allowPatchApply", annotations.get("codeops.allowPatchApply", "true"))).lower() == "true",
                 "allowTestPatchApply": str(labels.get(
                     "codeops.allowTestPatchApply", annotations.get("codeops.allowTestPatchApply", "true"))).lower() == "true",
                 "alertmanagerPayload": alert.get("rawPayload"), "alertLabels": labels,
                 "alertAnnotations": annotations}, maxRounds=8, maxToolCalls=50)
    state = await codeops_graph.invoke(request)
    task = state["task"]
    await store.put("tasks", task["taskId"], task, task["updateTime"])
    return ApiResponse(info="real alertmanager incident-to-fix chain executed", data=_incident_fix_view(task))


@app.post("/api/v1/ops/verify/full-chain")
async def verify_full_chain(body: dict[str, Any] | None = None) -> ApiResponse:
    body = body or {}
    now = datetime.now()
    service = str(body.get("serviceName") or "ops-demo-service")
    start = str(body.get("startTime") or (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"))
    end = str(body.get("endTime") or (now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))
    problem = str(body.get("problem") or
                  "Verify real Prometheus, ELK, SkyWalking, and PgVector reads for production incident diagnosis.")
    metrics, logs, traces, pg_matches = await asyncio.gather(
        ops_graph.tools.prometheus(service, start, end, endpoint=str(body.get("endpoint") or ""), problem=problem),
        ops_graph.tools.elk(service, start, end, problem),
        ops_graph.tools.skywalking(service, str(body.get("traceId") or ""), start, end, problem=problem),
        runbook_rag.search(f"{service} {problem} database connection pool Dubbo RPC timeout", 4),
    )
    observations = metrics.get("observations") if isinstance(metrics.get("observations"), list) else []
    samples = logs.get("errorSamples") if isinstance(logs.get("errorSamples"), list) else []
    spans = traces.get("spans") if isinstance(traces.get("spans"), list) else []
    trace_raw = str(traces.get("rawData") or json.dumps(traces.get("raw", {}), ensure_ascii=False))
    result = {"diagnosisId": f"verify-{uuid.uuid4()}", "sessionId": str(uuid.uuid4()),
              "serviceName": service, "window": f"{start} ~ {end}",
              "prometheus": {"sourceReachable": metrics.get("available") is True,
                  "hasMetricSeries": any(str(item).startswith(("OK:", "ANOMALY:")) for item in observations),
                  "hasAnomaly": any(str(item).startswith("ANOMALY:") for item in observations),
                  "summary": metrics.get("summary", ""), "observations": observations},
              "elk": {"sourceReachable": logs.get("available") is True,
                  "hasIncidentSamples": any("zero matching" not in str(item).lower() for item in samples),
                  "summary": logs.get("summary", ""), "samples": samples},
              "skywalking": {"sourceReachable": traces.get("available") is True,
                  "hasTraceData": any(key.lower() in trace_raw.lower() for key in
                                      ("queryBasicTraces", "queryTrace", "traces", "spans")),
                  "hasErrorOrSlowTraceSignal": any(key.lower() in trace_raw.lower() for key in
                                                   ('"isError":true', '"isError": true', "duration")),
                  "summary": traces.get("summary", ""), "spans": spans},
              "pgvector": {"sourceReachable": any(str(item.get("path") or "").startswith("pgvector:")
                                                    for item in pg_matches),
                           "hasRunbookMatches": bool(pg_matches), "matches": pg_matches}}
    result["overallReady"] = all(result[key]["sourceReachable"] for key in ("prometheus", "elk", "skywalking", "pgvector"))
    return ok(result)


@app.get("/api/v1/ops/mock/health")
async def mock_health() -> dict[str, Any]:
    return {"status": "OK", "mode": "health", "costMillis": 0, "time": now_iso()}


@app.get("/api/v1/ops/mock/environment")
async def mock_environment() -> dict[str, Any]:
    mysql, pgvector, prometheus, elk, skywalking = await asyncio.gather(
        _check_mysql(), _check_pgvector(), _check_http_environment("prometheus"),
        _check_http_environment("elk"), _check_http_environment("skywalking"))
    return {"time": now_iso(), "app": "UP", "mysql": mysql, "pgvector": pgvector,
            "prometheus": prometheus, "elk": elk, "skywalking": skywalking}


@app.get("/api/v1/ops/mock/order/create")
async def mock_order_create(mode: str = "normal", sleepMillis: int = 1200, holdSeconds: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    normalized = (mode or "normal").strip().lower()
    if normalized == "error":
        raise HTTPException(500, "Mock order create failed: database connection timeout")
    if normalized == "slow":
        await asyncio.sleep(max(100, min(sleepMillis, 10000)) / 1000)
    elif normalized == "db":
        await _hold_mysql(max(1, min(holdSeconds, 30)))
    elif normalized != "normal":
        raise HTTPException(400, "Unsupported mock mode. Use normal, error, slow, or db.")
    return {"status": "OK", "mode": normalized, "costMillis": int((time.perf_counter() - started) * 1000),
            "time": now_iso()}


async def _hold_mysql(seconds: int) -> None:
    if not store._pool:
        raise HTTPException(500, "MySQL datasource is not configured")
    async with store._pool.acquire() as connection, connection.cursor() as cursor:
        await cursor.execute("SELECT SLEEP(%s)", (seconds,))


async def _check_mysql() -> dict[str, Any]:
    if not store._pool:
        return {"status": "DOWN", "error": "MySQL datasource is not configured"}
    try:
        async with store._pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            return {"status": "UP" if await cursor.fetchone() else "UNKNOWN"}
    except Exception as exc:
        return {"status": "DOWN", "error": str(exc)}


async def _check_pgvector() -> dict[str, Any]:
    if not settings.pgvector_url:
        return {"status": "NOT_CONFIGURED"}
    try:
        import asyncpg
        connection = await asyncpg.connect(settings.pgvector_url, user=settings.pgvector_username,
                                           password=settings.pgvector_password, timeout=3)
        try:
            rows = await connection.fetchval(f"SELECT COUNT(1) FROM {settings.pgvector_table}")
        finally:
            await connection.close()
        return {"status": "UP", "runbookVectorRows": rows}
    except Exception as exc:
        return {"status": "DOWN", "error": str(exc)}


async def _check_http_environment(kind: str) -> dict[str, Any]:
    url = {"prometheus": settings.prometheus_base_url, "elk": settings.elk_base_url,
           "skywalking": settings.skywalking_graphql_url}[kind]
    if not url:
        return {"status": "NOT_CONFIGURED"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2)) as client:
            if kind == "prometheus":
                health = await client.get(url.rstrip("/") + "/-/healthy")
                targets = await client.get(url.rstrip("/") + "/api/v1/targets", params={"state": "active"})
                return {"status": "UP" if health.status_code == 200 else "DOWN",
                        "healthCode": health.status_code, "targetCode": targets.status_code,
                        "appTargetVisible": "ops-demo-service" in targets.text and '\"health\":\"up\"' in targets.text}
            if kind == "elk":
                response = await client.get(url.rstrip("/") + "/_cluster/health")
                return {"status": "UP" if response.status_code < 300 else "DOWN",
                        "healthCode": response.status_code}
            response = await client.post(url, json={"query": "query { version }"})
            return {"status": "UP" if response.status_code < 500 else "DOWN", "graphqlCode": response.status_code}
    except Exception as exc:
        return {"status": "DOWN", "error": str(exc)}


def _task_dto(task: dict[str, Any]) -> dict[str, Any]:
    return {"taskId": task.get("taskId"), "taskType": task.get("taskType"), "goal": task.get("goal"),
            "repository": task.get("repository"), "changeRef": task.get("changeRef"),
            "status": task.get("status"), "maxRounds": task.get("maxRounds"),
            "maxToolCalls": task.get("maxToolCalls"), "finalSummary": task.get("finalSummary"),
            "steps": [{"stepNo": step.get("stepNo"), "decision": step.get("decision"),
                       "selectedSkill": step.get("selectedSkill"), "reason": step.get("reason"),
                       "expectedEvidence": step.get("expectedEvidence"),
                       "resultSummary": step.get("resultSummary"),
                       "rawEvidenceJson": step.get("rawEvidenceJson"), "status": step.get("status")}
                      for step in task.get("steps", [])],
            "createTime": task.get("createTime"), "updateTime": task.get("updateTime")}


def _build_task_trace(task: dict[str, Any]) -> dict[str, Any]:
    timeline = []
    for step in task.get("steps", []):
        raw = _json(step.get("rawEvidenceJson"))
        safe_raw = _safe_trace_payload(raw)
        timeline.append({"stepNo": step.get("stepNo"), "skillId": step.get("selectedSkill"),
                         "decision": step.get("decision"), "status": step.get("status"),
                         "reason": step.get("reason"), "summary": step.get("resultSummary"),
                         "evidence": step.get("expectedEvidence", []), "phase": safe_raw.get("phase"),
                         "artifactId": safe_raw.get("artifactId"),
                         "highlights": _trace_highlights(step.get("selectedSkill"), safe_raw),
                         "rawEvidence": safe_raw})
    return {"taskId": task.get("taskId"), "threadId": task.get("threadId", task.get("taskId")),
            "runId": task.get("runId", ""), "taskType": task.get("taskType"), "goal": task.get("goal"),
            "status": task.get("status"), "finalSummary": task.get("finalSummary"),
            "repairAttempt": task.get("repairAttempt", 0), "reviewVerdict": task.get("reviewVerdict", ""),
            "blockedReason": task.get("blockedReason", ""), "approvalId": (task.get("approval") or {}).get("approvalId"),
            "stepCount": len(timeline), "usedToolCalls": task.get("usedToolCalls"),
            "workingMemorySummary": _working_memory_summary(task), "timeline": timeline}


def _safe_trace_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep dashboard/trace payloads summary-only; full artifacts stay in Store."""
    blocked = {"prompt", "rawContent", "patchDraft", "testPatchDraft", "codeSnippets", "codeSearchMatches",
               "output", "response", "request", "trace", "agentLoopTrace"}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key in blocked:
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:24]
            result[key] = {"redacted": True, "artifactId": "artifact-" + digest}
        else:
            result[key] = redact(value)
    return result


def _trace_highlights(skill_id: str | None, raw: dict[str, Any]) -> dict[str, Any]:
    keys = ("changedFiles", "relatedTestFiles", "codeHints", "evidenceGraphSummary",
            "evidenceGraphRankedCodeNodes", "preLoopCodeContextPack", "localizationDecision",
            "codeLocalization", "localizationQuality", "localizationReflection",
            "localizationReflectionRequired", "localizationBlocking", "codeSearchMatches", "findings",
            "baselineFindings", "knowledgeMatches", "llmReviewSuccess", "llmReviewFallback",
            "llmReviewError", "opsDiagnosis", "patchDraft", "repairPlan", "exactReplaceBlocks",
            "exactReplaceApply", "failureDiagnostic", "repairObservations", "patchAttempts",
            "patchValidation", "patchApply", "rootCause", "confidence", "llmGenerated",
            "llmErrorMessage", "codeSnippets", "mavenCommands", "recommendedTests", "coverageGaps",
            "testExecutionResults", "testExecutionAsync", "queuedBackgroundTasks", "backgroundToolTasks",
            "taskNotifications", "testPatchGenerated", "testPatchTargetFiles", "testPatchDraft",
            "testPatchValidation", "testPatchApply", "riskPoints", "codeReview", "reviewVerdict",
            "qualityScore", "patchDecision", "observationMetrics", "rollbackConcerns",
            "manualTakeoverRequired", "autoPatchBlockedReason", "verificationBlockedReason",
            "blockedAutomationSummary", "agentRuntime", "toolRuntime")
    result = {key: raw[key] for key in keys if raw.get(key) is not None}
    if skill_id == "agent_loop_investigation":
        aliases = {"agentLoopFinalAnswer": "finalAnswer", "agentLoopTurns": "turns",
                   "agentLoopTrace": "trace", "agentLoopStopReason": "stopReason"}
        result.update({target: raw[source] for target, source in aliases.items() if raw.get(source) is not None})
        for key in ("targetFiles", "targetMethods", "rootCauseCandidateFiles", "directEvidenceFiles",
                    "candidateScope", "recommendedTests", "fixStrategy", "scopeDecisionType",
                    "rootCauseLocationType", "strategyType", "localizationConfidence"):
            if raw.get(key) is not None:
                result[key] = raw[key]
    result["skillId"] = skill_id
    return result


def _working_memory_summary(task: dict[str, Any]) -> dict[str, Any]:
    context = task.get("context") or {}
    memory = context.get("incidentFixWorkingMemory")
    summary: dict[str, Any] = {}
    if isinstance(memory, dict):
        for key in ("incidentSummary", "codeHints", "rootCauseAnalysis", "agentTrace"):
            if memory.get(key) is not None:
                summary[key] = memory[key]
        compact = {"codeLocalization": ("localizationConfidence", "targetFiles", "targetMethods",
                    "suspiciousLocations", "missingEvidence", "evidenceGraphSummary",
                    "evidenceGraphRankedCodeNodes", "finalAnswer", "turns", "trace", "recommendedTests",
                    "strategyType"), "patchGeneration": ("rootCause", "confidence", "targetFiles",
                    "llmGenerated", "patchValidation"), "testVerification": ("recommendedTests",
                    "coverageGaps", "mavenCommands", "testExecutionAsync", "testExecutionResults",
                    "queuedBackgroundTasks", "backgroundToolTasks", "taskNotifications"),
                    "releaseRisk": ("codeReview", "reviewVerdict", "qualityScore", "patchDecision",
                    "patchFacts", "releaseRiskReport", "humanApprovalPoints", "releaseRiskReasoning",
                    "manualTakeoverRequired", "autoPatchBlockedReason", "verificationBlockedReason",
                    "blockedAutomationSummary")}
        for name, keys in compact.items():
            source = memory.get(name)
            if isinstance(source, dict) and source:
                summary[name] = {key: source[key] for key in keys if source.get(key) is not None}
    for key in ("taskDagNodes", "backgroundToolTasks", "taskNotifications", "agentRuntimeTrace",
                "toolRuntimeTrace", "repairObservations", "patchAttempts"):
        if context.get(key) is not None:
            summary[key] = context[key]
    return summary


_INCIDENT_STAGES = (
    ("ops_evidence", "线上证据采集", ("ops_diagnosis",)),
    ("code_localization", "代码定位", ("agent_loop_investigation", "repo_understanding")),
    ("knowledge_rag", "知识检索", ("engineering_knowledge_rag",)),
    ("code_repair", "代码修复", ("bug_fix",)),
    ("test_verification", "编译测试", ("test_verification",)),
    ("release_risk", "发布风险", ("release_risk_analysis",)),
    ("human_approval", "人工审批", ()),
)


def _incident_fix_view(task: dict[str, Any]) -> dict[str, Any]:
    context, approval = task.get("context") or {}, task.get("approval") or {}
    latest = {step.get("selectedSkill"): step for step in task.get("steps", []) if step.get("selectedSkill")}
    stages = []
    for stage_id, stage_name, skills in _INCIDENT_STAGES:
        if stage_id == "human_approval":
            approval_status = str(approval.get("status") or "NOT_REQUIRED")
            status = {"PENDING": "WAITING_APPROVAL", "APPROVED": "SUCCESS", "REJECTED": "FAILED"}.get(
                approval_status.upper(), "SKIPPED")
            stages.append({"stageId": stage_id, "stageName": stage_name, "status": status,
                           "skillIds": [], "stepNo": None,
                           "summary": f"审批状态：{approval_status}" if approval else "当前任务没有进入人工审批",
                           "evidence": approval.get("approvalReasons", []) if approval else [],
                           "keyArtifacts": approval})
            continue
        step = next((latest[skill] for skill in skills if skill in latest), None)
        raw = _json(step.get("rawEvidenceJson")) if step else {}
        raw_status = str(step.get("status") or "") if step else ""
        status = "PENDING" if not step else ({"STOPPED": "SKIPPED"}.get(raw_status.upper(), raw_status or "UNKNOWN"))
        stages.append({"stageId": stage_id, "stageName": stage_name, "status": status,
                       "skillIds": list(skills), "stepNo": step.get("stepNo") if step else None,
                       "summary": step.get("resultSummary", "") if step else "",
                       "evidence": step.get("expectedEvidence", []) if step else [],
                       "keyArtifacts": _stage_artifacts(stage_id, raw)})
    if approval and str(approval.get("status", "")).upper() == "PENDING":
        current_stage = "human_approval"
    else:
        current_stage = next((stage["stageId"] for stage in stages
                              if stage["status"].upper() in {"FAILED", "WAITING_APPROVAL", "PENDING"}),
                             "failed" if str(task.get("status", "")).upper() == "FAILED" else "completed")
    finished = sum(stage["status"].upper() in {"SUCCESS", "SKIPPED"} for stage in stages)
    progress = 100 if str(task.get("status", "")).upper() == "COMPLETED" else min(99, round(finished * 100 / len(stages)))
    incident_keys = ("source", "eventId", "opsDiagnosisId", "serviceName", "alertRule", "severity",
                     "traceId", "startTime", "endTime", "evidenceMode", "fixtureFallbackAllowed")
    incident = {key: context[key] for key in incident_keys if context.get(key) is not None}
    guardrails = context.get("guardrailSummary") if isinstance(context.get("guardrailSummary"), dict) else {}
    merged_raw: dict[str, Any] = {}
    for step in task.get("steps", []):
        merged_raw.update(_json(step.get("rawEvidenceJson")))
    artifacts = {"evidence": _pick(merged_raw, "evidenceCoverage", "evidenceProvenance", "evidenceSources", "evidenceDetails"),
                 "localization": _stage_artifacts("code_localization", merged_raw),
                 "patch": _stage_artifacts("code_repair", merged_raw),
                 "tests": _stage_artifacts("test_verification", merged_raw),
                 "releaseRisk": _stage_artifacts("release_risk", merged_raw), "guardrails": guardrails}
    return {"taskId": task.get("taskId"), "status": task.get("status"), "currentStage": current_stage,
            "progressPercent": progress, "requiresApproval": bool(approval) and str(approval.get("status", "")).upper() == "PENDING",
            "approvalStatus": str(approval.get("status")) if approval else "NOT_REQUIRED_OR_NOT_SUBMITTED",
            "goal": task.get("goal"), "repository": task.get("repository"), "finalSummary": task.get("finalSummary"),
            "incident": incident, "guardrails": guardrails, "approval": approval, "artifacts": artifacts,
            "stages": stages, "trace": _build_task_trace(task)}


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if source.get(key) is not None}


def _stage_artifacts(stage_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    keys = {"ops_evidence": ("evidenceCoverage", "evidenceProvenance", "evidenceSources", "rootCause", "confidence", "traceId"),
            "code_localization": ("targetFiles", "targetMethods", "suspiciousLocations", "localizationConfidence",
                "codeSearchMatches", "finalAnswer", "turns", "trace", "recommendedTests", "strategyType",
                "fixStrategy", "scopeDecisionType", "rootCauseLocationType", "directEvidenceFiles", "relatedFiles",
                "rootCauseCandidateFiles", "doNotModifyFiles", "candidateScope", "localizationDecision",
                "codeLocalization", "localizationQuality", "localizationReflection", "localizationReflectionRequired",
                "localizationBlocking", "preLoopCodeContextPack", "stopReason", "evidenceGraphSummary",
                "evidenceGraphRankedCodeNodes", "evidenceGraph"),
            "knowledge_rag": ("knowledgeMatches", "runbookMatches"),
            "code_repair": ("repairPlan", "llmGenerated", "patchGenerated", "rootCause", "exactReplaceBlocks",
                "exactReplaceApply", "patchApply", "patchScopeGuard", "patchSandbox", "patchQuality", "compileGate",
                "failureDiagnostic", "repairObservations", "patchAttempts"),
            "test_verification": ("recommendedTests", "mavenCommands", "testExecutionResults", "testExecutionAsync",
                "queuedBackgroundTasks", "backgroundToolTasks", "taskNotifications", "testFailureType",
                "failedTestFiles", "repairObservations"),
            "release_risk": ("releaseRiskReport", "humanApprovalPoints", "releaseRiskReasoning", "codeReview",
                "reviewVerdict", "qualityScore", "patchDecision", "patchFacts", "modelRouting",
                "manualTakeoverRequired", "autoPatchBlockedReason", "verificationBlockedReason",
                "blockedAutomationSummary", "repairObservations", "patchAttempts")}
    return _pick(raw, *keys.get(stage_id, ()))


@app.post("/api/v1/codeops/task/submit")
async def submit_task(body: CodeOpsTaskRequest) -> ApiResponse:
    result = await codeops_graph.invoke(body)
    task = result["task"]
    await store.put("tasks", task["taskId"], task, task["updateTime"])
    return ok(_task_dto(task))


@app.post("/api/v1/codeops/task/incident/submit")
async def submit_incident_fix(body: IncidentFixRequest) -> ApiResponse:
    context = dict(body.context or {})
    context["source"] = str(context.get("source") or "codeops_incident_api")
    context["evidenceMode"] = str(context.get("evidenceMode") or "LIVE")
    for key, value in (("serviceName", body.service_name), ("alertRule", body.alert_rule),
                       ("severity", body.severity), ("problem", body.problem), ("endpoint", body.endpoint),
                       ("traceId", body.trace_id), ("startTime", body.start_time), ("endTime", body.end_time),
                       ("repository", body.repository)):
        if value is not None:
            context[key] = value
    context.update({"allowPatchApply": body.allow_patch_apply is not False,
                    "allowTestPatchApply": body.allow_test_patch_apply is not False,
                    "fixtureFallbackAllowed": body.fixture_fallback_allowed is True,
                    "alertLabels": body.labels or {}, "alertAnnotations": body.annotations or {}})
    endpoint = f"，接口：{body.endpoint}" if body.endpoint and body.endpoint.strip() else ""
    goal = (f"{body.service_name or 'unknown-service'} 触发线上告警 "
            f"[{body.alert_rule or 'unknown-alert'}]，级别：{body.severity or 'UNKNOWN'}{endpoint}，"
            f"问题描述：{body.problem or '线上异常待诊断'}。请完成 Incident-to-Fix：采集可观测证据、定位代码、"
            "判断是否需要修复、生成最小补丁、编译测试验证并输出发布风险。")
    focus = body.focus_areas or ["incident", "code_location", "knowledge_rag", "bug_fix",
                                "test_verification", "release_risk"]
    request = CodeOpsTaskRequest(taskType="INCIDENT_TO_FIX", goal=goal, repository=body.repository,
                                 changeRef=body.change_ref, focusAreas=focus, context=context,
                                 maxRounds=body.max_rounds, maxToolCalls=body.max_tool_calls)
    result = await codeops_graph.invoke(request)
    task = result["task"]
    await store.put("tasks", task["taskId"], task, task["updateTime"])
    return ok(_incident_fix_view(task))


@app.get("/api/v1/codeops/task/{task_id}")
async def query_task(task_id: str) -> Any:
    if task_id == "skills":
        return ok(CodeOpsGraph.SKILLS)
    task = await store.get("tasks", task_id)
    return ok(_task_dto(task)) if task else fail("CodeOps task not found")


@app.get("/api/v1/codeops/task/{task_id}/trace")
async def task_trace(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    if not task:
        return fail("CodeOps task not found")
    return ok(_build_task_trace(task))


@app.get("/api/v1/codeops/task/{task_id}/observability")
async def task_observability(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    if not task:
        return fail("CodeOps task not found")
    metrics = [item for item in await store.recent("runtime_metrics", 10000)
               if str(item.get("taskId") or "") == task_id]
    artifacts = [item for item in await store.recent("artifacts", 10000)
                 if str(item.get("taskId") or "") == task_id]
    metric_values = {str(item.get("metricName")): item.get("value") for item in metrics}
    approval = task.get("approval") or {}
    report = {
        "businessEffect": {"status": task.get("status"), "reviewVerdict": task.get("reviewVerdict", ""),
                            "patchDigest": task.get("patchDigest", ""),
                            "testStatus": (task.get("context") or {}).get("verificationPassed")},
        "runtimeSafety": {"unauthorizedTargetRepositoryWrites": metric_values.get("unauthorized_target_repository_writes", 0),
                          "blockedReason": task.get("blockedReason", ""), "approvalStatus": approval.get("status", "")},
        "reliability": {"checkpointResume": metric_values.get("checkpoint_resume", 0),
                         "eventCount": len(await _task_event_projection(task_id)),
                         "subgraphCount": len(task.get("subgraphArtifacts") or {})},
        "efficiency": {"toolCalls": task.get("usedToolCalls", 0), "repairAttempt": task.get("repairAttempt", 0),
                        "llmCalls": metric_values.get("llm_call", 0)},
        "humanBurden": {"approvalStatus": approval.get("status", "NOT_REQUIRED"),
                        "approvalReasonCount": len(approval.get("approvalReasons", [])) if isinstance(approval, dict) else 0},
    }
    return ok({"taskId": task_id, "threadId": task.get("threadId", task_id), "runId": task.get("runId", ""),
               "trace": _build_task_trace(task), "events": await _task_event_projection(task_id),
               "artifacts": [{key: item.get(key) for key in
                              ("artifactId", "kind", "summary", "digest", "subgraph", "node")}
                             for item in artifacts],
               "metrics": [{key: item.get(key) for key in
                            ("metricId", "metricName", "value", "subgraph", "node", "attempt", "tags")}
                           for item in metrics], "report": report})


@app.get("/api/v1/codeops/evaluation/runtime/cases")
async def runtime_evaluation_cases() -> ApiResponse:
    return ok(runtime_reliability_cases())


@app.get("/api/v1/codeops/evaluation/cases")
async def codeops_evaluation_cases(evaluationLevel: str | None = None, caseLifecycle: str | None = None,
                                   caseSource: str | None = None, taskType: str | None = None,
                                   status: str | None = None) -> ApiResponse:
    """Read-only catalog view; runtime reliability cases stay on their legacy endpoint."""
    cases = builtin_codeops_eval_cases()
    filters = {"evaluationLevel": evaluationLevel, "caseLifecycle": caseLifecycle,
               "caseSource": caseSource, "taskType": taskType}
    filtered = [case for case in cases if all(value is None or str(case.get(key, "")).upper() == str(value).upper()
                                              for key, value in filters.items())]
    if status:
        filtered = [case for case in filtered if str(case.get("caseLifecycle", "")).upper() == status.upper()]
    return ok(filtered)


@app.get("/api/v1/codeops/evaluation/summary")
async def codeops_evaluation_summary() -> ApiResponse:
    catalog = builtin_codeops_eval_cases()
    business = [case for case in catalog if case.get("evaluationLevel") == BUSINESS_EVAL_LEVEL]
    return ok({"businessE2ETotal": len(business),
               "baselineCompleted": sum(case.get("caseSource") == BASELINE_CASE_SOURCE for case in business),
               "newlyAddedCompleted": sum(case.get("caseSource") == EXPANSION_CASE_SOURCE for case in business),
               "runtimeSafetyReliabilityCases": len(runtime_reliability_cases()),
               "caseIds": [case.get("caseId") for case in business]})


async def _task_event_projection(task_id: str) -> list[dict[str, Any]]:
    events = [item for item in await store.recent("task_events", 10000)
              if str(item.get("taskId") or "") == task_id]
    return sorted(events, key=lambda item: (int(item.get("timestamp") or 0), str(item.get("eventId") or "")))


@app.get("/api/v1/codeops/task/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    last_event_id = request.headers.get("Last-Event-ID", "")

    async def stream() -> AsyncIterator[str]:
        sent = last_event_id == ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = await _task_event_projection(task_id)
            if not sent and last_event_id:
                index = next((index for index, item in enumerate(events)
                              if str(item.get("eventId")) == last_event_id), None)
                events = events[index + 1:] if index is not None else events
                sent = True
            if events:
                if last_event_id and getattr(codeops_graph, "store", None):
                    await codeops_graph.observability_runtime.metric(task_id, "sse_replay", 1,
                                                                     node="task_events")
                for event in events:
                    summary = {key: event.get(key) for key in (
                        "eventId", "taskId", "stage", "kind", "attempt", "timestamp", "status",
                        "summary", "artifactRefs", "runId", "subgraph", "node", "toolCallId",
                        "approvalId", "patchDigest", "reviewVerdict", "blockedReason", "testStatus",
                        "sandboxState", "recoveryState")}
                    yield f"id: {summary['eventId']}\ndata: {json.dumps(summary, ensure_ascii=False, separators=(',', ':'))}\n\n"
                return
            task = await store.get("tasks", task_id)
            if task and task.get("status") not in {"RUNNING", "WAITING_APPROVAL"}:
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.get("/api/v1/codeops/task/incident/{task_id}")
async def incident_view(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    if not task:
        return fail("CodeOps task not found")
    return ok(_incident_fix_view(task))


@app.get("/api/v1/codeops/task/skills")
async def list_skills() -> ApiResponse:
    return ok(CodeOpsGraph.SKILLS)


@app.get("/api/v1/codeops/task/security/governance")
async def security_governance() -> ApiResponse:
    return ok(security_governance_service.global_summary())


@app.get("/api/v1/codeops/task/{task_id}/security")
async def task_security(task_id: str) -> ApiResponse:
    task = await store.get("tasks", task_id)
    return ok(security_governance_service.task_summary(task, task.get("approval", {}))) if task else fail("CodeOps task not found")


@app.get("/api/v1/codeops/task/list/recent")
async def recent_tasks() -> ApiResponse:
    return ok([_task_dto(task) for task in await store.recent("tasks", 10)])


@app.post("/api/v1/codeops/agent-loop/run")
async def agent_loop(body: AgentLoopRequest) -> ApiResponse:
    task_id = f"agent-loop-debug-{uuid.uuid4()}"
    max_turns = body.max_turns if body.max_turns is not None else 8
    task = {"taskId": task_id, "taskType": "AGENT_LOOP_DEBUG", "goal": body.goal,
            "repository": body.repository, "changeRef": body.change_ref, "focusAreas": body.focus_areas,
            "context": dict(body.context), "status": "RUNNING", "maxRounds": max_turns,
            "maxToolCalls": 20, "usedToolCalls": 0, "steps": [],
            "createTime": now_iso(), "updateTime": now_iso()}

    async def model_client(request: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
        if body.dry_run is True:
            return _mock_agent_loop_decision(request, steps)
        if not llm.available:
            reason = "OPENAI_API_KEY or OPENAI_BASE_URL is not configured"
            return {"thoughtSummary": "LLM client unavailable", "final": True,
                    "finalAnswer": f"Agent loop model client is unavailable: {reason}"}
        prompt = _agent_loop_prompt(request, steps, engineering_tool_gateway.list_registered_tools(read_only=True))
        try:
            content = await llm.complete(prompt)
        except RuntimeError:
            content = await llm.complete(prompt)
        return _parse_agent_loop_decision(content)

    result = await agent_loop_service.run({"goal": body.goal, "repository": body.repository,
                                           "changeRef": body.change_ref, "focusAreas": body.focus_areas,
                                           "context": body.context, "maxTurns": max_turns,
                                           "maxToolCalls": 20, "task": task}, model_client)
    task.update(status=result["status"], finalSummary=result["finalAnswer"] or result["stopReason"],
                steps=result["steps"], usedToolCalls=len(result["steps"]), updateTime=now_iso())
    await store.put("tasks", task_id, task, task["updateTime"])
    return ok({**result, "steps": result["steps"] if body.include_steps is True else []})


def _mock_agent_loop_decision(request: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = request.get("context") or {}
    forced = str(metadata.get("forcedToolName") or "")
    if not steps:
        if forced:
            return {"thoughtSummary": f"Dry-run forced tool turn: execute {forced}",
                    "toolCalls": [{"toolName": forced,
                                   "arguments": dict(metadata.get("forcedToolArguments") or {})}]}
        goal = str(request.get("goal") or "").lower()
        keyword = "OrderService" if "orderservice" in goal else "Order" if "order" in goal else "Service"
        return {"thoughtSummary": "Dry-run first turn: search repository text for goal keywords.",
                "toolCalls": [{"toolName": "repo.search_text", "arguments": {
                    "repository": request.get("repository", ""), "queries": [keyword], "maxMatches": 20}}]}
    last = steps[-1]
    if forced == "repo.maven_background" and len(steps) == 1:
        output = last.get("toolResult", {}).get("output") or {}
        task_id = output.get("taskId", "") if isinstance(output, dict) else ""
        return {"thoughtSummary": f"Dry-run forced tool status turn: query background task {task_id}",
                "toolCalls": [{"toolName": "task.background_status",
                               "arguments": {"backgroundTaskId": task_id}}]}
    tool_result = last.get("toolResult") or {}
    final = {"summary": f"Dry-run agent loop completed. Last tool={last.get('toolName')}, "
                         f"status={tool_result.get('status', '')}, summary={tool_result.get('summary', '')}",
             "targetFiles": ["src/main/java/com/example/order/OrderServiceApplication.java"],
             "recommendedTests": ["src/test/java/com/example/order/OrderServiceApplicationTests.java"],
             "shouldEnterCodeRepair": True, "localizationConfidence": "MEDIUM",
             "missingEvidence": ["dry-run uses a deterministic mock summary instead of model reasoning"]}
    return {"thoughtSummary": "Dry-run second turn: summarize the observed tool result.", "final": True,
            "finalAnswer": json.dumps(final, ensure_ascii=False, separators=(",", ":"))}


def _agent_loop_prompt(request: dict[str, Any], steps: list[dict[str, Any]],
                       tools: list[dict[str, Any]]) -> str:
    completed = len({item.get("turnNo") for item in steps})
    payload = {"goal": request.get("goal", ""), "repository": request.get("repository", ""),
               "changeRef": request.get("changeRef", ""), "focusAreas": request.get("focusAreas", []),
               "maxTurns": request.get("maxTurns", 0), "completedTurns": completed,
               "remainingTurns": max(0, int(request.get("maxTurns", 0)) - completed),
               "availableTools": tools, "metadata": request.get("context", {}), "previousSteps": steps}
    return ("You are the CodeOps agent loop planner inside an engineering diagnosis harness. "
            "Decide the next tool call(s) or produce a final answer. Return JSON only. Use only availableTools; "
            "prefer read-only tools; call no more than 3 tools per turn; when remainingTurns <= 1 produce a "
            "finalAnswer and no tools. Required decision fields: thoughtSummary, toolCalls, finalAnswer. "
            "A finalAnswer must be a compact JSON object containing summary, fixStrategy, scopeDecision, "
            "rootCauseLocationType, targetFiles, targetMethods, supportingCodeEvidence, negativeEvidence, "
            "reasoning, recommendedTests, shouldEnterCodeRepair, localizationConfidence, missingEvidence.\n"
            + json.dumps(payload, ensure_ascii=False, default=str))


def _parse_agent_loop_decision(content: str) -> dict[str, Any]:
    parsed = OpsDiagnosisGraph._json_object(content)
    if not parsed:
        return {"thoughtSummary": "Failed to parse model JSON", "final": True,
                "finalAnswer": "模型输出无法解析为 agent loop JSON：invalid JSON\n原始输出：" + content[:1200]}
    calls = []
    raw_calls = parsed.get("toolCalls", parsed.get("tool_calls", []))
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if isinstance(item, dict) and str(item.get("toolName") or "").strip():
                calls.append({"toolCallId": item.get("toolCallId") or f"tool-call-{uuid.uuid4()}",
                              "toolName": item["toolName"], "arguments": dict(item.get("arguments") or {})})
    answer = parsed.get("finalAnswer", parsed.get("final_answer", ""))
    if isinstance(answer, dict):
        answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    return {"thoughtSummary": parsed.get("thoughtSummary", parsed.get("thought_summary", "")),
            "toolCalls": calls, "final": bool(str(answer or "").strip()), "finalAnswer": str(answer or "")}


@app.get("/api/v1/codeops/task/{task_id}/approval")
async def task_approval_status(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    approval = task.get("approval") if task else None
    if not approval:
        return ApiResponse(info="No pending approval for this task", data={})
    return ok({**approval, "checkpoint": await codeops_graph.checkpoint_summary(task_id)})


@app.get("/api/v1/codeops/evaluation/approval/{task_id}")
async def evaluation_approval_status(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    approval = task.get("approval") if task else None
    return ok(approval) if approval else ApiResponse(info="No pending approval for this task", data=None)


def _approval_request(decision: str, body: ApprovalDecision | ApprovalDecisionContract | None) -> ApprovalDecisionContract:
    if decision == "reject":
        if body is None:
            return ApprovalDecisionContract(approved=False, action=ApprovalAction.REJECT, reason="No reason provided")
        raw = body.model_dump(by_alias=True)
        raw.update(approved=False, action=ApprovalAction.REJECT.value)
        return ApprovalDecisionContract.model_validate(raw)
    if body is None:
        return ApprovalDecisionContract(approved=True, action=ApprovalAction.APPROVE_DELIVERY)
    raw = body.model_dump(by_alias=True)
    raw_action = raw.get("action")
    action = raw_action.value if isinstance(raw_action, ApprovalAction) else str(
        raw_action or ApprovalAction.APPROVE_DELIVERY.value)
    if action == ApprovalAction.REJECT.value or raw.get("approved") is False:
        raise ValueError("approve endpoint only accepts an approve action")
    raw.update(approved=True, action=action)
    return ApprovalDecisionContract.model_validate(raw)


async def decide_approval(task_id: str, decision: str, body: ApprovalDecision | ApprovalDecisionContract | None) -> Any:
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    task = await store.get("tasks", task_id)
    approval = task.get("approval") if task else None
    if not approval:
        return fail("No pending approval for task: " + task_id)
    try:
        contract = _approval_request(decision, body)
    except ValueError as exc:
        return fail(str(exc), "0002")
    approval_id = str(approval.get("approvalId") or "")
    if contract.approval_id and contract.approval_id != approval_id:
        return fail("approvalId does not match the pending approval", "0002")
    decision_id = contract.decision_id
    if str(approval.get("decisionId") or "") == decision_id and str(approval.get("status")) != "PENDING":
        return ApiResponse(info="Approval decision already processed", data=approval)
    stored = await store.get("approvals", approval_id) if approval_id else None
    if stored and str(stored.get("decisionId") or "") == decision_id and str(stored.get("status")) != "PENDING":
        return ApiResponse(info="Approval decision already processed", data=stored)
    # Old persisted fixtures can represent a pending approval without a LangGraph
    # checkpoint or approvalId. Preserve that read/write projection contract, but
    # never take this path for an interrupt-backed approval.
    if not approval_id:
        reason = contract.reason or "No reason provided"
        approval["status"] = "APPROVED" if decision == "approve" else "REJECTED"
        approval["approvedAt"] = now_iso() if decision == "approve" else None
        approval["rejectionReason"] = reason if decision == "reject" else None
        task["status"] = "COMPLETED" if decision == "approve" else "HUMAN_REJECTED"
        line = "人工审批已通过，任务完成。" if decision == "approve" else "人工审批已拒绝：" + reason
        current_summary = str(task.get("finalSummary") or "")
        task["finalSummary"] = current_summary + ("\n" if current_summary.strip() else "") + line
        task["approval"] = approval
        task["updateTime"] = now_iso()
        await store.put("tasks", task_id, task, task["updateTime"])
        return ApiResponse(info="Legacy approval projection updated; no checkpoint was available", data=approval)
    try:
        resumed = await codeops_graph.resume(task_id, contract)
    except (ValueError, RuntimeError) as exc:
        # Legacy fixtures may contain a hand-written task without a checkpoint. Never use this
        # compatibility path for a real interrupt-backed approval (which always has approvalId).
        checkpoint = await codeops_graph.checkpoint_summary(task_id)
        if approval_id or checkpoint.get("checkpointPresent"):
            return fail(str(exc), "0002")
        reason = contract.reason or "No reason provided"
        approval["status"] = "APPROVED" if decision == "approve" else "REJECTED"
        approval["approvedAt"] = now_iso() if decision == "approve" else None
        approval["rejectionReason"] = reason if decision == "reject" else None
        task["status"] = "COMPLETED" if decision == "approve" else "HUMAN_REJECTED"
        line = "人工审批已通过，任务完成。" if decision == "approve" else "人工审批已拒绝：" + reason
        current_summary = str(task.get("finalSummary") or "")
        task["finalSummary"] = current_summary + ("\n" if current_summary.strip() else "") + line
        task["approval"] = approval
        task["updateTime"] = now_iso()
        await store.put("tasks", task_id, task, task["updateTime"])
        return ApiResponse(info="Legacy approval projection updated; no checkpoint was available", data=approval)
    result_task = resumed.get("task") if isinstance(resumed, dict) else None
    result_approval = result_task.get("approval") if isinstance(result_task, dict) else resumed.get("approval", {})
    result_approval = result_approval if isinstance(result_approval, dict) else approval
    audit_time = now_iso()
    audit = {"auditId": f"approval-decision-{approval_id}-{decision_id}", "taskId": task_id,
             "approvalId": approval_id, "decisionId": decision_id, "operatorId": contract.operator_id,
             "action": contract.action.value if isinstance(contract.action, ApprovalAction) else str(contract.action),
             "result": result_approval.get("status", "PROCESSED"), "reason": contract.reason,
             "createTime": audit_time}
    await store.put("audit_logs", audit["auditId"], audit, audit_time)
    info = "Task approved by human reviewer" if decision == "approve" else f"Task rejected: {contract.reason or 'No reason provided'}"
    return ApiResponse(info=info, data=result_approval)


@app.post("/api/v1/codeops/task/{task_id}/approval/approve")
@app.post("/api/v1/codeops/evaluation/approval/{task_id}/approve")
async def approve_task(task_id: str, body: ApprovalDecision | None = None) -> Any:
    return await decide_approval(task_id, "approve", body)


@app.post("/api/v1/codeops/task/{task_id}/approval/reject")
@app.post("/api/v1/codeops/evaluation/approval/{task_id}/reject")
async def reject_task(task_id: str, body: ApprovalDecision | None = None) -> Any:
    return await decide_approval(task_id, "reject", body)


@app.get("/api/v1/codeops/dashboard/tasks")
async def dashboard_tasks() -> ApiResponse:
    return ok([_task_row(task) for task in await store.recent("tasks", 20)])


@app.get("/api/v1/codeops/dashboard/tasks/{task_id}")
async def dashboard_task(task_id: str) -> Any:
    task = await store.get("tasks", task_id)
    if not task:
        return fail("CodeOps task not found")
    incident = _incident_fix_view(task)
    return ok({"task": _task_row(task), "incidentView": incident,
               "trace": _build_task_trace(task),
               "security": security_governance_service.task_summary(task, task.get("approval", {})),
               "llmCost": _task_cost_summary(task), "failure": _failure_summary(task, incident)})


@app.get("/api/v1/codeops/dashboard/overview")
async def dashboard_overview() -> ApiResponse:
    tasks = await store.recent("tasks", 20)
    scheduler = incident_scheduler.status() if incident_scheduler else {"running": False, "status": "NOT_INITIALIZED"}
    counts = {status: sum(t.get("status") == status for t in tasks) for status in
              ("RUNNING", "COMPLETED", "FAILED", "WAITING_APPROVAL", "WAITING_BACKGROUND_TASK")}
    if counts["FAILED"]:
        system_status = "DEGRADED"
    elif counts["RUNNING"] or int(scheduler.get("runningSlots") or 0):
        system_status = "PROCESSING"
    elif scheduler.get("running") is False:
        system_status = "SCHEDULER_DOWN"
    else:
        system_status = "READY"
    return ok({"generatedAt": now_iso(), "systemStatus": system_status,
               "services": _service_summary(tasks, scheduler),
               "taskSummary": {"totalRecent": len(tasks), "running": counts["RUNNING"],
                    "completed": counts["COMPLETED"], "failed": counts["FAILED"],
                    "waitingApproval": counts["WAITING_APPROVAL"],
                    "waitingBackground": counts["WAITING_BACKGROUND_TASK"],
                    "incidentToFix": sum(t.get("taskType") == "INCIDENT_TO_FIX" for t in tasks)},
               "scheduler": scheduler, "security": security_governance_service.global_summary(),
               "llmCost": codeops_graph.cost_control.global_summary()})


@app.get("/api/v1/codeops/dashboard/alerts")
async def dashboard_alerts() -> ApiResponse:
    tasks = await store.recent("tasks", 20)
    scheduler = incident_scheduler.status() if incident_scheduler else {"running": False}
    alerts = [_alert_item("AGGREGATING", item) for item in scheduler.get("activeIncidentItems", [])]
    alerts.extend(_alert_item("QUEUED", item) for item in scheduler.get("queuedIncidents", []))
    alerts.extend(_alert_item(str(task.get("status")), _task_context_alert(task), task)
                  for task in tasks if task.get("taskType") == "INCIDENT_TO_FIX")
    return ok(alerts)


@app.post("/api/v1/codeops/evaluation/run")
async def run_codeops_evaluation() -> ApiResponse:
    try:
        return ok(await _evaluate_cases(None))
    except Exception as exc:
        return fail(str(exc))


@app.post("/api/v1/codeops/evaluation/run/{case_id}")
async def run_codeops_evaluation_case(case_id: str) -> ApiResponse:
    try:
        return ok(await _evaluate_cases(case_id))
    except Exception as exc:
        return fail(str(exc))


@app.get("/api/v1/codeops/evaluation/report")
async def codeops_evaluation_report() -> ApiResponse:
    report = evaluation_state["lastReport"]
    return ok(report) if report else ApiResponse(info="No report yet. Run an eval first.", data=None)


@app.post("/api/v1/codeops/evaluation/report/rebuild")
async def rebuild_codeops_evaluation_report() -> ApiResponse:
    """Re-score persisted task artifacts without invoking an LLM or changing a repository."""
    try:
        return ok(await _rebuild_evaluation_report())
    except Exception as exc:
        return fail(str(exc))


@app.get("/api/v1/codeops/evaluation/scheduler/status")
async def scheduler_status() -> ApiResponse:
    return ok(incident_scheduler.status() if incident_scheduler else {"running": False, "status": "NOT_INITIALIZED"})


async def scheduler_action(action: str) -> ApiResponse:
    if incident_scheduler is None:
        return fail("Incident scheduler is not initialized")
    if action == "start":
        await incident_scheduler.start()
        return ApiResponse(info="Scheduler started", data=None)
    elif action == "stop":
        await incident_scheduler.stop()
        return ApiResponse(info="Scheduler stopped", data=None)
    raise HTTPException(404)


@app.post("/api/v1/codeops/evaluation/scheduler/start")
async def scheduler_start() -> ApiResponse:
    return await scheduler_action("start")


@app.post("/api/v1/codeops/evaluation/scheduler/stop")
async def scheduler_stop() -> ApiResponse:
    return await scheduler_action("stop")


@app.post("/api/v1/codeops/evaluation/scheduler/simulate")
async def scheduler_simulate(body: dict[str, Any]) -> ApiResponse:
    if incident_scheduler is None:
        return fail("Incident scheduler is not initialized")
    count = int(body.get("count", 100)) if isinstance(body.get("count", 100), (int, float)) else 100
    severity = body.get("severity") if isinstance(body.get("severity"), str) else "HIGH"
    run_id = body.get("runId") if isinstance(body.get("runId"), str) and body["runId"].strip() else str(uuid.uuid4())
    accepted = 0
    for index in range(count):
        service = f"order-service-{index % 3}"
        result = await incident_scheduler.ingest(
            f"{run_id}-storm-test-{index % 5}", f"StormSimulation{index % 5}", service, severity,
            f"Simulated alert #{index} — {severity} on {service}", "POST /api/orders/submit")
        accepted += result is not None
    deduped = count - accepted
    return ApiResponse(info=f"Sent {count} alerts: {deduped} deduped, {accepted} accepted into queue",
                       data={"runId": run_id, "totalSent": count, "deduped": deduped,
                             "accepted": accepted, "queueStats": incident_scheduler.status()})


@app.get("/api/v1/codeops/evaluation/model-router/stats")
async def model_router_stats() -> ApiResponse:
    stats = codeops_graph.model_router.stats()
    return ok({key: stats[key] for key in ("flashCalls", "proCalls", "escalations", "totalCalls",
                                           "flashRatio", "proRatio")})


@app.post("/api/v1/ops/evaluation/run")
async def run_ops_evaluation() -> ApiResponse:
    if not settings.ops_agent_evaluation_enabled:
        return fail("Ops evaluation harness is disabled. Please set ops.agent.evaluation.enabled=true")
    try:
        return ok(await _ops_fixture_summary(None))
    except Exception as exc:
        return fail(str(exc))


@app.post("/api/v1/ops/evaluation/run/{case_id}")
async def run_ops_evaluation_case(case_id: str) -> ApiResponse:
    if not settings.ops_agent_evaluation_enabled:
        return fail("Ops evaluation harness is disabled. Please set ops.agent.evaluation.enabled=true")
    try:
        return ok(await _ops_fixture_summary(case_id))
    except Exception as exc:
        return fail(str(exc))


@app.post("/api/v1/ops/evaluation/runbook-rag/run")
async def runbook_rag_evaluation() -> ApiResponse:
    if not settings.ops_agent_evaluation_enabled:
        return fail("Ops evaluation harness is disabled. Please set ops.agent.evaluation.enabled=true")
    return ok(await _run_rag_evaluation("HYBRID_RAG"))


@app.post("/api/v1/ops/evaluation/runbook-rag/ablation")
async def runbook_rag_ablation() -> ApiResponse:
    if not settings.ops_agent_evaluation_enabled:
        return fail("Ops evaluation harness is disabled. Please set ops.agent.evaluation.enabled=true")
    hybrid = await _run_rag_evaluation("HYBRID_RAG")
    keyword = await _run_rag_evaluation("KEYWORD_ONLY")
    hybrid["ablationSummaries"] = {"HYBRID_RAG": _rag_summary_view(hybrid),
                                   "KEYWORD_ONLY": _rag_summary_view(keyword)}
    return ok(hybrid)


@app.get("/api/v1/ops/evaluation/runbook-rag/governance")
async def runbook_rag_governance() -> ApiResponse:
    return ok(runbook_rag.governance())


@app.get("/api/v1/ops/evaluation/memory-toolchain/summary")
async def memory_toolchain_summary() -> ApiResponse:
    memory_count = len(await store.recent("memories", 100))
    comparison_modes = ["WITH_HISTORY_MEMORY", "WITHOUT_HISTORY_MEMORY", "TOOL_GOVERNANCE_ENABLED",
                        "KEYWORD_ONLY_RAG", "HYBRID_RAG"]
    return ok({"evaluationId": f"memory-toolchain-eval-{uuid.uuid4()}",
               "historicalMemoryCards": memory_count, "historicalMemoryHitRate": 1 if memory_count else 0,
               "comparisonModes": comparison_modes,
               "memoryCapabilities": {"shortTermWorkingMemory": True, "longTermHistoricalMemory": True,
                    "plannerConsumesWorkingMemory": True, "plannerConsumesHistoricalMemory": True,
                    "reviewerConsumesWorkingMemory": True, "reviewerConsumesHistoricalMemory": True,
                    "reportWriterConsumesWorkingMemory": True, "reportWriterConsumesHistoricalMemory": True,
                    "historicalMemoryCards": memory_count},
               "toolchainCapabilities": {"toolProtocolLogged": True, "logicalToolNameLogged": True,
                    "governanceDecisionLogged": True, "supportedProtocols": ["PROMETHEUS_HTTP",
                        "ELASTICSEARCH_HTTP", "ELASTICSEARCH_MCP", "SKYWALKING_HTTP", "RUNBOOK_RAG",
                        "LLM_CHAT_AGENT"], "governanceControls": ["whitelist", "budget", "timeout metadata",
                        "failure downgrade", "call log"]},
               "evaluationMetrics": {"historicalMemoryHitRate": 1 if memory_count else 0,
                    "toolProtocolCoverage": 1, "memoryPromptCoverage": 1,
                    "comparisonModesImplemented": comparison_modes},
               "explanation": "This endpoint is a lightweight capability and persisted-data summary. "
                    "Live diagnosis evaluation remains under /api/v1/ops/evaluation/run, and Runbook RAG "
                    "ablation remains under /api/v1/ops/evaluation/runbook-rag/ablation."})


def _diagnosis_record(state: dict[str, Any]) -> dict[str, Any]:
    req = state["request"]
    return {"diagnosisId": state["diagnosis_id"], "sessionId": state["session_id"], "serviceName": req["serviceName"],
            "startTime": req["startTime"], "endTime": req["endTime"], "problem": req["problem"], "traceId": req.get("traceId"),
            "status": state["status"], "requestJson": json.dumps(req, ensure_ascii=False),
            "metricEvidenceJson": json.dumps(state.get("metrics", {}), ensure_ascii=False),
            "logEvidenceJson": json.dumps(state.get("logs", {}), ensure_ascii=False),
            "traceEvidenceJson": json.dumps(state.get("traces", {}), ensure_ascii=False),
            "evidenceChainJson": json.dumps(state.get("evidence", []), ensure_ascii=False),
            "runbookJson": json.dumps(state.get("runbooks", []), ensure_ascii=False), "report": state.get("report", ""),
            "errorMessage": state.get("error", ""), "createTime": now_iso(), "updateTime": now_iso()}


def _task_row(task: dict[str, Any]) -> dict[str, Any]:
    context, steps = task.get("context") or {}, task.get("steps") or []
    step = next((item for item in reversed(steps) if item and str(item.get("selectedSkill") or "").strip()),
                steps[-1] if steps else None)
    status = str(task.get("status") or "")
    stage_by_skill = {"ops_diagnosis": "ops_evidence", "agent_loop_investigation": "code_localization",
                      "repo_understanding": "code_localization", "engineering_knowledge_rag": "knowledge_rag",
                      "bug_fix": "code_repair", "test_verification": "test_verification",
                      "release_risk_analysis": "release_risk"}
    stage = stage_by_skill.get(step.get("selectedSkill"), step.get("selectedSkill")) if step else "queued"
    progress = 100 if status in {"COMPLETED", "WAITING_APPROVAL"} else min(
        95, max(10 if status == "FAILED" else 5, len(steps) * 14))
    service = str(context.get("serviceName") or _infer_service_name(task.get("goal")))
    alert = str(context.get("alertName") or _infer_alert_name(task.get("goal")))
    endpoint = context.get("endpoint") or context.get("affectedEndpoints") or ""
    failure = _failure_summary(task)
    return {"taskId": task.get("taskId"), "taskType": task.get("taskType"), "serviceName": service,
            "alertName": alert, "endpoint": str(endpoint), "severity": str(context.get("severity") or "UNKNOWN"),
            "status": task.get("status"), "stage": stage, "progressPercent": progress,
            "usedToolCalls": task.get("usedToolCalls") or 0,
            "estimatedLlmCostCny": _task_cost_summary(task)["estimatedTotalCostCny"],
            "stepCount": len(steps),
            "lastStepSummary": str((step or {}).get("resultSummary") or task.get("finalSummary") or ""),
            "failureReason": failure["reason"],
            "requiresAttention": status in {"FAILED", "WAITING_APPROVAL", "WAITING_BACKGROUND_TASK"},
            "createTime": task.get("createTime") or "", "updateTime": task.get("updateTime") or ""}


def _task_cost_summary(task: dict[str, Any]) -> dict[str, Any]:
    calls, tokens, cost, step_costs = 0, 0, 0.0, []
    for step in task.get("steps") or []:
        usage = _json(step.get("rawEvidenceJson")).get("llmUsage")
        if not isinstance(usage, dict):
            continue
        calls += 1
        tokens += int(usage.get("estimatedTotalTokens") or 0)
        cost += float(usage.get("estimatedTotalCostCny") or 0)
        step_costs.append({"stepNo": step.get("stepNo"), "skill": step.get("selectedSkill"),
                           "model": str(usage.get("model") or ""),
                           "modelTier": str(usage.get("modelTier") or ""),
                           "estimatedTotalTokens": int(usage.get("estimatedTotalTokens") or 0),
                           "estimatedTotalCostCny": float(usage.get("estimatedTotalCostCny") or 0),
                           "overSoftLimit": usage.get("overSoftLimit") is True})
    return {"llmCalls": calls, "estimatedTotalTokens": tokens, "estimatedTotalCostCny": round(cost, 4),
            "stepCosts": step_costs,
            "note": "Estimated by prompt/response character count. Real provider billing may differ."}


def _failure_summary(task: dict[str, Any], view: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"stage": "", "reason": "", "recoverable": False}
    status, steps = str(task.get("status") or ""), task.get("steps") or []
    step = next((item for item in reversed(steps) if item and str(item.get("selectedSkill") or "").strip()), None)
    if status == "FAILED":
        result.update(stage=str((step or {}).get("selectedSkill") or _task_row_stage(task)),
                      reason=str((step or {}).get("resultSummary") or task.get("finalSummary") or "Task failed"),
                      recoverable=True)
    elif status == "WAITING_APPROVAL":
        result.update(stage="human_approval", reason="High-risk patch is waiting for human approval.", recoverable=True)
    elif status == "WAITING_BACKGROUND_TASK":
        result.update(stage="test_verification", reason="Background verification task is still running.", recoverable=True)
    if not result["reason"] and view:
        blocked = next((stage for stage in view.get("stages", []) if stage.get("status") in {"FAILED", "BLOCKED"}), None)
        if blocked:
            result.update(stage=blocked.get("stageId") or blocked.get("stageName") or "",
                          reason=blocked.get("summary") or f"Stage {blocked.get('stageName') or blocked.get('stageId')} is {blocked.get('status')}",
                          recoverable=True)
    return result


def _task_row_stage(task: dict[str, Any]) -> str:
    steps = task.get("steps") or []
    step = next((item for item in reversed(steps) if item and item.get("selectedSkill")), None)
    return str((step or {}).get("selectedSkill") or "queued")


def _infer_service_name(goal: Any) -> str:
    parts = str(goal or "").strip().split()
    return parts[0] if parts else "unknown-service"


def _infer_alert_name(goal: Any) -> str:
    for part in str(goal or "").replace("[", " ").replace("]", " ").split():
        lower = part.lower()
        if any(token in lower for token in ("alert", "5xx", "latency", "gc", "timeout")):
            return part
    return "incident" if str(goal or "").strip() else "unknown-alert"


def _service_summary(tasks: list[dict[str, Any]], scheduler: dict[str, Any]) -> list[dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    for task in tasks:
        name = str((task.get("context") or {}).get("serviceName") or _infer_service_name(task.get("goal")))
        item = services.setdefault(name, {"serviceName": name, "status": "READY", "recentTasks": 0,
                                          "activeAlerts": 0, "lastTaskId": "", "lastUpdate": ""})
        item["recentTasks"] += 1
        item["status"] = _service_status(str(item["status"]), str(task.get("status") or ""))
        item["lastTaskId"], item["lastUpdate"] = task.get("taskId"), task.get("updateTime") or ""
    for active in scheduler.get("activeIncidentItems", []) if isinstance(scheduler.get("activeIncidentItems"), list) else []:
        name = str(active.get("service") or "unknown-service")
        item = services.setdefault(name, {"serviceName": name, "status": "READY", "recentTasks": 0,
                                          "activeAlerts": 0, "lastTaskId": "", "lastUpdate": ""})
        item["activeAlerts"] += int(active.get("alertCount") or 0)
        item["status"] = _service_status(str(item["status"]), "RUNNING")
    return list(services.values())


def _service_status(current: str, task_status: str) -> str:
    if task_status == "FAILED":
        return "DEGRADED"
    if task_status in {"RUNNING", "WAITING_BACKGROUND_TASK"}:
        return "PROCESSING"
    return current if current in {"DEGRADED", "PROCESSING"} else "READY"


def _task_context_alert(task: dict[str, Any]) -> dict[str, Any]:
    context = task.get("context") or {}
    return {"taskId": task.get("taskId"), "service": context.get("serviceName") or _infer_service_name(task.get("goal")),
            "alertName": context.get("alertName") or _infer_alert_name(task.get("goal")),
            "severity": context.get("severity") or "UNKNOWN", "summary": task.get("goal"),
            "endpoint": context.get("endpoint") or context.get("affectedEndpoints") or "",
            "alertCount": int(context.get("alertCount") or 0)}


def _alert_item(status: str, source: dict[str, Any], task: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": task.get("taskId") if task else source.get("groupKey") or source.get("taskId") or "",
            "taskId": task.get("taskId") if task else source.get("taskId") or "",
            "serviceName": source.get("service") or (_infer_service_name(task.get("goal")) if task else "unknown-service"),
            "alertName": source.get("alertName") or (_infer_alert_name(task.get("goal")) if task else "unknown-alert"),
            "severity": source.get("severity") or "UNKNOWN", "status": status,
            "summary": source.get("summary") or (task.get("goal") if task else ""),
            "endpoint": source.get("endpoint") or source.get("endpoints") or "",
            "alertCount": int(source.get("alertCount") or 0),
            "lastUpdate": source.get("lastUpdate") or (task.get("updateTime") if task else "") or ""}


def _json(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def _expected_no_code_fix(case: dict[str, Any]) -> bool:
    expected = str(case.get("expectedFixStrategy") or "").upper()
    scope = str(case.get("expectedScopeDecision") or "").upper()
    outcome = case.get("expectedOutcome") if isinstance(case.get("expectedOutcome"), dict) else {}
    return expected == "NO_CODE_FIX" or scope == "NO_CODE_FIX" or str(outcome.get("classification") or "").upper() == "NO_CODE_FIX"


def _evaluation_terminal_success(case: dict[str, Any], status: str) -> bool:
    outcome = case.get("expectedOutcome") if isinstance(case.get("expectedOutcome"), dict) else {}
    expected_stop = str(outcome.get("requiredStoppingState") or "")
    if expected_stop and status in {"REVIEW_REJECTED", "REPAIR_STOPPED", "REQUIRES_REVIEW"}:
        return True
    return status in {"COMPLETED", "WAITING_APPROVAL"} or (
        (_expected_no_code_fix(case) or case.get("taskType") in {"RELEASE_RISK", "CODE_REVIEW"})
        and status == "NO_CODE_FIX")


def _bounded_evaluation_text(task: dict[str, Any], task_context: dict[str, Any],
                             memory: dict[str, Any], fixture_evidence: dict[str, Any] | None = None) -> str:
    """Build metric input without serializing prompts, snapshots or full tool output."""
    step_projection = [
        {key: str(step.get(key) or "")[:800]
         for key in ("selectedSkill", "status", "resultSummary", "rawEvidenceJson")}
        for step in (task.get("steps") or [])[-20:]
        if isinstance(step, dict)
    ]
    memory_keys = ("opsEvidence", "agentLoopInvestigation", "codeLocalization", "fixStrategy",
                   "engineeringKnowledge", "patchGeneration", "testVerification", "releaseRisk",
                   "releaseReview")
    projection: dict[str, Any] = {
        "task": {key: task.get(key) for key in ("taskType", "goal", "status", "finalSummary")},
        "steps": step_projection,
        "workingMemory": {},
        # Graph context compaction may omit a large fixture object.  The
        # evaluation request's fixture remains the authoritative, redacted
        # source for evidence scoring; it is not model output or a fake result.
        "fixtureEvidence": fixture_evidence if fixture_evidence is not None else task_context.get("fixtureEvidence", {}),
    }
    for key in memory_keys:
        if key not in memory:
            continue
        # Keep enough structured text for expected-keyword metrics, but never
        # include the unbounded source snapshot or raw provider response.
        value = redact(memory.get(key))
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        projection["workingMemory"][key] = encoded[:8000]
    evidence_details = (memory.get("opsEvidence", {}).get("evidenceDetails", {})
                        if isinstance(memory.get("opsEvidence"), dict) else {})
    projection["evidenceDetails"] = redact(evidence_details)
    return json.dumps(redact(projection), ensure_ascii=False, default=str)[:60000].lower()


async def _evaluate_cases(case_id: str | None) -> dict[str, Any]:
    cases = _load_eval_cases(case_id)
    if case_id and not cases:
        raise ValueError(f"CodeOps builtin eval case not found: {case_id}")
    batch_id = f"codeops-eval-batch-{uuid.uuid4()}"
    results: list[dict[str, Any]] = []
    case_reports: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    for case in cases:
        previous = await _previous_successful_eval_run(case)
        if previous and settings.codeops_eval_skip_previously_successful:
            skipped_existing.append({"caseId": case.get("caseId"), "taskId": previous.get("taskId", ""),
                                     "runId": previous.get("runId", ""),
                                     "reason": "PREVIOUS_SUCCESS_SAME_CASE_REVISION"})
            continue
        started = time.perf_counter()
        fixture_payloads = _fixture_payloads(case)
        context = {**(case.get("context") or {}), "evaluationCaseId": case.get("caseId"),
                   "fixtureEvidence": fixture_payloads,
                   # This is an evaluation-only contract.  It keeps a declared
                   # no-code fixture from being reinterpreted as a source patch
                   # merely because a model sees a Java file in the repository.
                   # Production requests never set this field.
                   "evaluationExpectedNoCodePatch": _expected_no_code_fix(case)}
        request = CodeOpsTaskRequest(taskType=case.get("taskType", "INCIDENT_TO_FIX"), goal=case.get("goal", "evaluate"),
                                     repository=case.get("repository"), changeRef=case.get("changeRef"),
                                     focusAreas=case.get("focusAreas"), maxRounds=8, maxToolCalls=40, context=context)
        state = await codeops_graph.invoke(request)
        task = state["task"]
        task_context = task.get("context") if isinstance(task.get("context"), dict) else {}
        memory = task_context.get("incidentFixWorkingMemory", {})
        # Evidence is a first-class output of OpsDiagnosisSubgraph and may live
        # in fixtureEvidence/evidenceDetails rather than in the task summary.
        # Include those bounded projections in the metric input; do not use the
        # metric as a reason to copy full prompts or tool responses into reports.
        text = _bounded_evaluation_text(task, task_context, memory if isinstance(memory, dict) else {}, fixture_payloads)
        selected = [step.get("selectedSkill") for step in task.get("steps", [])]
        expected_skills = case.get("expectedSkills") or []
        target_files = case.get("expectedTargetFiles") or []
        target_methods = case.get("expectedTargetMethods") or []
        patch_terms = case.get("expectedPatchKeywords") or []
        test_terms = case.get("expectedTestNames") or []
        risk_terms = case.get("expectedRiskKeywords") or []
        expected_artifacts = case.get("expectedArtifacts") or []
        expected_evidence = case.get("expectedEvidenceKeywords") or []
        expected_decision = _values(case, "expectedFixStrategy") + _values(case, "expectedScopeDecision")
        metrics = {
            "skillCoverage": _coverage(expected_skills, selected),
            "evidenceCoverage": _coverage(expected_evidence, text),
            "artifactCoverage": _coverage(expected_artifacts, text),
            "codeLocalizationCoverage": _coverage(target_files + target_methods, text),
            "localizationDecisionCoverage": _coverage(expected_decision, text),
            "patchCoverage": _coverage(patch_terms, text), "testCoverage": _coverage(test_terms, text),
            "riskCoverage": _coverage(risk_terms, text),
        }
        final_verification = memory.get("testVerification", {}) if isinstance(memory, dict) else {}
        recovered_repair = (state["status"] == "COMPLETED" and isinstance(final_verification, dict)
                            and final_verification.get("testsPassed") is True)
        failed_gate = (any(step.get("status") == "FAILED" and step.get("selectedSkill") in {"bug_fix", "test_verification"}
                           for step in task.get("steps", [])) and not recovered_repair)
        release_raw = task.get("context", {}).get("releaseRiskRaw", {})
        external_llm_reason = str(release_raw.get("llmReleaseRiskError") or "") if isinstance(release_raw, dict) else ""
        external_llm_skip = bool(external_llm_reason and re.search(
            r"EXTERNAL_LLM_EMPTY_CONTENT|ReadTimeout|ConnectTimeout|timeout|timed out",
            external_llm_reason, re.IGNORECASE))
        expected_stop = bool((case.get("expectedOutcome") or {}).get("requiredStoppingState"))
        success = _evaluation_terminal_success(case, state["status"]) and (expected_stop or not failed_gate) \
            and metrics["skillCoverage"] == 1
        for name in ("evidenceCoverage", "artifactCoverage", "codeLocalizationCoverage", "localizationDecisionCoverage",
                     "patchCoverage", "testCoverage", "riskCoverage"):
            if ({"codeLocalizationCoverage", "localizationDecisionCoverage", "patchCoverage", "testCoverage", "riskCoverage"}.__contains__(name)
                    and not ({"codeLocalizationCoverage": target_files + target_methods,
                              "localizationDecisionCoverage": expected_decision, "patchCoverage": patch_terms,
                              "testCoverage": test_terms, "riskCoverage": risk_terms}[name])):
                continue
            success = success and metrics[name] >= 0.5
        if external_llm_skip:
            success = False
        missing_skills = [value for value in expected_skills if value not in selected]
        missing_evidence = [value for value in expected_evidence if str(value).lower() not in text]
        missing_artifacts = [value for value in expected_artifacts if str(value).lower() not in text]
        detail = {"batchId": batch_id, "caseName": case.get("caseName", case.get("caseId")),
                  "evaluationScoringSchemaVersion": EVALUATION_SCORING_SCHEMA_VERSION,
                  "selectedSkills": selected, "expectedSkills": expected_skills,
                  "expectedEvidenceKeywords": expected_evidence, "expectedArtifacts": expected_artifacts,
                  "codeLocalizationCoverage": metrics["codeLocalizationCoverage"],
                  "localizationTargetFileHitRate": _coverage(target_files, text),
                  "localizationTargetMethodHitRate": _coverage(target_methods, text),
                  "localizationDecisionCoverage": metrics["localizationDecisionCoverage"],
                  "patchCoverage": metrics["patchCoverage"], "testCoverage": metrics["testCoverage"],
                   "riskCoverage": metrics["riskCoverage"], "taskStatus": state["status"],
                   "externalLlmSkip": external_llm_skip, "externalLlmReason": external_llm_reason,
                   "hasFailedRepairOrTestStep": failed_gate, "finalSummary": task.get("finalSummary", ""),
                  "expected": case}
        item = {"runId": f"codeops-eval-run-{uuid.uuid4()}", "caseId": case.get("caseId"),
                "taskId": task["taskId"], "taskType": case.get("taskType", "INCIDENT_TO_FIX"),
                 "status": "SKIPPED" if external_llm_skip else "SUCCESS" if success else "FAILED",
                "expectedSkillCoverage": metrics["skillCoverage"],
                "evidenceKeywordCoverage": metrics["evidenceCoverage"],
                "artifactCoverage": metrics["artifactCoverage"], "stepCount": len(task.get("steps", [])),
                "usedToolCalls": task.get("usedToolCalls", 0),
                "latencyMs": int((time.perf_counter() - started) * 1000), "missingSkills": missing_skills,
                "missingEvidenceKeywords": missing_evidence, "missingArtifacts": missing_artifacts,
                 "detail": detail, "errorMessage": external_llm_reason or None}
        results.append(item)
        case_report, trace, patch_diff = build_case_report(batch_id, case, item, task)
        write_case_artifacts(case_report, trace, patch_diff)
        case_reports.append(case_report)
        await store.put("eval_runs", item["runId"], item, now_iso())
    if not results:
        return {"batchId": batch_id, "totalCases": 0, "successCases": 0, "failedCases": 0,
                "skippedCases": len(skipped_existing), "businessE2ETotal": 52,
                "baselineCompleted": 16, "newlyAddedCompleted": 36,
                "runtimeSafetyReliabilityCases": 10, "runs": [],
                "skippedExistingSuccessCases": skipped_existing,
                "message": "No graph was invoked because every requested Case already has a successful run for the same Case revision."}
    count = len(results)
    def average(key: str) -> float:
        return round(sum(float(item["detail"].get(key, 0)) for item in results) / count, 4)
    target_file_hit = average("localizationTargetFileHitRate")
    target_method_hit = average("localizationTargetMethodHitRate")
    fix_accuracy = average("localizationDecisionCoverage")
    scope_accuracy = fix_accuracy
    report = build_report(batch_id, case_reports)
    evaluation_state["lastReport"] = report
    report_dir = Path("data/codeops-eval") / batch_id
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = report_dir / "report.json", report_dir / "report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(summary_markdown(report), encoding="utf-8")
    return {"batchId": batch_id, "totalCases": len(results),
            "successCases": report["successCases"], "failedCases": report["failedCases"],
            "skippedCases": report.get("skippedCases", 0) + len(skipped_existing),
            "businessE2ETotal": report["businessE2ETotal"],
            "baselineCompleted": report["baselineCompleted"],
            "newlyAddedCompleted": report["newlyAddedCompleted"],
            "runtimeSafetyReliabilityCases": report["runtimeSafetyReliabilityCases"],
            "averageExpectedSkillCoverage": round(sum(item["expectedSkillCoverage"] for item in results) / count, 4),
            "averageEvidenceKeywordCoverage": round(sum(item["evidenceKeywordCoverage"] for item in results) / count, 4),
            "averageArtifactCoverage": round(sum(item["artifactCoverage"] for item in results) / count, 4),
            "averageCodeLocalizationCoverage": average("codeLocalizationCoverage"),
            "averageLocalizationDecisionCoverage": average("localizationDecisionCoverage"),
            "averageLocalizationTargetFileHitRate": target_file_hit,
            "averageLocalizationTargetMethodHitRate": target_method_hit,
            "averageLocalizationFixStrategyAccuracy": fix_accuracy,
            "averageLocalizationScopeDecisionAccuracy": scope_accuracy,
            "averagePatchCoverage": average("patchCoverage"), "averageTestCoverage": average("testCoverage"),
            "averageRiskCoverage": average("riskCoverage"),
            "averageStepCount": round(sum(item["stepCount"] for item in results) / count, 4),
            "averageToolCallCount": round(sum(item["usedToolCalls"] for item in results) / count, 4),
            "averageLatencyMs": round(sum(item["latencyMs"] for item in results) / count, 4),
            "skippedExistingSuccessCases": skipped_existing,
            "runs": results, "reportJsonPath": str(json_path).replace("\\", "/"),
            "reportMarkdownPath": str(markdown_path).replace("\\", "/")}


async def _rebuild_evaluation_report() -> dict[str, Any]:
    """Build a fresh report from stored task artifacts, never re-running an Eval graph.

    A scoring-code change must not be hidden by same-case deduplication.  This path
    intentionally leaves ``eval_runs`` immutable and records exactly which catalog
    entries have a reconstructable current-revision task artifact.
    """
    catalog = {str(case.get("caseId")): case for case in builtin_codeops_eval_cases()}
    current_runs: dict[str, dict[str, Any]] = {}
    for run in await store.recent("eval_runs", 10_000):
        case_id = str(run.get("caseId") or "")
        detail = run.get("detail") if isinstance(run.get("detail"), dict) else {}
        expected = detail.get("expected") if isinstance(detail.get("expected"), dict) else {}
        case = catalog.get(case_id)
        if not case or str(expected.get("evaluationCaseRevision") or "1") != str(case.get("evaluationCaseRevision") or "1"):
            continue
        current_runs.setdefault(case_id, run)
    batch_id = f"codeops-eval-rebuild-{uuid.uuid4()}"
    reports: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for case_id, case in catalog.items():
        run = current_runs.get(case_id)
        if not run:
            unavailable.append(case_id)
            continue
        task = await store.get("tasks", str(run.get("taskId") or ""))
        if not isinstance(task, dict):
            unavailable.append(case_id)
            continue
        report, trace, patch_diff = build_case_report(batch_id, case, run, task)
        write_case_artifacts(report, trace, patch_diff)
        reports.append(report)
    rebuilt = build_report(batch_id, reports)
    business_catalog = [case for case in catalog.values() if case.get("evaluationLevel") == BUSINESS_EVAL_LEVEL]
    rebuilt.update({"evaluationScoringSchemaVersion": EVALUATION_SCORING_SCHEMA_VERSION,
                    "catalogBusinessE2ETotal": len(business_catalog),
                    "evaluatedBusinessE2ETotal": len(reports),
                    "unavailableCurrentArtifactCaseIds": unavailable})
    report_dir = Path("data/codeops-eval") / batch_id
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = report_dir / "report.json", report_dir / "report.md"
    json_path.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(summary_markdown(rebuilt), encoding="utf-8")
    evaluation_state["lastReport"] = rebuilt
    return {**rebuilt, "reportJsonPath": str(json_path).replace("\\", "/"),
            "reportMarkdownPath": str(markdown_path).replace("\\", "/")}


async def _previous_successful_eval_run(case: dict[str, Any]) -> dict[str, Any] | None:
    """Return a successful run only when it used this exact immutable Case revision."""
    case_id = str(case.get("caseId") or "")
    revision = str(case.get("evaluationCaseRevision") or "1")
    if not case_id:
        return None
    for run in await store.find("eval_runs", lambda item: item.get("caseId") == case_id and
                                item.get("status") == "SUCCESS", limit=10000):
        detail = run.get("detail") if isinstance(run.get("detail"), dict) else {}
        expected = detail.get("expected") if isinstance(detail.get("expected"), dict) else {}
        if str(expected.get("evaluationCaseRevision") or "1") == revision:
            return run
    return None


async def _ops_fixture_summary(case_id: str | None) -> dict[str, Any]:
    cases = await _load_ops_eval_cases(case_id)
    if case_id and not cases:
        raise ValueError(f"enabled eval case not found: {case_id}")
    batch_id, items = f"eval-batch-{uuid.uuid4()}", []
    for case in cases:
        expected = case.get("expected", {})
        started = time.perf_counter()
        created_at = now_iso()
        run_id = f"eval-run-{uuid.uuid4()}"
        request = IncidentAnalyzeRequest(serviceName=case.get("serviceName") or expected.get("serviceName", "unknown-service"),
            startTime="2026-01-01T00:00:00", endTime="2026-01-01T00:15:00", problem=case.get("problem") or case.get("input", "evaluate"),
            fixtureCaseId=case.get("caseId", ""), maxStep=10)
        running = {"runId": run_id, "caseId": case.get("caseId"), "diagnosisId": None,
                   "status": "RUNNING", "top1RootCauseHit": 0, "top3RootCauseHit": 0,
                   "requiredEvidenceCoverage": 0, "unsupportedConclusionCount": 0, "toolCallCount": 0,
                   "diagnosisLatencyMs": 0, "finalStatus": None,
                   "summaryJson": json.dumps({"batchId": batch_id}, ensure_ascii=False),
                   "errorMessage": None, "createTime": created_at, "updateTime": created_at}
        await store.put("eval_runs", run_id, running, created_at)
        state = await ops_graph.invoke(request)
        expected_root = str(case.get("expectedRootCause") or expected.get("rootCause", ""))
        top = state.get("candidates", [{}])[0]
        top_text = json.dumps(top, ensure_ascii=False).lower()
        all_text = json.dumps(state.get("candidates", []), ensure_ascii=False).lower()
        root_terms = [term for term in re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{3,}", expected_root.lower())
                      if term not in {"with", "from", "that", "this", "before", "under", "requires"}]
        threshold = max(1, min(3, len(root_terms)))
        top1 = int(sum(term in top_text for term in root_terms) >= threshold)
        top3 = int(top1 or sum(term in all_text for term in root_terms) >= threshold)
        evidence_expect = _as_string_list(case.get("expectedEvidenceTypesJson")) or [
            key for key in ("prometheus", "logs", "trace") if key in case.get("fixtures", {})]
        evidence_text = json.dumps({key: state.get(key) for key in ("metrics", "logs", "traces", "evidence", "runbooks")}, ensure_ascii=False).lower()
        evidence_coverage = _coverage(evidence_expect, evidence_text.replace("metrics", "prometheus").replace("traces", "trace"))
        expected_tools = _as_string_list(case.get("expectedToolsJson")) or [
            "query_prometheus", "query_elasticsearch", "query_skywalking_trace"]
        actual_tools = [item.get("tool") for item in state.get("tool_trace", [])]
        tool_coverage = _coverage(expected_tools, actual_tools)
        unsupported = int(evidence_coverage < 0.5 and ("root cause" in state.get("report", "").lower() or "根因" in state.get("report", ""))
                          and not any(word in state.get("report", "").lower() for word in ("可能", "疑似", "证据不足", "hypothesis")))
        summary_detail = {"caseId": case.get("caseId"), "caseName": case.get("caseName"),
                          "expectedRootCause": expected_root, "diagnosisId": state["diagnosis_id"],
                          "top1RootCauseHit": top1, "top3RootCauseHit": top3,
                          "requiredEvidenceCoverage": evidence_coverage,
                          "expectedToolCoverage": tool_coverage, "toolCallCount": len(actual_tools),
                          "plannerChatAgent": False, "reviewerChatAgent": False,
                          "reportWriterChatAgent": False, "strictThreeAgentPath": False,
                          "errorMessage": None}
        item = {"runId": run_id, "caseId": case.get("caseId"),
                "diagnosisId": state["diagnosis_id"], "status": "SUCCESS" if state["status"] == "SUCCESS" else "FAILED",
                "top1RootCauseHit": top1, "top3RootCauseHit": top3,
                "requiredEvidenceCoverage": evidence_coverage,
                "unsupportedConclusionCount": unsupported, "toolCallCount": len(actual_tools),
                "diagnosisLatencyMs": int((time.perf_counter() - started) * 1000), "finalStatus": state["status"],
                "summaryJson": json.dumps(summary_detail, ensure_ascii=False, separators=(",", ":")),
                "errorMessage": None, "createTime": created_at, "updateTime": now_iso()}
        items.append(item)
        await store.put("eval_runs", run_id, item, item["updateTime"])
        metric_values = {"top1RootCauseHit": top1, "top3RootCauseHit": top3,
                         "requiredEvidenceCoverage": evidence_coverage, "expectedToolCoverage": tool_coverage,
                         "unsupportedConclusionCount": unsupported, "toolCallCount": len(actual_tools),
                         "diagnosisLatencyMs": item["diagnosisLatencyMs"],
                         "finalStatus": int(state["status"] == "SUCCESS"), "plannerChatAgent": 0,
                         "reviewerChatAgent": 0, "reportWriterChatAgent": 0, "strictThreeAgentPath": 0}
        for metric_name, metric_value in metric_values.items():
            metric_id = str(uuid.uuid4())
            metric = {"id": None, "runId": run_id, "caseId": case.get("caseId"),
                      "metricName": metric_name, "metricValue": metric_value,
                      "metricDetailJson": item["summaryJson"], "createTime": now_iso()}
            await store.put("eval_metrics", metric_id, metric, metric["createTime"])
    count = len(items) or 1
    return {"batchId": batch_id, "totalCases": len(items),
            "successCases": sum(item["status"] == "SUCCESS" for item in items),
            "failedCases": sum(item["status"] != "SUCCESS" for item in items),
            "top1RootCauseHitRate": round(sum(item["top1RootCauseHit"] for item in items) / count, 4),
            "top3RootCauseHitRate": round(sum(item["top3RootCauseHit"] for item in items) / count, 4),
            "averageEvidenceCoverage": round(sum(item["requiredEvidenceCoverage"] for item in items) / count, 4),
            "averageExpectedToolCoverage": round(sum(json.loads(item["summaryJson"])["expectedToolCoverage"] for item in items) / count, 4),
            "averageToolCallCount": round(sum(item["toolCallCount"] for item in items) / count, 4),
            "averageLatencyMs": round(sum(item["diagnosisLatencyMs"] for item in items) / count, 4),
            "runs": items}


async def _load_ops_eval_cases(case_id: str | None) -> list[dict[str, Any]]:
    persisted = await store.recent("eval_cases", 10000)
    enabled = [case for case in persisted if int(case.get("enabled", 1) or 0) == 1]
    if enabled:
        return [case for case in enabled if not case_id or case.get("caseId") == case_id]
    return _load_eval_cases(case_id)


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        return [str(item) for item in parsed] if isinstance(parsed, list) else [str(value)]
    except json.JSONDecodeError:
        return [str(value)]


def _fixture_payloads(case: dict[str, Any]) -> dict[str, Any]:
    fixture_case = (case.get("context") or {}).get("fixtureCase")
    source = case
    if fixture_case:
        try:
            source = json.loads(Path(str(fixture_case)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            source = {}
    scenarios = source.get("scenarios") if isinstance(source, dict) else None
    if isinstance(scenarios, dict):
        scenario_id = str((case.get("context") or {}).get("fixtureCaseId") or case.get("caseId") or "")
        source = scenarios.get(scenario_id, {})
    payloads = {}
    for name, path in source.get("fixtures", {}).items():
        if isinstance(path, dict):
            payloads[name] = path
        else:
            try:
                payloads[name] = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payloads[name] = {"error": f"unable to load {path}"}
    return payloads


def _values(source: dict[str, Any], *keys: str) -> list[str]:
    values = []
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item)
        elif value not in (None, ""):
            values.append(str(value))
    return values


def _coverage(expected: list[str], actual: Any) -> float:
    expected = [str(item).lower() for item in expected if item]
    if not expected:
        return 1.0
    text = json.dumps(actual, ensure_ascii=False).lower() if not isinstance(actual, str) else actual.lower()
    return round(sum(item in text for item in expected) / len(expected), 4)


def _expected_skills(task_type: str, expected: dict[str, Any]) -> list[str]:
    if task_type == "CODE_REVIEW":
        return ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "pr_review", "test_verification"]
    if task_type == "RELEASE_RISK":
        return ["agent_loop_investigation", "repo_understanding", "engineering_knowledge_rag", "release_risk_analysis", "test_verification"]
    skills = ["agent_loop_investigation", "repo_understanding"]
    if task_type == "INCIDENT_TO_FIX":
        skills.insert(0, "ops_diagnosis")
    if not expected.get("expectedNoCodePatch", False):
        skills.extend(["engineering_knowledge_rag", "bug_fix"])
        skills.append("test_verification")
    skills.append("release_risk_analysis")
    return skills


async def _run_rag_evaluation(mode: str) -> dict[str, Any]:
    cases = [
        ("rag-db-pool-hikari", "Hikari pool saturation SQLTransientConnectionException", "database-connection-pool"),
        ("rag-http-500-npe", "HTTP 500 NullPointerException service stack trace", "http-500-error"),
        ("rag-gateway-502", "gateway 502 503 upstream error", "gateway-http-5xx"),
        ("rag-rpc-timeout", "Dubbo RPC Feign downstream timeout", "rpc-timeout"),
        ("rag-redis-timeout", "RedisCommandTimeoutException cache pool exhausted", "redis-timeout"),
        ("rag-jvm-full-gc", "JVM Full GC pause heap pressure", "jvm-full-gc"),
        ("rag-mq-backlog", "message queue consumer lag backlog retry", "mq-backlog"),
        ("rag-slow-sql", "slow SQL DB span lock wait", "slow-sql-db-span"),
        ("rag-cpu", "process CPU saturation all endpoints slow", "cpu-saturation"),
        ("rag-thread-pool", "thread pool active max rejected tasks saturation", "thread-pool-saturation"),
        ("rag-pod-crash", "Kubernetes pod CrashLoopBackOff restart", "kubernetes-pod-crashloop"),
        ("rag-idempotency", "payment callback duplicate request idempotency", "payment-callback-idempotency"),
    ]
    batch_id, runs = f"{mode.lower()}-rag-eval-{uuid.uuid4()}", []
    for case, query, expected in cases:
        started = time.perf_counter()
        matches = await runbook_rag.search(query, 5, mode)
        rank = next((index for index, item in enumerate(matches, 1)
                     if expected in str(item.get("document", item.get("metadata", {}).get("path", ""))).lower()), 0)
        run = {"runId": f"rag-run-{uuid.uuid4()}", "batchId": batch_id, "caseId": case,
               "status": "SUCCESS" if 0 < rank <= 3 else "FAILED", "expectedRunbookIds": [expected],
               "retrievedRunbooks": matches, "top1Hit": int(rank == 1), "top3Hit": int(0 < rank <= 3),
               "top5Hit": int(0 < rank <= 5), "rank": rank, "reciprocalRank": round(1 / rank, 4) if rank else 0,
               "latencyMs": int((time.perf_counter() - started) * 1000),
               "failureReason": "" if 0 < rank <= 3 else f"expected runbook not retrieved in Top3: {expected}"}
        runs.append(run)
        await store.put("eval_runs", run["runId"], run, now_iso())
    total = len(runs)
    summary = {"batchId": batch_id, "mode": mode, "status": "COMPLETED", "totalCases": total,
               "successCases": sum(item["status"] == "SUCCESS" for item in runs),
               "failedCases": sum(item["status"] != "SUCCESS" for item in runs),
               "top1Recall": round(sum(item["top1Hit"] for item in runs) / total, 4),
               "top3Recall": round(sum(item["top3Hit"] for item in runs) / total, 4),
               "top5Recall": round(sum(item["top5Hit"] for item in runs) / total, 4),
               "meanReciprocalRank": round(sum(item["reciprocalRank"] for item in runs) / total, 4),
               "rootCauseHitRate": round(sum(item["top3Hit"] for item in runs) / total, 4),
               "averageLatencyMs": round(sum(item["latencyMs"] for item in runs) / total, 4), "runs": runs}
    artifact_dir = Path("data/runbook-rag-eval") / batch_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path, failures_path = artifact_dir / "report.json", artifact_dir / "report.md", artifact_dir / "failures.json"
    summary.update(reportJsonPath=str(json_path), reportMarkdownPath=str(markdown_path), failureCasesPath=str(failures_path))
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [item for item in runs if item["status"] != "SUCCESS"]
    failures_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text("# Runbook RAG Evaluation Report\n\n" +
        "\n".join(f"- {key}: {summary[key]}" for key in ("batchId", "mode", "totalCases", "successCases", "failedCases", "top1Recall", "top3Recall", "top5Recall", "meanReciprocalRank")) +
        "\n\n| caseId | status | rank | expected |\n|---|---:|---:|---|\n" +
        "\n".join(f"| {item['caseId']} | {item['status']} | {item['rank']} | {item['expectedRunbookIds'][0]} |" for item in runs), encoding="utf-8")
    return summary


def _rag_summary_view(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in ("batchId", "mode", "totalCases", "successCases", "failedCases",
                                               "top1Recall", "top3Recall", "top5Recall", "meanReciprocalRank",
                                               "rootCauseHitRate", "averageLatencyMs", "reportJsonPath",
                                               "reportMarkdownPath", "failureCasesPath")}


def _load_eval_cases(case_id: str | None) -> list[dict[str, Any]]:
    cases = builtin_codeops_eval_cases()
    return [case for case in cases if case_id is None or str(case["caseId"]).lower() == case_id.lower()]


async def _dispatch_scheduled_incident(incident: dict[str, Any]) -> None:
    endpoints = incident.get("affectedEndpoints") if isinstance(incident.get("affectedEndpoints"), list) else []
    endpoint_suffix = f" Affected endpoints: {', '.join(str(item) for item in endpoints)}" if endpoints else ""
    service = incident.get("service") or incident.get("serviceName") or "unknown-service"
    request = CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX",
        goal=(f"{service} {incident.get('alertName', 'unknown')} "
              f"severity={incident.get('severity', 'UNKNOWN')}. Aggregated from {incident.get('alertCount', 0)} "
              f"alerts. {incident.get('summary', '')}{endpoint_suffix}"),
        repository="samples/order-service",
        focusAreas=["incident", "code_location", "bug_fix", "test_verification", "release_risk"],
        context={"serviceName": service,
                 "severity": incident.get("severity", "UNKNOWN"), "alertCount": incident.get("alertCount", 0),
                 "alertName": incident.get("alertName", "unknown"), "affectedEndpoints": endpoints,
                 "scheduledBy": "IncidentScheduler", "evidenceMode": "LIVE",
                 "fixtureFallbackAllowed": False, "allowPatchApply": True, "allowTestPatchApply": True},
        maxRounds=8, maxToolCalls=50)
    state = await codeops_graph.invoke(request)
    task = state["task"]
    await store.put("tasks", task["taskId"], task, task["updateTime"])


def _alert_incident_command(alert: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now()
    try:
        incident_start = datetime.fromisoformat(str(alert.get("startsAt") or ""))
    except ValueError:
        incident_start = now - timedelta(minutes=10)
    start = incident_start - timedelta(minutes=10)
    try:
        alert_end = datetime.fromisoformat(str(alert.get("endsAt") or ""))
    except ValueError:
        alert_end = now
    end = alert_end if alert_end >= start else now
    service = str(alert.get("serviceName") or "unknown-service").strip() or "unknown-service"
    rule = str(alert.get("alertRule") or "UNKNOWN_ALERT").strip() or "UNKNOWN_ALERT"
    severity = str(alert.get("severity") or "P2").strip() or "P2"
    return {"serviceName": service, "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
            "problem": (f"{service} 在最近 10 分钟触发告警 [{rule}]，严重级别 {severity}。请分析 Prometheus 指标、"
                        "ELK 日志、SkyWalking 链路与运维 Runbook，判断根因候选，并给出临时止血和长期优化建议。"),
            "traceId": alert.get("traceId"), "endpoint": alert.get("endpoint", ""),
            "maxStep": max(1, settings.ops_alert_max_step), "sessionId": str(uuid.uuid4()),
            "diagnosisId": f"diag-{uuid.uuid4()}"}


async def _dispatch_alert(alert: dict[str, Any], dispatch: dict[str, Any], command: dict[str, Any]) -> None:
    dispatch.update(dispatchStatus="RUNNING", startTime=now_iso(), updateTime=now_iso())
    await store.put("dispatches", dispatch["dispatchId"], dispatch, dispatch["updateTime"])
    try:
        request = IncidentAnalyzeRequest(serviceName=command["serviceName"], startTime=command["startTime"],
                                         endTime=command["endTime"], problem=command["problem"],
                                         traceId=command.get("traceId") or None, maxStep=command["maxStep"],
                                         endpoint=command.get("endpoint", ""))
        diagnosis = await ops_graph.invoke(request, session_id=command["sessionId"],
                                           diagnosis_id=command["diagnosisId"])
        record = _diagnosis_record(diagnosis)
        await store.put("diagnoses", diagnosis["diagnosis_id"], record, record["updateTime"])
        await _notify_diagnosis(alert, diagnosis, record)
        dispatch.update(dispatchStatus="SUCCESS", endTime=now_iso(), updateTime=now_iso())
    except Exception as exc:
        dispatch.update(dispatchStatus="FAILED", skipReason=str(exc)[:1000], endTime=now_iso(), updateTime=now_iso())
    await store.put("dispatches", dispatch["dispatchId"], dispatch, dispatch["updateTime"])


async def _trigger_codeops_alert(alert: dict[str, Any], command: dict[str, Any]) -> None:
    if not settings.codeops_incident_to_fix_alert_enabled:
        return
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    repository = (labels.get("repository") or labels.get("repo") or labels.get("code_repository")
                  or annotations.get("repository") or annotations.get("repo")
                  or annotations.get("code_repository"))
    def flag(key: str) -> bool:
        return str(labels.get(key, annotations.get(key, "true"))).lower() == "true"
    request = CodeOpsTaskRequest(
        taskType="INCIDENT_TO_FIX",
        goal=(f"{command['serviceName']} 触发线上告警 [{alert['alertRule']}]，问题描述：{command['problem']}。"
              "请完成 Incident-to-Fix：诊断线上证据，抽取异常类名/接口路径/可疑 Service，定位代码，"
              "生成修复补丁草稿、测试验证建议和发布风险观察项。"),
        repository=repository,
        focusAreas=["incident", "code_location", "knowledge_rag", "bug_fix", "test_verification", "release_risk"],
        context={"source": "alertmanager", "evidenceMode": "LIVE", "fixtureFallbackAllowed": False,
                 "eventId": alert["alertId"], "alertRule": alert["alertRule"], "severity": alert["severity"],
                 "fingerprint": alert.get("fingerprint"), "serviceName": command["serviceName"],
                 "startTime": command["startTime"], "endTime": command["endTime"],
                 "traceId": command.get("traceId"), "endpoint": command.get("endpoint") or labels.get("endpoint"),
                 "opsDiagnosisId": command["diagnosisId"], "repository": repository,
                 "allowPatchApply": flag("codeops.allowPatchApply"),
                 "allowTestPatchApply": flag("codeops.allowTestPatchApply"),
                 "alertmanagerPayload": alert.get("rawPayload"),
                 "alertLabels": labels, "alertAnnotations": annotations},
        maxRounds=8, maxToolCalls=50)
    try:
        result = await codeops_graph.invoke(request)
        task = result["task"]
        await store.put("tasks", task["taskId"], task, task["updateTime"])
    except Exception:
        return


async def _notify_diagnosis(alert: dict[str, Any], diagnosis: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    diagnosis_id = diagnosis["diagnosis_id"]
    if not settings.ops_notify_enabled or not settings.ops_notify_email_enabled:
        return await notification_service.skipped(diagnosis_id, alert["serviceName"], alert["severity"], "notification is disabled")
    owner = await service_owner_service.query(alert["serviceName"])
    if not owner:
        return await notification_service.skipped(diagnosis_id, alert["serviceName"], alert["severity"], "service owner is not configured")
    if owner.get("enabled") is False:
        return await notification_service.skipped(diagnosis_id, alert["serviceName"], alert["severity"], "service owner is disabled")
    recipients = list(dict.fromkeys(email.strip() for email in (owner.get("ownerEmail", ""), owner.get("backupOwnerEmail", "")) if email and email.strip()))
    if not recipients:
        return await notification_service.skipped(diagnosis_id, alert["serviceName"], alert["severity"], "owner email is blank")
    command = {"diagnosisId": diagnosis_id, "sessionId": diagnosis["session_id"],
               "startTime": diagnosis["request"]["startTime"], "endTime": diagnosis["request"]["endTime"],
               "problem": diagnosis["request"]["problem"]}
    subject, content = notification_template.build(alert, command, record)
    return await notification_service.send(recipients, subject, content, diagnosis_id,
                                           service_name=alert["serviceName"], severity=alert["severity"])


static_dir = Path(__file__).with_name("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def console() -> FileResponse:
        return FileResponse(static_dir / "ops-console.html")
