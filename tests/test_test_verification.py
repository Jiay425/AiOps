from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ops_autoagent.codeops.test_verification import TestPatchApplier, TestVerificationService
from ops_autoagent.codeops.runtime import PatchSandbox
from ops_autoagent.config import Settings
from ops_autoagent.graphs.codeops import CodeOpsGraph
from ops_autoagent.llm import OpenAICompatibleClient


def _state(repository: Path, *, patch: dict | None = None, context: dict | None = None) -> dict:
    return {"task": {"taskId": "test-task", "taskType": "INCIDENT_TO_FIX", "goal": "order inventory oversell concurrency",
                      "repository": str(repository), "steps": []},
            "context": {"diffContext": {"repositoryPath": str(repository), "changeRef": "working_tree",
                                        "changedFiles": ["src/main/java/example/OrderService.java"], "relatedTestFiles": [],
                                        "diffSummary": "changedFiles=1", "diffAvailable": False}, **(context or {})},
            "working_memory": {"codeLocalization": {"targetFiles": ["src/main/java/example/OrderService.java"]},
                               "patchGeneration": patch or {}}}


def test_test_patch_applier_supports_new_test_file_without_git_repository(tmp_path: Path):
    target = tmp_path / "src/test/java/example"
    target.mkdir(parents=True)
    patch = """--- /dev/null
+++ b/src/test/java/example/OrderServiceTest.java
@@ -0,0 +1,3 @@
+package example;
+class OrderServiceTest {}
+
"""
    result = TestPatchApplier().apply(str(tmp_path), patch)
    assert result["applied"] is True
    assert (target / "OrderServiceTest.java").read_text(encoding="utf-8").startswith("package example;")


@pytest.mark.asyncio
async def test_disabled_execution_records_java_compatible_skipped_background_tasks(tmp_path: Path):
    service = TestVerificationService(OpenAICompatibleClient(Settings(codeops_test_execution_enabled=False)),
                                     Settings(codeops_test_execution_enabled=False))
    result = await service.execute(_state(tmp_path))
    raw = result["raw"]
    assert result["status"] == "NO_DIFF"
    assert raw["testExecutionResults"] == ["真实测试执行未开启：设置 codeops.test.execution.enabled=true 后会运行推荐 Maven 命令。"]
    assert {item["status"] for item in raw["backgroundToolTasks"]} == {"SKIPPED"}
    assert {item["type"] for item in raw["taskNotifications"]} == {"BACKGROUND_TASK_SKIPPED"}


@pytest.mark.asyncio
async def test_test_patch_failure_rolls_back_new_test_file(tmp_path: Path, monkeypatch):
    settings = Settings(codeops_test_execution_enabled=True)
    service = TestVerificationService(OpenAICompatibleClient(settings), settings)

    async def failed_maven(repository: str, args: list[str]) -> dict:
        return {"command": ["mvn", *args], "success": False, "exitCode": 1, "costMillis": 1,
                "output": "[ERROR] COMPILATION FAILURE\nOrderServiceTest.java:3 cannot find symbol"}

    monkeypatch.setattr(service, "_maven", failed_maven)
    patch = {"testUnifiedDiffPatch": """--- /dev/null
+++ b/src/test/java/example/OrderServiceTest.java
@@ -0,0 +1,3 @@
+package example;
+class OrderServiceTest {}
+
"""}
    result = await service.execute(_state(tmp_path, patch=patch, context={"allowTestPatchApply": True}))
    raw = result["raw"]
    assert result["status"] == "FAILED"
    assert raw["testPatchApply"]["applied"] is True
    assert raw["testPatchRolledBack"] is True
    assert not (tmp_path / "src/test/java/example/OrderServiceTest.java").exists()
    assert raw["testFailureType"] == "TEST_COMPILE_FAILED"


@pytest.mark.asyncio
async def test_async_maven_emits_running_then_terminal_task_notification(tmp_path: Path, monkeypatch):
    settings = Settings(codeops_test_execution_enabled=True)
    service = TestVerificationService(OpenAICompatibleClient(settings), settings)

    async def passing_maven(repository: str, args: list[str]) -> dict:
        await asyncio.sleep(0)
        return {"command": ["mvn", *args], "success": True, "exitCode": 0, "costMillis": 1,
                "output": "BUILD SUCCESS"}

    monkeypatch.setattr(service, "_maven", passing_maven)
    result = await service.execute(_state(tmp_path, context={"asyncTestExecution": True}))
    assert result["status"] == "WAITING_BACKGROUND_TASK"
    await asyncio.sleep(0.01)
    assert {item["status"] for item in result["raw"]["backgroundToolTasks"]} == {"SUCCESS"}
    assert {item["type"] for item in result["raw"]["taskNotifications"]} >= {
        "BACKGROUND_TASK_STARTED", "BACKGROUND_TASK_FINISHED"}


@pytest.mark.asyncio
async def test_bug_fix_compile_failure_rolls_back_isolated_sandbox(tmp_path: Path, monkeypatch):
    source = tmp_path / "src/main/java/example/OrderService.java"
    source.parent.mkdir(parents=True)
    source.write_text("class OrderService { int submit() { return 1; } }\n", encoding="utf-8")
    settings = Settings(codeops_test_execution_enabled=True)
    graph = CodeOpsGraph(OpenAICompatibleClient(settings))
    state = {"task": {"taskId": "bugfix-task", "taskType": "INCIDENT_TO_FIX", "goal": "fix order failure",
                      "repository": str(tmp_path), "changeRef": "", "maxRounds": 12, "maxToolCalls": 20},
             "context": {"allowPatchApply": True, "repairObservations": []}, "working_memory": {
                 "fixStrategy": {"shouldEnterCodeRepair": True, "scopeDecisionType": "FULL_FILE"},
                 "codeLocalization": {"targetFiles": ["src/main/java/example/OrderService.java"], "targetMethods": ["submit"]}},
             "steps": [], "tool_calls": 0, "round": 0, "executed_skills": [], "decision": {"decision": "CALL_SKILL"}}

    async def proposed(_: dict) -> dict:
        return {"patch_proposal": {"summary": "replace result", "rationale": "minimal change", "tests": [],
                                    "patches": [{"path": "src/main/java/example/OrderService.java",
                                                 "old": "return 1;", "new": "return 2;"}]},
                "context": state["context"], "tool_calls": 1, "round": 1}

    async def failed_compile(repository: str, source_valid: bool) -> dict:
        return {"requested": True, "success": False, "command": ["mvn", "-q", "-DskipTests", "compile"],
                "exitCode": 1, "costMillis": 1, "output": "COMPILATION FAILURE"}

    monkeypatch.setattr(graph, "_execute_skill", proposed)
    monkeypatch.setattr(graph, "_compile_gate", failed_compile)
    result = await graph._skill_bug_fix(state)
    raw = result["working_memory"]["patchGeneration"]
    try:
        assert result["steps"][-1]["status"] == "FAILED"
        assert raw["patchApply"]["applied"] is True
        assert raw["compileGate"]["success"] is False
        assert raw["patchRolledBack"] is True
        sandbox_file = Path(raw["sandboxRepositoryPath"]) / "src/main/java/example/OrderService.java"
        assert "return 1;" in sandbox_file.read_text(encoding="utf-8")
        assert "return 1;" in source.read_text(encoding="utf-8")
    finally:
        if raw["sandboxRepositoryPath"]:
            PatchSandbox.cleanup(raw["sandboxRepositoryPath"])


@pytest.mark.asyncio
async def test_bugfix_agent_prompt_parses_complete_file_rewrite_contract(tmp_path: Path):
    source = tmp_path / "src/main/java/example/OrderService.java"
    source.parent.mkdir(parents=True)
    source.write_text("class OrderService { int submit() { return 1; } }\n", encoding="utf-8")
    settings = Settings(codeops_agent_bugfix_llm_enabled=True)

    class BugfixLlm:
        available = True

        def __init__(self):
            self.settings = settings
            self.prompt = ""

        async def complete(self, prompt, **_kwargs):
            self.prompt = prompt
            return """{
              "rootCause":"wrong result","confidence":"HIGH",
              "targetFiles":["src/main/java/example/OrderService.java"],
              "reasoning":["visible method returns one"],
              "scopeDecision":{"decision":"KEEP_SCOPE","finalScopeType":"FULL_FILE"},
              "fileRewrites":[{"filePath":"src/main/java/example/OrderService.java",
                "newContent":"class OrderService { int submit() { return 2; } }\\n","reasoning":"minimal"}],
              "testSuggestions":["OrderServiceTest"],"mavenCommands":["mvn -q -DskipTests compile"],
              "testUnifiedDiffPatch":"","testFileRewrites":[],"riskNotes":[]
            }"""

    llm = BugfixLlm()
    graph = CodeOpsGraph(llm)
    state = {"task": {"taskId": "special-bugfix", "taskType": "INCIDENT_TO_FIX", "goal": "fix result",
                      "repository": str(tmp_path)}, "context": {"codeSearchMatches": []},
             "working_memory": {"fixStrategy": {"shouldEnterCodeRepair": True, "strategyType": "CODE_FIX"},
                                "codeLocalization": {"targetFiles": ["src/main/java/example/OrderService.java"]}},
             "round": 0, "tool_calls": 0, "current_skill": "bug_fix", "steps": []}
    result = await graph._execute_skill(state)

    assert "fileRewrites" in llm.prompt and "candidate scope" in llm.prompt
    assert result["bugfixAgent"]["rootCause"] == "wrong result"
    assert result["patch_proposal"]["patches"][0]["old"] == "class OrderService { int submit() { return 1; } }\n"
    assert "return 2" in result["patch_proposal"]["patches"][0]["new"]


def test_bugfix_agent_unified_diff_fallback_becomes_isolated_production_patch(tmp_path: Path):
    source = tmp_path / "src/main/java/example/OrderService.java"
    source.parent.mkdir(parents=True)
    source.write_text("class OrderService {\n  int submit() { return 1; }\n}\n", encoding="utf-8")
    patch = """--- a/src/main/java/example/OrderService.java
+++ b/src/main/java/example/OrderService.java
@@ -1,3 +1,3 @@
 class OrderService {
-  int submit() { return 1; }
+  int submit() { return 2; }
 }
"""
    patches = CodeOpsGraph._production_patches_from_unified_diff(str(tmp_path), patch)

    assert patches == [{"path": "src/main/java/example/OrderService.java",
                        "old": "class OrderService {\n  int submit() { return 1; }\n}\n",
                        "new": "class OrderService {\n  int submit() { return 2; }\n}\n"}]
    assert "return 1" in source.read_text(encoding="utf-8")
