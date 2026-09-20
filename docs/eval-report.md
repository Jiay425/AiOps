# Eval 说明与复现

本仓库把**业务效果**和**运行时安全/可靠性**分开统计，避免把“接口能跑”误写成“模型修复成功”。

## 当前评测资产

| 套件 | 数量 | 作用 |
| --- | ---: | --- |
| 业务 E2E | 52 | 16 条历史基线 + 36 条扩展，覆盖证据、定位、补丁、验证与发布风险。 |
| 运行时安全/可靠性 | 16 | Checkpoint/SSE、Scope Guard、Outbox、Kafka DLQ、Neo4j RCA、Isolation Forest、Runbook dry-run。 |

业务 Case 都带有 `fixtureReference`、`fixtureReuseFrom` 和
`repositoryFixtureReference`。Fixture 是脱敏的测试数据，不代表真实线上遥测。

## 运行

```powershell
.venv311\Scripts\python.exe -m pytest -q

# 查看 Case Catalog 与统计
Invoke-RestMethod http://127.0.0.1:8099/api/v1/codeops/evaluation/cases
Invoke-RestMethod http://127.0.0.1:8099/api/v1/codeops/evaluation/runtime/cases
Invoke-RestMethod http://127.0.0.1:8099/api/v1/codeops/evaluation/summary
```

启用真实 LLM、观测源和 Maven 后再调用 `POST /api/v1/codeops/evaluation/run`。
报告会保存实际的阶段结果、失败原因和可重放 Trace；未配置服务时会记录
`REVIEW_UNAVAILABLE`、`NOT_CONFIGURED` 等真实边界，绝不生成虚假的通过率。

## 判分边界

- Runbook Top-3、根因命中、补丁应用、编译测试通过率只从真实执行报告计算；不能由 Case Catalog 推导。
- `delivery_only` 下的“补丁应用率”只能表示 Patch Sandbox 中的应用结果，不能表示目标仓库已修改。
- Outbox/Kafka/Neo4j/Runbook 的安全 Case 是运行时工程验证，不计入业务修复成功率。
- Bad Case 应新增为固定回归 Case，并记录失败阶段、Fixture、预期停止状态和修复版本。
