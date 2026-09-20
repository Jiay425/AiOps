# Case Study：订单重复提交导致重复写入

> 这是一个真实执行过的端到端修复 Case。观测输入使用仓库内脱敏的
> TEST_SIMULATED_DATA Fixture；LLM 调用、仓库定位、补丁生成、Maven 验证和受控工作区
> 应用都是真实执行。它不代表真实生产事故，也不把 Fixture 伪装成生产遥测。

## 问题

order-service 在高并发重试时可能重复写入订单。代码采用了“先查询是否已处理，再标记为已处理”的两步操作；两个请求可以同时通过查询，再分别创建订单。

~~~text
request A ── alreadyProcessed(id) = false ─┐
                                            ├─ both create an order
request B ── alreadyProcessed(id) = false ─┘
~~~

## 这次执行的结果

| 项目 | 结果 |
| --- | --- |
| Case | incident-order-idempotency-race |
| 模型 | DeepSeek Flash，OpenAI-compatible API |
| 工具调用 | 17 次 |
| 总耗时 | 约 73 秒 |
| 修复范围 | IdempotencyService.java、OrderSubmitService.java |
| 验证 | Scope Guard、静态安全、Maven 编译、IdempotencyServiceAtomicityTest 均通过 |
| 审查结论 | ACCEPT_WITH_HUMAN_REVIEW |
| 最终策略 | 高置信且全部门禁通过，自动应用到受控工作区 |
| 审计 | 保存 Patch Digest、Baseline Digest、前后文件校验和与 Effect Log |

## 实际流程

~~~mermaid
sequenceDiagram
    participant A as Alert / Fixture
    participant G as CodeOpsGraph
    participant D as Diagnosis Agent
    participant R as Repair Agent
    participant V as Maven Verification
    participant W as Review + Policy
    participant T as Managed Worktree

    A->>G: incident-order-idempotency-race
    G->>D: Metrics / Logs / Trace / Runbook / anomaly signals
    D-->>G: idempotency race hypothesis + localization constraints
    G->>R: read-only repo investigation
    R-->>G: minimal PatchSandbox proposal
    G->>V: compile + IdempotencyServiceAtomicityTest
    V-->>G: passed
    G->>W: facts, scope, risk, dry-run
    W-->>G: review decision + auto-apply eligibility
    G->>T: apply_approved_patch
    T-->>G: effect log + checksums
~~~

### 1. 证据和诊断

OpsEvidenceSubgraph 固定读取 Metrics、Logs、Traces 和 Runbook。数值和文本证据先经过
3-Sigma、EWMA、规则及 Isolation Forest，形成带来源和严重度的结构化异常信号；随后
Diagnosis Agent 才给出“幂等检查与写入之间存在竞态”的代码定位约束。

如果 Neo4j 已配置，GraphRcaSubgraph 会同时查询
order-service → payment-service → mysql-primary 的依赖路径、近期变更和 Trace 关联。
它输出的是带证据来源的根因候选，不会在没有图数据时编造结果。

### 2. 代码定位与补丁

Repair Agent 只能使用只读仓库工具调查代码和测试。补丁在 PatchSandbox 内生成：

~~~java
// IdempotencyService：一次同步操作完成检查和占位
public synchronized boolean markProcessedIfAbsent(String requestId) {
    if (processed.contains(requestId)) {
        return true;
    }
    processed.add(requestId);
    return false;
}

// OrderSubmitService：不再把 check 和 mark 分成两步
if (idempotencyService.markProcessedIfAbsent(requestId)) {
    throw new IllegalStateException("Duplicate requestId " + requestId);
}
~~~

Scope Guard 限制可改文件和方法；Patch Digest 将审核的补丁和实际应用的补丁绑定。

### 3. 验证、审查与自动应用

图先运行编译和定向 JUnit：

~~~powershell
mvn -q -Dtest=IdempotencyServiceAtomicityTest test
~~~

独立 Review 子图只能读取补丁事实、Scope Guard、编译/测试结果和风险上下文。它不能改补丁，
也不能调用写工具。最终策略层不会只相信模型文字：只有策略启用、任务类型为
INCIDENT_TO_FIX、高置信、低/中风险、低爆炸半径，以及 Scope Guard、PatchSandbox、
编译和测试全部通过，才会走：

~~~text
auto_approve_patch → apply_approved_patch → APPLIED
~~~

低置信或高风险 Case 则会在 interrupt() 暂停，使用同一 thread_id 经
Command(resume=...) 继续。默认 delivery_only 只交付补丁制品；本次验证在显式允许的
容器样例工作区启用了 apply_to_worktree，不等于生产仓库授权。

## 可复核证据

- **LangGraph Checkpoint**：当前节点、修复轮次、自动/人工审批身份、Patch Digest。
- **Task Event 与 SSE**：节点、子图、工具、验证和 Effect 事件可按任务回放。
- **Effect Log**：两处目标文件的应用前后 SHA-256 校验和、操作者和应用状态。
- **MySQL / Kafka**：任务投影、Artifact、Metric、Outbox、消费幂等键和 DLQ。
- **Neo4j**：服务依赖、近期变更、RCA 候选的证据路径（配置后才参与结论）。

## 如何复现

先按 [Demo 指南](demo.md) 启动全栈，再把凭证仅注入当前终端：

~~~powershell
$env:OPENAI_BASE_URL = "https://api.deepseek.com"
$env:OPENAI_MODEL = "deepseek-flash"
$env:OPENAI_API_KEY = "<your-api-key>"

Invoke-RestMethod -Method Post `
  http://127.0.0.1:8099/api/v1/codeops/evaluation/run/incident-order-idempotency-race
~~~

随后通过任务 ID 查看事件、审计和可观测性投影。真实服务未配置时，系统会明确记录对应
阶段不可用；不会伪造模型、Maven、Neo4j 或线上观测成功。
