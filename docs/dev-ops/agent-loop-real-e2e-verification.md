# Agent Loop Real E2E Verification

更新时间：2026-06-10

## 目标

验证真实 DeepSeek 模型驱动的 CodeOps Agent Loop 是否能在完整 `ISSUE_TO_PATCH` 链路中工作，并确认 agent loop 的结构化输出能传递给后续修复、测试验证和发布风险阶段。

本次验收不把任何 API key 写入仓库。真实模型 key 只通过本地进程环境变量注入。

## 运行参数

接口：

```http
POST /api/v1/codeops/task/submit
```

请求要点：

```json
{
  "taskType": "ISSUE_TO_PATCH",
  "goal": "Investigate OrderSubmitService null validation and repository tests. Use the agent loop to identify target files and test recommendations, then run the CodeOps repair, verification, and release-risk flow.",
  "repository": "E:/DeskTop/java_project/ops-autoagent-diagnosis/samples/order-service",
  "maxRounds": 6,
  "maxToolCalls": 24,
  "context": {
    "agentLoopMaxTurns": 4
  }
}
```

任务 ID：

```text
6898fa0a-b429-44fe-aea2-14a6b23a1c6d
```

## 链路结果

任务整体状态为 `FAILED`，但链路阶段按预期完整执行到了发布风险分析。

```text
1. agent_loop_investigation  SUCCESS
2. engineering_knowledge_rag SUCCESS
3. bug_fix                   FAILED
4. test_verification         FAILED
5. release_risk_analysis     NO_DIFF
6. STOPPED
```

使用工具调用数：`14`。

## Agent Loop 输出

真实模型在 `agent_loop_investigation` 阶段成功调用了只读仓库工具，包括：

- `repo.search_text`
- `repo.read_file_snippet`

结构化 finalAnswer 成功解析进 working memory：

```json
{
  "targetFiles": [
    "src/main/java/com/example/order/OrderSubmitService.java",
    "src/main/java/com/example/order/OrderRepository.java"
  ],
  "recommendedTests": [
    "Add unit tests for OrderSubmitService.submit() with null request, null userId, null skuId",
    "Add unit tests for OrderRepository.create() with null or blank parameters",
    "Extend OrderControllerTest to cover null request and invalid fields"
  ],
  "shouldEnterCodeRepair": true,
  "localizationConfidence": "MEDIUM",
  "missingEvidence": [
    "Exact source of OrderSubmitServiceTest.java to see expected exceptions and current failures"
  ]
}
```

这证明结构化 agent loop 输出已经能进入 `codeLocalization`，并驱动后续阶段选择。

## 后续阶段表现

### Bug Fix

`bug_fix` 读取到了 agent loop 的候选文件和修复意图，并生成了修复方向：

- 根因：`OrderSubmitService.submit` 缺少 `request/userId/skuId` 空值校验。
- 置信度：`HIGH`。
- 结果：被 `PatchScopeGuard` 拦截。

拦截原因：

```text
UNIFIED_DIFF_ONLY: Cannot reliably detect changedMethods from unifiedDiffPatch.
For STRICT_SINGLE_METHOD / MULTI_METHOD scope, use fileRewrites instead.
```

这是安全闸的预期保护行为：模型生成了 diff，但当前 ScopeGuard 不信任只靠 unified diff 判断方法边界。

### Test Verification

`test_verification` 读取到了修复上下文，并生成了测试计划：

- 建议测试：`src/test/java/com/example/order/OrderSubmitServiceNullValidationTest.java`
- 编译命令：`mvn -q -DskipTests compile`
- 目标测试命令：`mvn -q -Dtest=OrderSubmitServiceNullValidationTest test`
- 全量测试命令：`mvn -q test`

结果：

- 编译通过。
- 目标测试失败，因为测试补丁未应用，测试类不存在。

失败信息：

```text
No tests matching pattern "OrderSubmitServiceNullValidationTest" were executed.
```

这是符合当前安全配置的结果：`allowTestPatchApply=false`，系统不会自动创建测试文件。

### Release Risk

`release_risk_analysis` 成功消费了前面阶段结果，输出高风险结论：

- patch 被 ScopeGuard 拦截，未应用。
- test patch 未授权，未应用。
- 目标测试不存在，验证失败。
- 当前修复不能视为已验证或可发布。
- 需要人工接管，手动应用 patch、创建测试、运行验证。

## 结论

本次真实 E2E 验收通过了“链路可解释性”和“安全治理”两个关键目标：

1. 真实 DeepSeek agent loop 可以调用仓库工具并输出结构化 finalAnswer。
2. 结构化字段能进入 working memory。
3. 后续 `bug_fix`、`test_verification`、`release_risk_analysis` 能消费 agent loop 的定位和测试建议。
4. ScopeGuard 和测试补丁授权 gate 能阻止未充分验证的自动修改。
5. Release Risk 能把失败原因、人工确认点和上线风险讲清楚。

当前任务最终为 `FAILED` 是合理结果，不是链路不可用。它表示自动修复被安全策略拦截，系统进入人工接管态。

## 后续优化

1. 让 Bug Fix Agent 优先输出 `fileRewrites`，减少 unified diff 被 ScopeGuard 拦截。
2. 在 `allowTestPatchApply=false` 时，Test Verification 不应执行尚未创建的目标测试类，可改为输出待执行计划或过滤未应用的新测试类。
3. 给 Release Risk 增加明确字段：`manualTakeoverRequired=true`、`autoPatchBlockedReason`、`verificationBlockedReason`。
4. 增加一个真实 E2E eval case，断言链路至少包含 agent loop、bug fix、test verification、release risk 四段，并检查 release risk 是否解释安全拦截原因。

## 2026-06-10 后续加固

围绕本次真实 E2E 暴露的问题，已追加一轮成体系优化：

1. `CodeOpsBugFixPrompts` 强化 patch 格式优先级：
   - 生产 Java 修复默认优先输出 `fileRewrites`。
   - `fileRewrites` 存在时保持 `unifiedDiffPatch` 为空。
   - 只有无法安全重建完整文件内容时才使用 `unifiedDiffPatch`。
   - 明确提示：仅输出 unified diff 可能被 ScopeGuard 判定为 `UNIFIED_DIFF_ONLY`。

2. `TestVerificationSkill` 增强未应用测试补丁时的验证策略：
   - 当 `allowTestPatchApply=false` 且测试补丁未应用时，自动过滤仅针对新测试类的 Maven 命令。
   - 如果命令混合了“未应用的新测试类”和“已有测试类”，会剔除未应用的新测试 selector，只保留已有测试执行。
   - 输出 `skippedMavenCommands` 和 `verificationBlockedReason`，避免把“测试类不存在”误判为代码失败。

3. `ReleaseRiskSkill` 增加结构化人工接管字段：
   - `manualTakeoverRequired`
   - `autoPatchBlockedReason`
   - `verificationBlockedReason`
   - `blockedAutomationSummary`

4. `EngineeringTaskTraceService` 已把这些字段提升到 Trace highlights、working memory summary 和 stage artifacts。

5. `CodeOpsAgentLoopModelClient` 增加单次重试，用于缓解真实模型端偶发 EOF / 网络瞬断。

最新验证任务：

```text
taskId=e3d83696-f01c-439e-9b09-0d9a5b19b748
```

关键结果：

```text
agent_loop_investigation: SUCCESS
engineering_knowledge_rag: SUCCESS
bug_fix: FAILED
test_verification: SUCCESS
release_risk_analysis: NO_DIFF
```

`releaseRisk.blockedAutomationSummary` 已能在 working memory summary 中展示：

```json
{
  "manualTakeoverRequired": true,
  "autoPatchBlockedReason": "Production patch was not applied: Blocked by PatchScopeGuard"
}
```
