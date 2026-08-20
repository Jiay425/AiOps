from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiosqlite


class Store:
    TABLES = {
        "diagnoses", "tasks", "alerts", "dispatches", "memories", "tool_logs", "notifications",
        "eval_cases", "eval_runs", "eval_metrics", "incident_states", "plans", "reviews", "audit_logs",
        "service_owners", "tool_policies", "approvals", "task_events",
        "artifacts", "runtime_metrics",
    }
    def __init__(self, path: Path, mysql_url: str = "", mysql_username: str = "root", mysql_password: str = "",
                 mysql_pool_min_size: int = 1, mysql_pool_max_size: int = 10,
                 mysql_pool_recycle_seconds: int = 1800, mysql_connect_timeout_seconds: int = 30):
        self.path = path
        self.mysql_url, self.mysql_username, self.mysql_password = mysql_url, mysql_username, mysql_password
        self.mysql_pool_min_size = mysql_pool_min_size
        self.mysql_pool_max_size = mysql_pool_max_size
        self.mysql_pool_recycle_seconds = mysql_pool_recycle_seconds
        self.mysql_connect_timeout_seconds = mysql_connect_timeout_seconds
        self._pool = None

    async def initialize(self) -> None:
        if self.mysql_url:
            await self._initialize_mysql()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            for table in sorted(self.TABLES):
                await db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            await db.commit()

    async def put(self, table: str, key: str, payload: dict[str, Any], updated_at: str) -> None:
        self._validate_table(table)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        if self._pool:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"INSERT INTO {table}(id,payload,updated_at) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE payload=VALUES(payload),updated_at=VALUES(updated_at)",
                                     (key, serialized, updated_at))
                mirror_payload = {**payload, "updateTime": payload.get("updateTime") or updated_at,
                                  "createTime": payload.get("createTime") or updated_at}
                await self._mirror_legacy(cursor, table, mirror_payload)
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"INSERT INTO {table}(id, payload, updated_at) VALUES(?,?,?) "
                f"ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (key, serialized, updated_at),
            )
            await db.commit()

    async def get(self, table: str, key: str) -> dict[str, Any] | None:
        self._validate_table(table)
        if self._pool:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"SELECT payload FROM {table} WHERE id=%s", (key,))
                row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return await self._legacy_get(table, key)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(f"SELECT payload FROM {table} WHERE id=?", (key,))
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def recent(self, table: str, limit: int = 20) -> list[dict[str, Any]]:
        self._validate_table(table)
        safe_limit = max(1, min(int(limit), 10000))
        if self._pool:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"SELECT payload FROM {table} ORDER BY updated_at DESC LIMIT {safe_limit}")
                rows = await cursor.fetchall()
            values = [json.loads(row[0]) for row in rows]
            return values or await self._legacy_recent(table, safe_limit)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(f"SELECT payload FROM {table} ORDER BY updated_at DESC LIMIT ?", (safe_limit,))
            rows = await cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def delete(self, table: str, key: str) -> bool:
        self._validate_table(table)
        if self._pool:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"DELETE FROM {table} WHERE id=%s", (key,))
                return cursor.rowcount > 0
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(f"DELETE FROM {table} WHERE id=?", (key,))
            await db.commit()
        return cursor.rowcount > 0

    async def find(self, table: str, predicate, limit: int = 500) -> list[dict[str, Any]]:
        return [item for item in await self.recent(table, limit) if predicate(item)]

    async def close(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _initialize_mysql(self) -> None:
        import aiomysql
        raw = self.mysql_url.removeprefix("jdbc:")
        parsed = urlparse(raw)
        if parsed.scheme not in {"mysql", "mariadb"} or not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("MYSQL_URL must be mysql://host:port/database or jdbc:mysql://host:port/database")
        query = parse_qs(parsed.query)
        charset = query.get("characterEncoding", ["utf8mb4"])[0].lower().replace("-", "")
        if charset == "utf8":
            charset = "utf8mb4"
        self._pool = await aiomysql.create_pool(
            host=parsed.hostname, port=parsed.port or 3306, db=parsed.path.strip("/"),
            user=parsed.username or self.mysql_username, password=parsed.password or self.mysql_password,
            charset=charset, autocommit=True,
            minsize=max(1, self.mysql_pool_min_size),
            maxsize=max(self.mysql_pool_min_size, self.mysql_pool_max_size),
            pool_recycle=max(1, self.mysql_pool_recycle_seconds),
            connect_timeout=max(1, self.mysql_connect_timeout_seconds),
        )
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            for table in sorted(self.TABLES):
                await cursor.execute(f"CREATE TABLE IF NOT EXISTS {table} (id VARCHAR(191) PRIMARY KEY, payload LONGTEXT NOT NULL, updated_at VARCHAR(64) NOT NULL, INDEX idx_{table}_updated_at(updated_at)) CHARACTER SET utf8mb4")

    @staticmethod
    def _validate_table(table: str) -> None:
        if table not in Store.TABLES:
            raise ValueError(f"Unsupported table: {table}")

    async def query_legacy_service_owner(self, service_name: str) -> dict[str, Any] | None:
        """Read the original normalized owner table when the migrated MySQL schema is in use."""
        if not self._pool:
            return None
        sql = ("SELECT id,service_name,owner_name,owner_email,owner_wecom,owner_dingtalk,"
               "backup_owner_email,enabled,create_time,update_time FROM ops_service_owner "
               "WHERE service_name=%s LIMIT 1")
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(sql, (service_name,))
                row = await cursor.fetchone()
        except Exception:
            return None
        if not row:
            return None
        keys = ("id", "serviceName", "ownerName", "ownerEmail", "ownerWecom", "ownerDingTalk",
                "backupOwnerEmail", "enabled", "createTime", "updateTime")
        return dict(zip(keys, row, strict=True))

    async def load_ai_client(self, client_id: str) -> dict[str, Any] | None:
        """Resolve the original ai_client armory relationship graph from MySQL."""
        if not self._pool:
            return None
        sql = """
            SELECT c.client_id,c.client_name,m.model_name,a.base_url,a.api_key,a.completions_path,
                   p.prompt_content
            FROM ai_client c
            JOIN ai_client_config cm ON cm.source_type='client' AND cm.source_id=c.client_id
                                     AND cm.target_type='model' AND cm.status=1
            JOIN ai_client_model m ON m.model_id=cm.target_id AND m.status=1
            JOIN ai_client_api a ON a.api_id=m.api_id AND a.status=1
            LEFT JOIN ai_client_config cp ON cp.source_type='client' AND cp.source_id=c.client_id
                                          AND cp.target_type='prompt' AND cp.status=1
            LEFT JOIN ai_client_system_prompt p ON p.prompt_id=cp.target_id AND p.status=1
            WHERE c.client_id=%s AND c.status=1 LIMIT 1
        """
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(sql, (client_id,))
                row = await cursor.fetchone()
                if not row:
                    return None
                await cursor.execute("""
                    SELECT a.advisor_id,a.advisor_name,a.advisor_type,a.order_num,a.ext_param
                    FROM ai_client_config cc
                    JOIN ai_client_advisor a ON a.advisor_id=cc.target_id AND a.status=1
                    WHERE cc.source_type='client' AND cc.source_id=%s
                      AND cc.target_type='advisor' AND cc.status=1
                    ORDER BY a.order_num,a.id
                """, (client_id,))
                advisor_rows = await cursor.fetchall()
                await cursor.execute("""
                    SELECT DISTINCT t.mcp_id,t.mcp_name,t.transport_type,t.transport_config,t.request_timeout
                    FROM ai_client_config client_model
                    JOIN ai_client_config model_tool
                      ON model_tool.source_type='model' AND model_tool.source_id=client_model.target_id
                     AND model_tool.target_type='tool_mcp' AND model_tool.status=1
                    JOIN ai_client_tool_mcp t ON t.mcp_id=model_tool.target_id AND t.status=1
                    WHERE client_model.source_type='client' AND client_model.source_id=%s
                      AND client_model.target_type='model' AND client_model.status=1
                    ORDER BY t.id
                """, (client_id,))
                mcp_rows = await cursor.fetchall()
                await cursor.execute("""
                    SELECT r.rag_id,r.rag_name,r.knowledge_tag
                    FROM ai_client_config cc
                    JOIN ai_client_rag_order r ON r.rag_id=cc.target_id AND r.status=1
                    WHERE cc.source_type='client' AND cc.source_id=%s
                      AND cc.target_type='rag' AND cc.status=1 ORDER BY r.id
                """, (client_id,))
                rag_rows = await cursor.fetchall()
        except Exception:
            return None
        keys = ("clientId", "clientName", "model", "baseUrl", "apiKey", "completionsPath", "systemPrompt")
        config = dict(zip(keys, row, strict=True))
        config["advisors"] = [
            {"advisorId": item[0], "advisorName": item[1], "advisorType": item[2], "orderNum": item[3],
             "extParam": self._json_or_empty(item[4])} for item in advisor_rows
        ]
        config["mcpTools"] = [
            {"mcpId": item[0], "mcpName": item[1], "transportType": item[2],
             "transportConfig": self._json_or_empty(item[3]), "requestTimeout": item[4]}
            for item in mcp_rows
        ]
        config["ragOrders"] = [
            {"ragId": item[0], "ragName": item[1], "knowledgeTag": item[2]} for item in rag_rows
        ]
        return config

    @staticmethod
    def _json_or_empty(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    async def _mirror_legacy(self, cursor: Any, table: str, payload: dict[str, Any]) -> None:
        spec = self._legacy_write_spec(table, payload)
        if not spec:
            return
        legacy_table, values = spec
        columns = list(values)
        updates = [column for column in columns if column not in {self._legacy_id_columns()[table], "create_time"}]
        sql = (f"INSERT INTO {legacy_table} (`{'`,`'.join(columns)}`) VALUES "
               f"({','.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE "
               + ",".join(f"`{column}`=VALUES(`{column}`)" for column in updates))
        try:
            await cursor.execute(sql, tuple(values.values()))
        except Exception:
            # The JSON sidecar remains authoritative until the legacy migration scripts have been applied.
            return

    async def _legacy_get(self, table: str, key: str) -> dict[str, Any] | None:
        metadata = self._legacy_metadata().get(table)
        if not metadata or not self._pool:
            return None
        legacy_table, id_column = metadata
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"SELECT * FROM {legacy_table} WHERE {id_column}=%s LIMIT 1", (key,))
                row = await cursor.fetchone()
                columns = [item[0] for item in cursor.description] if cursor.description else []
        except Exception:
            return None
        return self._legacy_row(columns, row) if row else None

    async def _legacy_recent(self, table: str, limit: int) -> list[dict[str, Any]]:
        metadata = self._legacy_metadata().get(table)
        if not metadata or not self._pool:
            return []
        legacy_table, _ = metadata
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute(f"SELECT * FROM {legacy_table} ORDER BY id DESC LIMIT {limit}")
                rows = await cursor.fetchall()
                columns = [item[0] for item in cursor.description] if cursor.description else []
        except Exception:
            return []
        return [self._legacy_row(columns, row) for row in rows]

    @staticmethod
    def _legacy_row(columns: list[str], row: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for column, value in zip(columns, row, strict=True):
            name = Store._camel(column)
            if column.endswith("_json") and isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            result[name] = value
        return result

    @staticmethod
    def _camel(value: str) -> str:
        head, *tail = value.split("_")
        return head + "".join(item.capitalize() for item in tail)

    @staticmethod
    def _legacy_metadata() -> dict[str, tuple[str, str]]:
        return {
            "diagnoses": ("ops_incident_diagnosis", "diagnosis_id"),
            "alerts": ("ops_alert_event", "event_id"),
            "dispatches": ("ops_diagnosis_dispatch", "dispatch_id"),
            "memories": ("ops_historical_incident_memory", "memory_id"),
            "tool_logs": ("ops_tool_call_log", "call_id"),
            "notifications": ("ops_notification_record", "notification_id"),
            "eval_runs": ("ops_eval_run", "run_id"),
            "eval_cases": ("ops_eval_case", "case_id"),
            "eval_metrics": ("ops_eval_metric", "id"),
            "incident_states": ("ops_incident_state", "state_id"),
            "plans": ("ops_investigation_plan", "plan_id"),
            "reviews": ("ops_agent_review", "review_id"),
            "audit_logs": ("ops_audit_log", "audit_id"),
            "service_owners": ("ops_service_owner", "service_name"),
            "tool_policies": ("ops_tool_policy", "tool_name"),
        }

    @staticmethod
    def _legacy_id_columns() -> dict[str, str]:
        return {key: value[1] for key, value in Store._legacy_metadata().items()}

    @staticmethod
    def _legacy_write_spec(table: str, value: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        def dump(item: Any) -> str:
            return json.dumps(item, ensure_ascii=False, default=str) if not isinstance(item, str) else item
        now = value.get("updateTime") or value.get("createTime")
        specs: dict[str, tuple[str, dict[str, Any]]] = {
            "diagnoses": ("ops_incident_diagnosis", {
                "diagnosis_id": value.get("diagnosisId"), "session_id": value.get("sessionId"),
                "service_name": value.get("serviceName"), "start_time": value.get("startTime"),
                "end_time": value.get("endTime"), "problem": value.get("problem"), "trace_id": value.get("traceId"),
                "status": value.get("status", "FAILED"), "request_json": dump(value.get("requestJson", {})),
                "metric_evidence_json": dump(value.get("metricEvidenceJson", {})),
                "log_evidence_json": dump(value.get("logEvidenceJson", {})),
                "trace_evidence_json": dump(value.get("traceEvidenceJson", {})),
                "evidence_chain_json": dump(value.get("evidenceChainJson", {})),
                "runbook_json": dump(value.get("runbookJson", {})), "report": value.get("report", ""),
                "error_message": value.get("errorMessage", ""), "create_time": value.get("createTime", now),
                "update_time": now}),
            "alerts": ("ops_alert_event", {
                "event_id": value.get("alertId"), "source": "alertmanager", "service_name": value.get("serviceName"),
                "alert_rule": value.get("alertName", "unknown-alert"), "severity": value.get("severity"),
                "status": value.get("status", "FIRING"), "fingerprint": value.get("fingerprint"),
                "trace_id": value.get("traceId"), "starts_at": value.get("startsAt"), "ends_at": value.get("endsAt"),
                "labels_json": dump(value.get("labels", {})), "annotations_json": dump(value.get("annotations", {})),
                "raw_payload": dump(value), "received_time": value.get("createTime", now),
                "create_time": value.get("createTime", now)}),
            "dispatches": ("ops_diagnosis_dispatch", {
                "dispatch_id": value.get("dispatchId"), "event_id": value.get("eventId"),
                "diagnosis_id": value.get("diagnosisId"), "service_name": value.get("serviceName"),
                "dedup_key": value.get("dedupKey") or value.get("eventId"),
                "dispatch_status": value.get("dispatchStatus", "NEW"),
                "skip_reason": value.get("skipReason") or value.get("errorMessage"),
                "create_time": value.get("createTime", now), "start_time": value.get("startTime"),
                "end_time": value.get("endTime"), "update_time": now}),
            "tool_logs": ("ops_tool_call_log", {
                "call_id": value.get("toolCallId") or value.get("callId"), "session_id": value.get("sessionId"),
                "diagnosis_id": value.get("diagnosisId"), "tool_name": value.get("toolName", "unknown"),
                "logical_tool_name": value.get("logicalToolName") or value.get("toolName"),
                "protocol": value.get("protocol"), "governance_decision": value.get("governanceDecision"),
                "target": value.get("target"), "request_summary": dump(value.get("request", value.get("requestSummary", {}))),
                "response_summary": dump(value.get("response", value.get("responseSummary", {}))),
                "status_code": value.get("statusCode"), "cost_millis": value.get("costMillis", 0),
                "success": str(value.get("success", False)).lower(), "error_message": value.get("errorMessage"),
                "create_time": value.get("createTime", now)}),
            "notifications": ("ops_notification_record", {
                "notification_id": value.get("notificationId"), "diagnosis_id": value.get("diagnosisId"),
                "service_name": value.get("serviceName", "unknown-service"), "channel": value.get("channel", "EMAIL"),
                "receiver": value.get("receiver"), "severity": value.get("severity"), "subject": value.get("subject"),
                "send_status": value.get("sendStatus") or value.get("status", "SKIPPED"),
                "retry_count": value.get("retryCount", 0), "error_message": value.get("errorMessage"),
                "send_time": value.get("sendTime") or now, "create_time": value.get("createTime", now)}),
            "incident_states": ("ops_incident_state", {
                "state_id": value.get("stateId"), "diagnosis_id": value.get("diagnosisId"),
                "session_id": value.get("sessionId"), "event_id": value.get("eventId"),
                "service_name": value.get("serviceName"), "severity": value.get("severity"),
                "alert_rule": value.get("alertRule"), "time_window_json": dump(value.get("timeWindow", {})),
                "current_round": value.get("currentRound", 1), "max_rounds": value.get("maxRounds", 2),
                "plan_json": dump(value.get("plan", {})),
                "metrics_evidence_json": dump(value.get("metricsEvidence", {})),
                "log_evidence_json": dump(value.get("logEvidence", {})),
                "trace_evidence_json": dump(value.get("traceEvidence", {})),
                "runbook_evidence_json": dump(value.get("runbookEvidence", [])),
                "candidate_root_causes_json": dump(value.get("candidateRootCauses", [])),
                "missing_evidence_json": dump(value.get("missingEvidence", [])),
                "tool_history_json": dump(value.get("toolHistory", [])), "review_status": value.get("reviewStatus"),
                "final_report": value.get("finalReport"), "status": value.get("status", "INIT"),
                "error_message": value.get("errorMessage"), "create_time": value.get("createTime", now),
                "update_time": now}),
            "plans": ("ops_investigation_plan", {
                "plan_id": value.get("planId"), "diagnosis_id": value.get("diagnosisId"),
                "state_id": value.get("stateId"), "round": value.get("round", 1),
                "alert_type": value.get("alertType", "GENERAL"), "hypotheses_json": dump(value.get("hypotheses", [])),
                "steps_json": dump(value.get("steps", [])),
                "required_tools_json": dump([step.get("tool") for step in value.get("steps", [])]),
                "expected_evidence_json": dump(value.get("expectedEvidence", [])),
                "risk_level": value.get("riskLevel", "MEDIUM"), "budget_json": dump({"maxSteps": value.get("maxSteps")}),
                "plan_json": dump(value), "planner_type": value.get("plannerType", "RULE_BASED"),
                "create_time": value.get("createTime", now), "update_time": now}),
            "reviews": ("ops_agent_review", {
                "review_id": value.get("reviewId"), "diagnosis_id": value.get("diagnosisId"),
                "state_id": value.get("stateId"), "plan_id": value.get("planId"), "round": value.get("round", 1),
                "review_status": value.get("status", value.get("reviewStatus", "INSUFFICIENT_FINAL")),
                "sufficient": value.get("sufficient", False), "confidence": value.get("confidenceScore", 0),
                "confirmed_facts_json": dump(value.get("confirmedFacts", [])),
                "weak_evidence_json": dump(value.get("weakEvidence", [])),
                "missing_evidence_json": dump(value.get("missingEvidence", [])),
                "next_actions_json": dump(value.get("requiredTools", [])),
                "report_constraints_json": dump(value.get("reportConstraints", [])),
                "stop_reason": value.get("stopReason", ""), "reviewer_type": value.get("reviewerType", "RULE_BASED"),
                "review_json": dump(value), "create_time": value.get("createTime", now), "update_time": now}),
            "audit_logs": ("ops_audit_log", {
                "audit_id": value.get("auditId"), "session_id": value.get("sessionId"),
                "diagnosis_id": value.get("diagnosisId"), "operator_id": value.get("operatorId"),
                "client_ip": value.get("clientIp"), "action": value.get("action", "OPS_API_REQUEST"),
                "resource": value.get("resource"), "request_json": dump(value.get("requestJson", {})),
                "result": value.get("result", "ALLOW"), "reason": value.get("reason"),
                "create_time": value.get("createTime", now)}),
            "memories": ("ops_historical_incident_memory", {
                "memory_id": value.get("memoryId"), "diagnosis_id": value.get("diagnosisId"),
                "service_name": value.get("serviceName"), "alert_rule": value.get("alertRule") or value.get("problem"),
                "severity": value.get("severity"), "symptom_summary": value.get("problem", "")[:1000],
                "evidence_summary": dump(value.get("evidence", [])), "root_cause_category": value.get("category"),
                "root_cause_summary": value.get("rootCause", value.get("category")),
                "remediation_summary": value.get("report", "")[:4000], "confidence": value.get("confidence", 0),
                "review_status": value.get("reviewStatus", "SUFFICIENT"),
                "time_window_json": dump(value.get("timeWindow", {})), "tags": dump(value.get("tags", [])),
                "similarity_text": " ".join((str(value.get("problem", "")), str(value.get("category", "")))),
                "source_record_json": dump(value), "create_time": value.get("createTime", now), "update_time": now}),
            "eval_runs": ("ops_eval_run", {
                "run_id": value.get("runId"), "case_id": value.get("caseId"),
                "diagnosis_id": value.get("diagnosisId"), "status": value.get("status", "FAILED"),
                "top1_root_cause_hit": value.get("top1RootCauseHit", 0),
                "top3_root_cause_hit": value.get("top3RootCauseHit", 0),
                "required_evidence_coverage": value.get("requiredEvidenceCoverage", 0),
                "unsupported_conclusion_count": value.get("unsupportedConclusionCount", 0),
                "tool_call_count": value.get("toolCallCount", 0),
                "diagnosis_latency_ms": value.get("diagnosisLatencyMs", value.get("latencyMs", 0)),
                "final_status": value.get("finalStatus"), "summary_json": dump(value),
                "error_message": value.get("errorMessage") or value.get("failureReason"),
                "create_time": value.get("createTime", now), "update_time": now}),
            "eval_cases": ("ops_eval_case", {
                "case_id": value.get("caseId"), "case_name": value.get("caseName"),
                "service_name": value.get("serviceName"),
                "alert_payload_json": dump(value.get("alertPayloadJson", {})),
                "problem": value.get("problem"), "expected_root_cause": value.get("expectedRootCause"),
                "expected_evidence_types_json": dump(value.get("expectedEvidenceTypesJson", [])),
                "expected_tools_json": dump(value.get("expectedToolsJson", [])),
                "golden_summary": value.get("goldenSummary"), "severity": value.get("severity"),
                "tags": value.get("tags"), "enabled": value.get("enabled", 1),
                "create_time": value.get("createTime", now), "update_time": now}),
            "eval_metrics": ("ops_eval_metric", {
                "id": value.get("id"), "run_id": value.get("runId"), "case_id": value.get("caseId"),
                "metric_name": value.get("metricName"), "metric_value": value.get("metricValue"),
                "metric_detail_json": dump(value.get("metricDetailJson", {})),
                "create_time": value.get("createTime", now)}),
        }
        if table == "memories" and value.get("memoryType") == "CODEOPS":
            return None
        if table == "audit_logs" and not value.get("auditId"):
            return None
        if table == "eval_runs" and "top1RootCauseHit" not in value:
            return None
        return specs.get(table)
