import asyncio
from pathlib import Path

import pytest

from ops_autoagent.codeops import (
    AgentLoopService, CodeOpsHookService, CodeOpsTaskDagService, EngineeringToolGateway, IncidentScheduler,
    PatchDiffAnalysis, PatchProposal,
    PatchSandbox, PatchScopeGuard, PatchValidation, RepositoryToolkit, SecurityPolicy, ToolBudget, ToolRuntime,
)
from ops_autoagent.graphs import CodeOpsGraph
from ops_autoagent.codeops.runtime import FilePatch, TestResult as MavenResult, TestRunner


def test_repository_toolkit_enforces_repository_boundary(tmp_path: Path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tools = RepositoryToolkit(tmp_path, ToolBudget(10))
    assert tools.search(["return"])[0]["file"] == "app.py"
    with pytest.raises(PermissionError):
        tools.read("../secret.txt")


def test_patch_sandbox_isolated_then_checked_apply(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    proposal = PatchProposal.from_llm('{"summary":"fix","patches":[{"path":"app.py","old":"return 1","new":"return 2"}],"tests":[]}')
    sandbox = PatchSandbox(tmp_path)
    result = sandbox.apply(proposal)
    assert result.success
    assert "return 1" in source.read_text(encoding="utf-8")
    assert "return 2" in (Path(result.sandbox) / "app.py").read_text(encoding="utf-8")
    assert sandbox.apply_to_repository(proposal, result.checksums) == ["app.py"]
    assert "return 2" in source.read_text(encoding="utf-8")
    PatchSandbox.cleanup(result.sandbox)


@pytest.mark.asyncio
async def test_engineering_gateway_matches_legacy_registry_and_audits(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("token = 'not-a-real-secret'\n", encoding="utf-8")
    runtime = ToolRuntime()
    gateway = EngineeringToolGateway(runtime)
    expected = {
        "repo.create_snapshot", "repo.search_text", "repo.list_files", "repo.read_file_snippet",
        "repo.git_diff", "repo.git_log", "repo.find_tests", "repo.maven", "repo.maven_background",
        "repo.exact_replace", "task.background_status", "knowledge.search", "ops.query_prometheus",
        "ops.search_logs", "ops.query_trace", "artifact.generate_review_report",
    }
    assert {item["toolName"] for item in gateway.list_tools()} == expected
    assert {item["toolName"] for item in gateway.list_registered_tools()} == {
        "repo.create_snapshot", "repo.search_text", "repo.read_file_snippet", "repo.git_diff", "repo.maven",
        "repo.maven_background", "repo.exact_replace", "task.background_status",
    }
    result = await gateway.invoke("repo.search_text", {"repository": str(tmp_path), "queries": ["token"], "apiKey": "secret-value"})
    assert result[0]["file"] == "src/service.py"
    assert runtime.list_recent(1)[0]["metadata"]["apiKey"] == "***"
    replaced = await gateway.invoke("repo.exact_replace", {
        "repository": str(tmp_path), "filePath": "src/service.py",
        "oldText": "token = 'not-a-real-secret'", "newText": "token = 'masked'",
    })
    assert replaced["updated"] is True
    assert "token = 'masked'" in (tmp_path / "src" / "service.py").read_text(encoding="utf-8")


def test_permission_policy_blocks_dangerous_commands_and_escaped_writes(tmp_path: Path):
    policy = SecurityPolicy()
    assert policy.is_command_allowed("mvn -q test")
    assert not policy.is_command_allowed("mvn test $(whoami)")
    assert policy.is_write_allowed(tmp_path, "src/app.py", source_only=True)
    assert not policy.is_write_allowed(tmp_path, "../outside.py")


def test_patch_scope_validation_and_diff_risk(tmp_path: Path):
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    proposal = PatchProposal.from_llm('{"patches":[{"path":"src/service.py","old":"return 1","new":"return 2"}]}')
    guard = PatchScopeGuard().validate(tmp_path, proposal, {
        "scopeType": "STRICT_SINGLE_METHOD", "targetFiles": ["src/service.py"], "targetMethods": ["run"],
    })
    assert guard["passed"]
    sandbox = PatchSandbox(tmp_path).apply(proposal)
    validation = PatchValidation().validate(tmp_path, sandbox.diff)
    analysis = PatchDiffAnalysis().analyze(sandbox.diff, validation, guard)
    assert validation["valid"]
    assert analysis["scopeAligned"] and analysis["minimalChangeScore"] == 100
    PatchSandbox.cleanup(sandbox.sandbox)

    denied = PatchScopeGuard().validate(tmp_path, proposal, {
        "scopeType": "FULL_FILE", "targetFiles": ["src/another.py"],
    })
    assert not denied["passed"]


def test_java_scope_guard_accepts_signature_and_does_not_treat_if_as_method(tmp_path: Path):
    source = tmp_path / "src/main/java/com/example/order/OrderQueryService.java"
    source.parent.mkdir(parents=True)
    source.write_text("""package com.example.order;
import java.util.List;
public class OrderQueryService {
    public List<String> pageOrders(List<String> ids, int page, int size) {
        int from = page * size;
        return ids.subList(from, from + size);
    }
}
""", encoding="utf-8")
    proposal = PatchProposal(summary="pagination bounds", patches=[FilePatch(
        path="src/main/java/com/example/order/OrderQueryService.java",
        old="""    public List<String> pageOrders(List<String> ids, int page, int size) {
        int from = page * size;
        return ids.subList(from, from + size);
    }""",
        new="""    public List<String> pageOrders(List<String> ids, int page, int size) {
        int from = page * size;
        if (from >= ids.size()) {
            return List.of();
        }
        return ids.subList(from, Math.min(from + size, ids.size()));
    }""",
    )])
    guard = PatchScopeGuard().validate(tmp_path, proposal, {
        "scopeType": "STRICT_SINGLE_METHOD",
        "targetFiles": ["src/main/java/com/example/order/OrderQueryService.java"],
        "targetMethods": ["OrderQueryService.pageOrders(List<String>, int, int)"],
    })
    assert guard["passed"]
    assert guard["changedMethods"] == ["OrderQueryService.pageOrders"]


def test_java_scope_guard_rejects_unrelated_method_in_complete_file_rewrite(tmp_path: Path):
    source = tmp_path / "src/main/java/com/example/order/OrderService.java"
    source.parent.mkdir(parents=True)
    old = """package com.example.order;
public class OrderService {
    public int submit() { return 1; }
    public int calculateTotal() { return 2; }
}
"""
    new = """package com.example.order;
public class OrderService {
    public int submit() { return 3; }
    public int calculateTotal() { return 4; }
}
"""
    source.write_text(old, encoding="utf-8")
    proposal = PatchProposal(summary="mixed rewrite", patches=[FilePatch(
        path="src/main/java/com/example/order/OrderService.java", old=old, new=new,
    )])
    guard = PatchScopeGuard().validate(tmp_path, proposal, {
        "scopeType": "STRICT_SINGLE_METHOD",
        "targetFiles": ["src/main/java/com/example/order/OrderService.java"],
        "targetMethods": ["OrderService.submit()"],
    })
    assert not guard["passed"]
    assert "OrderService.calculateTotal" in guard["changedMethods"]
    assert any("calculateTotal" in item for item in guard["violations"])


def test_java_scope_guard_does_not_treat_record_declaration_as_constructor_method():
    old = """public record OrderSubmitRequest(String userId) {
    public void validate() {
    }
}
"""
    new = """public record OrderSubmitRequest(String userId) {
    public void validate() {
        if (userId == null) throw new IllegalArgumentException();
    }
}
"""
    assert PatchScopeGuard._changed_methods("OrderSubmitRequest.java", old, new) == ["OrderSubmitRequest.validate"]


def test_task_dag_and_source_write_hook_contract():
    dag = CodeOpsTaskDagService()
    context = dag.mark({}, 1, "repo_understanding", "SUCCESS", "localized")
    context = dag.mark(context, 2, "bug_fix", "FAILED", "compile failed", {"patch": "x"})
    assert context["taskDagNodes"][1]["blockedBy"] == ["step-1-repo_understanding"]
    assert context["taskDagNodes"][1]["stage"] == "code_repair"
    hooks = CodeOpsHookService()
    context, denied = hooks.emit(context, "BEFORE_TOOL_USE", "PATCH", "write", {
        "toolName": "repo.exact_replace", "arguments": {"filePath": "../secret"},
    })
    assert not denied["allowed"]
    _, allowed = hooks.emit(context, "BEFORE_TOOL_USE", "PATCH", "write", {
        "toolName": "repo.exact_replace", "arguments": {"filePath": "src/app.py"},
    })
    assert allowed["allowed"]


@pytest.mark.asyncio
async def test_agent_loop_executes_model_selected_tools_and_stops(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
    service = AgentLoopService(EngineeringToolGateway())

    async def model(request, steps):
        if not steps:
            return {"thoughtSummary": "search", "toolCalls": [{"toolCallId": "1", "toolName": "repo.search_text",
                    "arguments": {"repository": request["repository"], "queries": ["run"]}}]}
        return {"thoughtSummary": "done", "final": True, "finalAnswer": "found implementation"}

    task = {"taskId": "loop-1", "context": {}}
    result = await service.run({"repository": str(tmp_path), "goal": "find run", "task": task, "maxTurns": 4}, model)
    assert result["status"] == "COMPLETED" and result["turns"] == 2
    assert result["steps"][0]["toolResult"]["output"][0]["file"] == "src/app.py"
    assert len(task["context"]["agentLoopTrace"]) == 2


@pytest.mark.asyncio
async def test_agent_loop_normalizes_absolute_read_path_inside_repository(tmp_path: Path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def run(): return 1\n", encoding="utf-8")
    service = AgentLoopService(EngineeringToolGateway())

    async def model(request, steps):
        if not steps:
            return {"toolCalls": [{"toolName": "repo.read_file_snippet", "arguments": {
                "repository": str(tmp_path), "filePath": str(source), "centerLine": 1, "radius": 5}}]}
        return {"final": True, "finalAnswer": "read source"}

    result = await service.run({"repository": str(tmp_path), "goal": "read source",
                                "task": {"taskId": "loop-absolute-path", "context": {}}, "maxTurns": 3}, model)
    assert result["status"] == "COMPLETED"
    assert result["steps"][0]["toolResult"]["status"] == "SUCCESS"
    assert result["steps"][0]["arguments"]["filePath"] == "src/app.py"


@pytest.mark.asyncio
async def test_agent_loop_retries_malformed_read_only_call_in_next_turn(tmp_path: Path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def run(): return 1\n", encoding="utf-8")
    service = AgentLoopService(EngineeringToolGateway())

    async def model(request, steps):
        if not steps:
            return {"toolCalls": [{"toolName": "repo.read_file_snippet", "arguments": {}}]}
        if steps[-1]["toolResult"]["status"] == "DENIED":
            return {"toolCalls": [{"toolName": "repo.read_file_snippet", "arguments": {
                "filePath": "src/app.py", "centerLine": 1, "radius": 3}}]}
        return {"final": True, "finalAnswer": '{"shouldEnterCodeRepair":true,"targetFiles":["src/app.py"]}'}

    result = await service.run({"repository": str(tmp_path), "goal": "inspect run",
                                "task": {"taskId": "loop-recover-read", "context": {}}, "maxTurns": 4}, model)
    assert result["status"] == "COMPLETED"
    assert result["steps"][0]["toolResult"]["status"] == "DENIED"
    assert result["steps"][1]["toolResult"]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_background_maven_tasks_emit_and_consume_java_contract_notifications(tmp_path: Path, monkeypatch):
    runtime, gateway = ToolRuntime(), EngineeringToolGateway()
    task = {"taskId": "background-contract", "context": {"taskDagNodes": [
        {"skillId": "test_verification", "nodeId": "step-4-test_verification"}
    ]}}

    async def successful_run(*_args, **_kwargs):
        return MavenResult(["mvn", "-q", "test"], "PASSED", 0, "BUILD SUCCESS", 7)

    monkeypatch.setattr(TestRunner, "run", successful_run)
    token = runtime.bind(task, agent_or_skill="agent_loop")
    gateway.runtime = runtime
    try:
        started = await gateway.invoke("repo.maven_background", {
            "repository": str(tmp_path), "args": ["-q", "test"], "nodeId": "test_verification",
        })
        assert started["backgroundTaskId"].startswith("bgt-") and started["status"] == "RUNNING"
        await asyncio.sleep(0.01)
        status = await gateway.invoke("task.background_status", {"backgroundTaskId": started["backgroundTaskId"]})
        assert status["status"] == "SUCCESS" and status["nodeId"] == "step-4-test_verification"
        consumed = gateway.consume_terminal_notifications(task, "agent-loop-service")
        assert consumed[0]["type"] == "BACKGROUND_TASK_FINISHED"
        assert consumed[0]["consumed"] is True and not gateway.consume_terminal_notifications(task)
        assert task["context"]["consumedTaskNotifications"][0]["backgroundTaskId"] == started["backgroundTaskId"]
    finally:
        runtime.clear(token)


@pytest.mark.asyncio
async def test_agent_loop_consumes_terminal_background_notification_before_model_turn():
    task = {"taskId": "loop-notification", "context": {"taskNotifications": [{
        "notificationId": "ntf-1", "backgroundTaskId": "bgt-1", "type": "BACKGROUND_TASK_FAILED",
        "status": "FAILED", "summary": "exitCode=1", "payload": {"exitCode": 1}, "consumed": False,
    }]}}
    service = AgentLoopService(EngineeringToolGateway())

    async def model(request, _steps):
        observation = request["metadata"]["latestBackgroundTaskObservation"]
        assert observation["backgroundTaskId"] == "bgt-1"
        return {"thoughtSummary": "respond to background failure", "final": True, "finalAnswer": "retry"}

    result = await service.run({"task": task, "maxTurns": 1}, model)
    assert result["status"] == "COMPLETED"
    assert task["context"]["taskNotifications"][0]["consumed"] is True
    assert task["context"]["agentLoopTrace"][0]["consumedBackgroundNotifications"][0]["type"] == "BACKGROUND_TASK_FAILED"


@pytest.mark.asyncio
async def test_scheduler_deduplicates_aggregates_prioritizes_and_persists(tmp_path: Path):
    dispatched = []

    async def handler(incident):
        dispatched.append(incident)

    scheduler = IncidentScheduler(handler, max_concurrent=1, max_per_service=1,
                                  queue_file=tmp_path / "queue.json", dedup_window_seconds=300)
    first = await scheduler.ingest("fp-1", "Http5xx", "orders", "HIGH", "first", "/orders")
    assert first and await scheduler.ingest("fp-1", "Http5xx", "orders", "HIGH", "duplicate", "/orders") is None
    assert await scheduler.ingest("fp-2", "Http5xx", "orders", "CRITICAL", "aggregate", "/checkout") is None
    status = scheduler.status()
    assert status["activeIncidents"] == 1 and status["queueStats"]["totalEnqueued"] == 1
    assert status["activeIncidentItems"][0]["alertCount"] == 2
    assert (tmp_path / "queue.json").exists()
    await scheduler.start()
    await scheduler.queue.join()
    await scheduler.stop()
    assert len(dispatched) == 1 and scheduler.status()["queueStats"]["totalDispatched"] == 1


class PatchLlm:
    available = True

    async def complete(self, prompt, **_kwargs):
        if "agent loop planner" in prompt:
            return '{"thoughtSummary":"localized","toolCalls":[],"finalAnswer":{"summary":"found app",' \
                   '"fixStrategy":"CODE_FIX","scopeDecision":"FULL_FILE","targetFiles":["app.py"],' \
                   '"targetMethods":["run"],"shouldEnterCodeRepair":true,"localizationConfidence":"HIGH"}}'
        return '{"summary":"fix","rationale":"test","patches":[{"path":"app.py","old":"return 1","new":"return 2"}],"tests":[]}'


def test_human_approval_requires_generated_patch_and_real_passing_tests():
    state = {
        "task": {"taskId": "task-1", "taskType": "INCIDENT_TO_FIX", "goal": "repair incident"},
        "steps": [
            {"selectedSkill": "bug_fix", "status": "SUCCESS", "rawEvidenceJson":
             '{"llmGenerated":true,"rootCause":"race","patchDraft":"diff",'
             '"changedFiles":["app.py"],"patchQuality":{"minimalChangeScore":90},'
             '"patchSandbox":{"isolated":true}}'},
            {"selectedSkill": "test_verification", "status": "SUCCESS", "rawEvidenceJson":
             '{"testExecutionResults":"Tests run: 1, Failures: 0, Errors: 0"}'},
            {"selectedSkill": "release_risk_analysis", "status": "SUCCESS", "rawEvidenceJson":
             '{"releaseRiskReport":{"riskLevel":"HIGH"}}'},
        ],
    }
    approval = CodeOpsGraph._approval_payload(state)
    assert approval and approval["status"] == "PENDING"
    assert set(approval) == {"taskId", "caseName", "status", "rootCause", "patchSummary", "changedFiles",
                             "riskLevel", "testResults", "approvalReasons", "evidenceSummary", "patchQuality",
                             "patchSandbox", "submittedAt", "approvedAt", "rejectionReason"}
    state["steps"][2]["rawEvidenceJson"] = '{"releaseRiskReport":{"riskLevel":"LOW"}}'
    assert CodeOpsGraph._approval_payload(state) is None
    state["steps"][2]["rawEvidenceJson"] = '{"releaseRiskReport":{"riskLevel":"HIGH"}}'
    state["steps"][1]["rawEvidenceJson"] = '{"testExecutionResults":"BUILD FAILURE"}'
    assert CodeOpsGraph._approval_payload(state) is None
