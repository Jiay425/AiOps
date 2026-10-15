"""The local live chain must not borrow evaluation evidence or a sample repo."""

import json
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import ops_autoagent.api as api_module

from ops_autoagent.codeops.orchestrator import IncidentFixOrchestratorPolicy
from ops_autoagent.codeops.services import IncidentScheduler
from ops_autoagent.config import Settings
from ops_autoagent.graphs.codeops import CodeOpsGraph
from ops_autoagent.incident_routing import (IncidentRepositoryUnmapped, build_scheduled_incident_request,
                                            resolve_incident_repository)
from ops_autoagent.llm import OpenAICompatibleClient
from ops_autoagent.tools import ObservabilityTools
from ops_autoagent.store import Store


def test_production_alert_uses_operator_mapping_and_never_sample_fallback(tmp_path: Path):
    repo = tmp_path / "order-service"
    repo.mkdir()
    settings = Settings(ops_runtime_mode="production", ops_repository_root=tmp_path,
                        ops_service_repositories_json=json.dumps({"order-service": "order-service"}))
    incident = {"serviceName": "order-service", "repository": "samples/untrusted-repo"}
    assert resolve_incident_repository(incident, settings) == str(repo)
    with pytest.raises(IncidentRepositoryUnmapped, match="No repository mapping"):
        resolve_incident_repository({"serviceName": "unknown", "repository": "samples/order-service"}, settings)


def test_repository_mapping_rejects_missing_and_outside_paths(tmp_path: Path):
    for value in ("missing", "../outside"):
        settings = Settings(ops_runtime_mode="production", ops_repository_root=tmp_path,
                            ops_service_repositories_json=json.dumps({"order-service": value}))
        with pytest.raises(IncidentRepositoryUnmapped):
            resolve_incident_repository({"serviceName": "order-service"}, settings)


def test_scheduled_request_preserves_alert_facts_and_does_not_force_code_fix(tmp_path: Path):
    first_seen = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    request = build_scheduled_incident_request({
        "serviceName": "order-service", "alertName": "OrderServiceLatencyHigh",
        "highestSeverity": "P1", "latestSummary": "Dependency latency increased",
        "alertCount": 3, "firstSeen": first_seen,
        "affectedEndpoints": ["GET /api/orders/dependency-latency"],
        "incidentId": "incident-1", "traceId": "trace-1",
    }, str(tmp_path))
    assert "severity=P1" in request.goal and "Dependency latency increased" in request.goal
    assert request.context["startTime"].startswith("2026-09-01T11:50")
    assert request.context["traceId"] == "trace-1"
    assert request.context["evidenceMode"] == "LIVE"
    assert request.context["fixtureFallbackAllowed"] is False
    assert request.context["autonomousTriage"] is True
    assert "bug_fix" not in request.focus_areas


def test_scheduled_request_interprets_existing_naive_alert_time_as_local(tmp_path: Path):
    local_first_seen = datetime(2026, 9, 1, 12, 0)
    request = build_scheduled_incident_request({
        "serviceName": "order-service", "firstSeen": local_first_seen.isoformat(),
    }, str(tmp_path))
    expected = (local_first_seen.astimezone(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert request.context["startTime"] == expected
    assert datetime.fromisoformat(request.context["startTime"]).tzinfo == timezone.utc


def test_autonomous_triage_respects_no_code_and_requires_localization():
    policy = IncidentFixOrchestratorPolicy()
    context = {"autonomousTriage": True, "allowPatchApply": True, "allowTestPatchApply": True}
    localized = {"targetFiles": ["src/OrderService.java"], "localizationBlocking": False}
    memory = {"codeLocalization": localized,
              "fixStrategy": {"strategyType": "NO_CODE_FIX", "shouldEnterCodeRepair": False}}
    assert policy._should_repair(memory, context, ["bug_fix"], [policy.REPO]) is False
    memory["fixStrategy"] = {"strategyType": "CODE_FIX", "shouldEnterCodeRepair": True}
    assert policy._should_repair(memory, context, [], []) is False
    assert policy._should_repair(memory, context, [], [policy.REPO]) is True


def test_production_observability_never_falls_back_to_incident_fixture():
    settings = Settings(ops_runtime_mode="production", ops_fixture_fallback=True,
                        prometheus_base_url="")
    result = ObservabilityTools(settings)._fixture_or_error(
        "order-service", "prometheus.json", RuntimeError("gateway offline"))
    assert result["available"] is False
    assert result["source"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_live_codeops_rejects_declared_eval_fixture():
    settings = Settings(ops_runtime_mode="production", openai_api_key="")
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    with pytest.raises(ValueError, match="LIVE_EVIDENCE_FIXTURE_FORBIDDEN"):
        await graph._skill_ops_diagnosis({
            "context": {"serviceName": "order-service", "evidenceMode": "LIVE",
                        "fixtureEvidence": {"prometheus": {"signal": "synthetic"}}},
            "task": {"goal": "diagnose"},
        })


@pytest.mark.asyncio
async def test_live_ops_evidence_queries_three_gateways_concurrently_without_llm():
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def observe(source: str) -> dict:
        started.add(source)
        if len(started) == 3:
            all_started.set()
        await release.wait()
        return {"source": source, "available": True, "observations": []}

    class Gateways:
        async def prometheus(self, *_):
            return await observe("Prometheus")

        async def elk(self, *_):
            return await observe("Elasticsearch")

        async def skywalking(self, *_):
            return await observe("SkyWalking")

    graph = CodeOpsGraph(OpenAICompatibleClient(Settings(openai_api_key="")))
    graph.observability = Gateways()
    graph.runbook_rag = None
    state = {"task": {"taskId": "live-evidence", "goal": "investigate an alert"},
             "context": {"serviceName": "order-service", "evidenceMode": "LIVE",
                         "startTime": "2026-09-01T00:00:00Z", "endTime": "2026-09-01T00:10:00Z"},
             "round": 0, "tool_calls": 0, "steps": []}
    task = asyncio.create_task(graph._skill_ops_diagnosis(state))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=3)
        release.set()
        result = await asyncio.wait_for(task, timeout=3)
        assert result["working_memory"]["opsEvidence"]["evidenceCoverage"]["realAvailableSources"] == 3
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_scheduler_restores_queued_incident_once_after_restart(tmp_path: Path):
    path = tmp_path / "queue.json"
    async def unused(_: dict) -> None:
        raise AssertionError("first scheduler must not execute")

    first = IncidentScheduler(unused, queue_file=path)
    await first.enqueue({"incidentId": "incident-1", "serviceName": "order-service", "severity": "P1"})
    assert path.is_file()

    seen: list[str] = []
    async def handle(incident: dict) -> None:
        seen.append(incident["incidentId"])

    restored = IncidentScheduler(handle, queue_file=path, max_concurrent=1)
    await restored.start()
    try:
        await restored.enqueue({"incidentId": "incident-1", "serviceName": "order-service"})
        await asyncio.wait_for(restored.queue.join(), timeout=3)
        assert seen == ["incident-1"]
    finally:
        await restored.stop()


@pytest.mark.asyncio
async def test_scheduler_orders_p1_p2_p3_by_severity(tmp_path: Path):
    dispatched: list[str] = []

    async def handle(incident: dict) -> None:
        dispatched.append(incident["incidentId"])

    scheduler = IncidentScheduler(handle, max_concurrent=1, queue_file=tmp_path / "queue.json")
    for severity in ("P3", "P2", "P1"):
        await scheduler.enqueue({"incidentId": severity, "serviceName": severity, "severity": severity})
    await scheduler.start()
    try:
        await asyncio.wait_for(scheduler.queue.join(), timeout=3)
        assert dispatched == ["P1", "P2", "P3"]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_busy_service_does_not_block_other_service_or_lose_priority(tmp_path: Path):
    first_started = asyncio.Event()
    other_started = asyncio.Event()
    release_first = asyncio.Event()
    dispatched: list[str] = []

    async def handle(incident: dict) -> None:
        name = incident["incidentId"]
        dispatched.append(name)
        if name == "a-first":
            first_started.set()
            await release_first.wait()
        elif name == "b":
            other_started.set()

    queue_file = tmp_path / "queue.json"
    scheduler = IncidentScheduler(handle, max_concurrent=2, max_per_service=1, queue_file=queue_file)
    await scheduler.enqueue({"incidentId": "a-first", "serviceName": "a", "severity": "P1"})
    await scheduler.start()
    try:
        await asyncio.wait_for(first_started.wait(), timeout=3)
        await scheduler.enqueue({"incidentId": "a-second", "serviceName": "a", "severity": "P1"})
        await scheduler.enqueue({"incidentId": "b", "serviceName": "b", "severity": "P2"})
        await asyncio.wait_for(other_started.wait(), timeout=3)
        assert "a-second" not in dispatched
        assert scheduler.status()["queueStats"]["queueSize"] == 1
        assert any(item["incidentId"] == "a-second" for item in json.loads(queue_file.read_text())["queue"])
        release_first.set()
        await asyncio.wait_for(scheduler.queue.join(), timeout=3)
        assert dispatched == ["a-first", "b", "a-second"]
    finally:
        release_first.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_unmapped_scheduled_alert_stops_before_graph(tmp_path: Path, monkeypatch):
    database = Store(tmp_path / "incidents.db")
    await database.initialize()
    monkeypatch.setattr(api_module, "store", database)
    monkeypatch.setattr(api_module, "settings", Settings(
        ops_runtime_mode="production", ops_repository_root=tmp_path,
        ops_service_repositories_json="{}"))

    async def unexpected(_: object) -> None:
        raise AssertionError("CodeOps graph must not run without a repository mapping")

    monkeypatch.setattr(api_module.codeops_graph, "invoke", unexpected)
    await api_module._dispatch_scheduled_incident({"incidentId": "incident-unmapped",
                                                   "serviceName": "unknown-service"})
    incident = await database.get("incidents", "incident-unmapped")
    assert incident["status"] == "REPOSITORY_UNMAPPED"
    assert "No repository mapping" in incident["blockedReason"]


@pytest.mark.asyncio
async def test_scheduler_replay_recovers_existing_task_link(tmp_path: Path, monkeypatch):
    database = Store(tmp_path / "incidents.db")
    await database.initialize()
    await database.put("tasks", "task-existing", {
        "taskId": "task-existing", "status": "WAITING_APPROVAL",
        "context": {"incidentId": "incident-1"}}, "2026-09-01T00:00:00Z")
    monkeypatch.setattr(api_module, "store", database)

    async def unexpected(_: object) -> None:
        raise AssertionError("replay must not create a second CodeOps task")

    monkeypatch.setattr(api_module.codeops_graph, "invoke", unexpected)
    await api_module._dispatch_scheduled_incident({"incidentId": "incident-1",
                                                   "serviceName": "order-service"})
    incident = await database.get("incidents", "incident-1")
    assert incident["taskId"] == "task-existing"
    assert incident["status"] == "WAITING_APPROVAL"
