from pathlib import Path
import sys

import pytest

from ops_autoagent.config import Settings
from ops_autoagent.ops import (AlertDeduplicator, AlertNormalizer, EvidenceReviewer, EvidenceSignalExtractor,
                               NotificationService, NotificationTemplateService, RunbookRagService,
                               OpsAgentSkillService, OpsChatClientResolver, SensitiveMasker, ServiceOwnerService)
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.schemas import AlertmanagerWebhook, now_iso
from ops_autoagent.store import Store
from ops_autoagent.tools import McpStdioClient


def test_sensitive_masker_and_evidence_review():
    masked = SensitiveMasker().mask("authorization=Bearer abc token=secret user=a@example.com")
    assert "abc" not in masked and "secret" not in masked and "a@example.com" not in masked
    extractor = EvidenceSignalExtractor()
    signals = extractor.extract(
        {"available": True, "raw": {"message": "hikari connection pool timeout"}},
        {"available": True, "raw": {"message": "SQL timeout"}},
        {"available": False, "error": "not configured"},
    )
    assert signals and all("signalId" in signal for signal in signals)
    review = EvidenceReviewer().review(
        {"available": True, "summary": "metrics"}, {"available": True, "summary": "logs"},
        {"available": False}, [], 1, 2, {"serviceName": "orders"})
    assert review["status"] == "NEED_MORE_EVIDENCE"
    assert review["sufficient"] is False
    assert set(review["requiredTools"]) == {"query_skywalking_trace", "query_runbook"}


@pytest.mark.asyncio
async def test_alert_normalization_and_dedup(tmp_path: Path):
    store = Store(tmp_path / "ops.db")
    await store.initialize()
    webhook = AlertmanagerWebhook.model_validate({
        "status": "firing",
        "alerts": [{"status": "firing", "fingerprint": "same", "labels": {
            "alertname": "High5xx", "service": "order-service", "severity": "critical"
        }, "annotations": {"summary": "5xx spike"}}],
    })
    alert = AlertNormalizer().normalize(webhook)[0]
    assert alert["severity"] == "P1"
    dedup = AlertDeduplicator(store, 5)
    first = await dedup.accept(alert)
    assert first["accepted"]
    await store.put("dispatches", "dispatch-1", {"dispatchId": "dispatch-1", "serviceName": "order-service",
                    "dedupKey": first["dedupKey"], "dispatchStatus": "COMPLETED",
                    "createTime": now_iso(), "updateTime": now_iso()}, now_iso())
    duplicate = AlertNormalizer().normalize(webhook)[0]
    assert not (await dedup.accept(duplicate))["accepted"]


@pytest.mark.asyncio
async def test_runbook_chunking_and_hybrid_keyword_search(tmp_path: Path):
    (tmp_path / "database-timeout.md").write_text(
        "# Database timeout\nCheck Hikari connection pool saturation and slow SQL.\n\n## Recovery\nReduce traffic.",
        encoding="utf-8",
    )
    service = RunbookRagService(Settings(ops_runbook_path=tmp_path, ops_runbook_chunk_size=200,
                                         ops_runbook_hybrid_enabled=True, pgvector_url=""))
    result = await service.search("Hikari SQL timeout", 3)
    assert result and result[0]["document"] == "database-timeout.md"
    assert service.governance()["chunkCount"] >= 1


@pytest.mark.asyncio
async def test_service_owner_notification_template_and_skip_record(tmp_path: Path):
    store = Store(tmp_path / "ops.db")
    await store.initialize()
    await store.put("service_owners", "orders", {"serviceName": "orders", "ownerEmail": "owner@example.com", "enabled": True}, "2026-01-01T00:00:00+00:00")
    owner = await ServiceOwnerService(store).query("orders")
    assert owner["ownerEmail"] == "owner@example.com"
    subject, content = NotificationTemplateService("[AutoAgent]", "http://localhost:8099/").build(
        {"serviceName": "orders", "alertName": "Http5xx", "severity": "P1"},
        {"diagnosisId": "diag-1", "sessionId": "s-1", "startTime": "a", "endTime": "b", "problem": "500"},
        {"status": "SUCCESS", "report": "root cause"},
    )
    assert subject == "[AutoAgent] [P1] orders 自动诊断结果"
    assert "/api/v1/ops/incident/record/diag-1" in content
    record = await NotificationService(store).skipped("diag-1", "orders", "P1", "notification is disabled")
    assert record["sendStatus"] == "SKIPPED"


def test_mysql_legacy_contract_mappings_cover_persisted_ops_records():
    samples = {
        "diagnoses": {"diagnosisId": "d", "sessionId": "s", "serviceName": "svc"},
        "alerts": {"alertId": "a", "serviceName": "svc"},
        "dispatches": {"dispatchId": "x", "eventId": "a", "serviceName": "svc"},
        "tool_logs": {"toolCallId": "t", "toolName": "query_prometheus"},
        "notifications": {"notificationId": "n", "serviceName": "svc"},
        "incident_states": {"stateId": "st", "diagnosisId": "d", "sessionId": "s", "serviceName": "svc"},
        "plans": {"planId": "p", "diagnosisId": "d", "stateId": "st"},
        "reviews": {"reviewId": "r", "diagnosisId": "d"},
        "audit_logs": {"auditId": "au"},
        "memories": {"memoryId": "m", "diagnosisId": "d", "serviceName": "svc"},
        "eval_runs": {"runId": "e", "caseId": "c", "top1RootCauseHit": 1},
    }
    for logical_table, payload in samples.items():
        spec = Store._legacy_write_spec(logical_table, {**payload, "createTime": "2026-01-01 00:00:00",
                                                        "updateTime": "2026-01-01 00:00:00"})
        assert spec is not None, logical_table
        assert Store._legacy_id_columns()[logical_table] in spec[1]


@pytest.mark.asyncio
async def test_chat_client_resolver_has_role_specific_ids_and_fallback(tmp_path: Path):
    config = Settings(openai_api_key="test-key", ops_agent_chat_planner_client_id="4101")
    store = Store(tmp_path / "chat.db")
    await store.initialize()
    resolution = await OpsChatClientResolver(config, store, OpenAICompatibleClient(config)).resolve("PLANNER")
    assert resolution.available and resolution.client_id == "4101"
    assert resolution.source == "OPEN_AI_CHAT_MODEL_FALLBACK" and resolution.fallback


def test_ops_skill_front_matter_matching_and_tool_recommendations(tmp_path: Path):
    (tmp_path / "db.md").write_text(
        "---\nskillId: db-pool\nname: DB Pool\ncategory: database\n"
        "matchedAlertRules: HikariPool, connection timeout\nrecommendedTools: query_prometheus, query_elasticsearch\n"
        "temporaryFixes: reduce traffic\nlongTermFixes: tune pool\n---\n# DB Pool", encoding="utf-8")
    service = OpsAgentSkillService(Settings(ops_agent_skill_base_path=tmp_path))
    matches = service.match("HikariPool connection timeout", 3)
    assert matches[0]["skillId"] == "db-pool"
    assert service.recommended_tools(matches) == ["query_prometheus", "query_elasticsearch"]
    assert service.to_runbook_matches(matches)[0]["source"] == "OPS_SKILL"


@pytest.mark.asyncio
async def test_dynamic_stdio_mcp_lists_and_calls_tools(tmp_path: Path):
    server = tmp_path / "mcp_server.py"
    server.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " p=json.loads(line)\n"
        " if 'id' not in p: continue\n"
        " if p['method']=='initialize': r={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'t','version':'1'}}\n"
        " elif p['method']=='tools/list': r={'tools':[{'name':'echo','description':'Echo','inputSchema':{'type':'object'}}]}\n"
        " else: r={'content':[{'type':'text','text':p['params']['arguments']['value']}],'isError':False}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':p['id'],'result':r}),flush=True)\n",
        encoding="utf-8",
    )
    client = McpStdioClient(sys.executable, [str(server)], timeout=5)
    assert (await client.list_tools())[0]["name"] == "echo"
    result = await client.call_tool("echo", {"value": "ok"})
    assert result["content"][0]["text"] == "ok"
