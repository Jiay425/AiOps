# LangGraph AIOps 升级路线图

> 仓库：ops-autoagent-diagnosis-python  
> 目标：在不破坏原 Spring AI 兼容路由、DTO、Store 映射和既有评估的前提下，把当前分工式 Agent 工作流升级为安全、可恢复、可审计、可扩展的 LangGraph Incident-to-Fix 系统。  
> 本文是实现规格。所有后续代码改动、测试和发布均以本文为准。

## 1. 当前定位

当前项目已经是多 Agent 协作系统，不需要否认这一点：

- Ops 侧有 Planner、Evidence Reviewer、Report Writer 三个 LLM 角色；
- CodeOps 侧有 Repository Investigation、Bug Fix、Release Risk 等分工角色；
- LangGraph 负责 State、条件路由、循环和 checkpoint；
- 工具节点负责观测查询、仓库读取、补丁沙箱、编译和测试。

本轮升级的重点不是增加 Agent 数量，而是补齐 LangGraph 的生产运行时能力：

1. 把“任务表审批”升级为真正的 interrupt/resume；
2. 把源码变更收束到唯一、审批后的 effect boundary；
3. 通过 reducer 安全支持并行采证；
4. 让独立 Reviewer 的结论真正驱动 Repair 重试或人工审批；
5. 为 CodeOps 提供增量事件流、断线补发与状态恢复；
6. 让 checkpoint、Store 投影和后台任务具有明确的一致性协议；
7. 让所有影响路由、工具或写入的 LLM 输出经过结构化合同校验。

## 2. 当前基线与必须修正的缺口

| 范围 | 当前实现 | 升级结论 |
|---|---|---|
| Ops 图 | Metrics -> Logs -> Trace 串行采集；Evidence Reviewer 可补证 | 三路主证据可并行，但必须先加 reducer 和预算协调 |
| CodeOps 图 | Plan -> Orchestrate -> 一个 skill -> Orchestrate 的受控循环 | 保留策略编排，不强行改成无约束自治群聊 |
| checkpoint | memory、SQLite、PostgreSQL 均可配置 | 现有测试仅覆盖完成态读取；需覆盖 interrupt 后跨进程恢复 |
| 审批 | mark_approval 写 WAITING_APPROVAL，API 直接改 Store | 这不是 graph interrupt；必须改成同 thread_id 的 Command(resume=...) |
| 修复 | PatchScopeGuard、PatchSandbox、compile/test 已存在 | 保留并收束为唯一写入边界 |
| 工具 | 通用 AgentLoop 注册 repo.exact_replace | 必须从只读调查 loop 移除；当前实现会改传入仓库 |
| State | events、tool_trace、steps 为普通列表，无 reducer | 当前串行可用；并行前必须改为增量写入和 reducer |
| 流式输出 | Ops 有 SSE；CodeOps submit 等待 ainvoke 完成 | 增加 CodeOps task event stream，不破坏原同步端点 |
| 评估 | Ops、CodeOps、RAG fixtures 与契约测试已经存在 | 保留，额外增加安全、恢复、并发和审批评估 |

## 3. 目标架构

~~~mermaid
flowchart LR
  A["Alertmanager / API"] --> B["父图：Incident-to-Fix Orchestrator"]
  B --> C["OpsEvidence 子图"]
  C --> C1["Metrics"]
  C --> C2["Logs"]
  C --> C3["Trace"]
  C1 --> D["Evidence Barrier + Reviewer"]
  C2 --> D
  C3 --> D
  D --> E["Repository Investigation 子图<br/>只读 ReAct loop"]
  E --> F["Repair Proposal 子图<br/>Scope Guard + Sandbox"]
  F --> G["Test Verification"]
  G --> H["Independent Release Reviewer"]
  H -->|"RETRY_REPAIR 且未超预算"| F
  H -->|"需要人工决定"| I["LangGraph interrupt"]
  I -->|"批准交付"| J["Deliver patch artifact"]
  I -->|"明确批准应用"| K["Apply approved patch"]
  I -->|"拒绝"| L["Reject + audit"]
  J --> M["Summary"]
  K --> M
  L --> M
~~~

父图是控制平面。子图只抽取有独立输入、输出、循环和测试边界的领域能力；普通工具、校验和转换函数不是 Agent，也不必做成 subgraph。

## 4. 全局不变量

以下规则优先级最高：

- taskId 是业务任务 ID，也是 LangGraph thread_id；不得在恢复时生成新 thread；
- checkpoint 是图执行游标；tasks、approval、audit_logs 是供 API 查询和审计的投影；
- 任何真实目标仓库写入必须有 explicit approval action、approvalId、decisionId、patchDigest 和 baselineDigest；
- 默认审批动作是 APPROVE_DELIVERY，只交付已验证补丁；默认不写目标仓库；
- 只有 action=APPROVE_APPLY_TO_WORKTREE 才允许进入真实 apply 节点；
- interrupt 所在 node 在 resume 时会从 node 开头重跑，因此 interrupt 前不得出现不可幂等副作用；
- 所有工具调用、审批决定、效果写入必须有不可变 ID、时间、任务、node、attempt 和脱敏审计；
- 不允许 LLM 通过 repo.exact_replace、shell、路径穿越、符号链接或工作目录逃逸绕过审批；
- 不允许在没有 reducer 的 State 上引入 fan-out；
- 旧 API、camelCase DTO、legacy Store 映射、fixtures 和验收测试必须持续通过；
- 不将 Agent 批准等同于自动生产部署。部署仍交给既有 CI/CD 或人工发布流程。

## 5. 状态、工件和结构化输出设计

### 5.1 身份字段

| 字段 | 含义 | 规则 |
|---|---|---|
| taskId | 任务和 thread 的主键 | 创建后不变 |
| runId | 单次 API/worker 执行尝试 | 每次驱动可以新建 |
| stateSchemaVersion | checkpoint State 版本 | 只向前迁移 |
| approvalId | 某次审批请求 | 每次进入审批唯一 |
| decisionId | 人工决定幂等键 | 同一 approval 只能消费一次 |
| patchDigest | 补丁摘要 | 批准到应用之间必须一致 |
| repositoryBaselineDigest | 起始仓库摘要 | 应用前必须再次验证 |
| repairAttempt | 修复尝试序号 | 最大值由配置限制 |

### 5.2 reducer State

新增 src/ops_autoagent/graphs/state_models.py，集中管理公共 State、reducer、事件和审批合同。继续使用 TypedDict 作为图内 State，但跨 node 累积字段必须声明 reducer。

~~~python
from operator import add
from typing import Annotated, Any, TypedDict

EventList = Annotated[list[dict[str, Any]], add]
ToolTraceList = Annotated[list[dict[str, Any]], add]

class ApprovalState(TypedDict, total=False):
    approval_id: str
    status: str
    request: dict[str, Any]
    decision: dict[str, Any]

class DurableTaskState(TypedDict, total=False):
    state_schema_version: int
    events: EventList
    tool_trace: ToolTraceList
    approval: ApprovalState
    effect_log: EventList
~~~

规则：

1. reducer 字段的 node 只能返回本次增量，例如 events: [event]；
2. 不得返回 state["events"] 加新事件，否则 reducer 会重复累计；
3. 每个 event 有 eventId、taskId、attempt、stage、kind、timestamp、summary；
4. 并行写入顺序不具备业务确定性。展示层使用 stageOrder、attempt、timestamp、eventId 排序；
5. steps 不能再只使用 len(steps)+1 作为唯一身份。并行前应使用 stepId，最终展示再编号；
6. State 存摘要、artifact ID、关键结果和恢复游标。完整日志/大 prompt/完整工具响应进入 Store 或工件文件；
7. 对 State 做 schema 版本迁移，不能假定已暂停的旧线程天然有新字段。

### 5.3 Pydantic 结构化合同

新增合同，放入 schemas.py 或独立 contracts.py：

- InvestigationDecision：toolCalls、targetFiles、targetMethods、missingEvidence、shouldEnterCodeRepair；
- EvidenceReviewContract：sufficient、requiredTools、rootCause、confidence、constraints；
- PatchProposalContract：rootCause、scopeDecision、fileRewrites、exactReplaceBlocks、tests、risk notes；
- ReleaseReviewContract：reviewVerdict、patchDecision、riskLevel、retryInstructions、mustReview；
- ApprovalDecisionContract：approved、action、reason、operatorId、decisionId。

每个 LLM 边界遵守同一流程：

1. 要求 JSON only；
2. 解析 JSON；
3. Pydantic model_validate；
4. 无法校验时产生 STRUCTURED_OUTPUT_INVALID 工件；
5. 可进行一次只修复格式的 retry；
6. 再失败时进入确定性 fallback 或人工处理；
7. 记录模型、tier、promptVersion、输入 artifact IDs、耗时、成本估算、解析错误。

不得假设 Luna 或任意 OpenAI-compatible 供应商都支持原生 JSON Schema response_format；第一版使用 JSON prompt 加 Pydantic 校验，确认供应商能力后再可选启用 response_format。

## 6. M0：真实 HITL、唯一 effect boundary、恢复协议

M0 是必须先完成的基础。M0 未验收，不得开始并行 fan-out 或子图重构。

### 6.1 CodeOps 图改造

修改 src/ops_autoagent/graphs/codeops.py：

1. 从 langgraph.types 导入 interrupt；
2. 将当前 mark_approval 拆为 prepare_approval、human_approval、apply_approved_patch、deliver_patch、rejected、summarize；
3. prepare_approval 只计算并持久化审批请求，不应用补丁；
4. human_approval 的第一项副作用之前调用 interrupt(payload)；
5. interrupt payload 必须是 JSON 可序列化对象，至少带 approvalId、taskId、patchDigest、baselineDigest、changedFiles、testResults、riskLevel、approvalReasons、allowedActions；
6. resume 值校验为 ApprovalDecisionContract；
7. human_approval 根据 action 返回 approval decision，并由条件边路由；
8. APPROVE_DELIVERY 进入 deliver_patch，只生成可审计交付物；
9. APPROVE_APPLY_TO_WORKTREE 才进入 apply_approved_patch；
10. REJECT 进入 rejected，保存原因但绝不写目标仓库；
11. apply 前重新校验 patchDigest、baselineDigest、Scope Guard、静态验证和真实测试结果；
12. 任一校验不一致必须标记 STALE_APPROVAL 或 REQUIRES_REVIEW，再次申请审批，不能静默覆盖；
13. summarize 依据真实路径输出 COMPLETED、HUMAN_REJECTED、WAITING_APPROVAL、FAILED。

建议流程：

~~~text
finish
  -> approval_required ? prepare_approval : summarize
prepare_approval -> human_approval
human_approval
  -> APPROVE_APPLY_TO_WORKTREE : apply_approved_patch
  -> APPROVE_DELIVERY : deliver_patch
  -> REJECT : rejected
apply_approved_patch -> summarize
deliver_patch -> summarize
rejected -> summarize
~~~

### 6.2 API 和业务投影

修改 src/ops_autoagent/api.py：

- 任务初次运行碰到 interrupt 时保存 task 摘要、approval 摘要和 thread_id；
- approval 查询返回 Store 投影，并可显示只读 checkpoint 摘要、当前中断节点、interrupt 状态；
- approve/reject 端点不得只改 Store；必须调用 codeops_graph.resume(taskId, decision)；
- graph 恢复后再把 task、approval、事件投影写回 Store；
- 保留旧空 body 行为：空 body approve 默认 APPROVE_DELIVERY，不写目标仓库；
- decisionId 重放返回幂等结果，不重复恢复或重复写入；
- 增加受限状态诊断端点或内部函数，只返回节点、状态、interrupt 摘要，不泄露完整 prompt、密钥、敏感工具参数；
- 启动时实现 reconcile：找 WAITING_APPROVAL 任务读 checkpoint，标记投影缺失、checkpoint 缺失或状态不一致；绝不自动批准。

职责边界：

| 数据 | 主职责 |
|---|---|
| LangGraph checkpoint | 图游标、当前 node、恢复输入、状态历史 |
| Store tasks | 任务列表、UI 查询、最终投影 |
| Store approvals | 审批检索、operator 审计、幂等决策 |
| Store audit_logs/tool_logs | 外部效果和安全审计 |
| 事件投影 | SSE 重连/补发和时间线展示 |

### 6.3 工具和目标仓库写入边界

修改 runtime.py 和 services.py：

1. 通用 AgentLoopService 只允许只读工具：snapshot、search、list、read snippet、git diff/log、find tests、知识检索、观测查询、后台状态；
2. 从通用 loop 的注册/暴露工具列表移除 repo.exact_replace；
3. repo.exact_replace 若保留，只能在专门 Repair 路径操作受管 PatchSandbox；
4. SecurityPolicy 必须按真实 repository 路径判断是否沙箱，不能因工具名就认为隔离；
5. 真实目标仓库写入只允许 apply_approved_patch 节点完成；
6. apply 节点要求 approved action、关联 approval、digest/baseline 复验、Scope Guard、静态校验、真实测试通过；
7. 写入审计包括 pre/post checksum、changed files、taskId、approvalId、decisionId、toolCallId、operator；
8. 所有路径使用 resolve 后 relative_to 根目录验证，拒绝路径穿越和符号链接逃逸；
9. 默认配置和默认 API 请求不得触发目标仓库写入。

### 6.4 M0 必测场景

| 场景 | 必须断言 |
|---|---|
| 首次审批 | graph 返回 interrupt；任务 WAITING_APPROVAL；原仓库 hash 不变 |
| SQLite 重启恢复 | 新 CheckpointerManager/new graph 使用同 taskId 恢复；resume 值在 human_approval 中可见 |
| 拒绝 | 状态 HUMAN_REJECTED；无源码写入；原因和审计持久化 |
| 批准交付 | 完成并输出 patch artifact；无目标仓库写入 |
| 明确批准应用 | 仅 action=APPROVE_APPLY_TO_WORKTREE 可写；所有 digest 一致 |
| 陈旧批准 | 基线或 patch 变化后阻止 apply，状态为 STALE_APPROVAL 并重新 interrupt |
| 决定重放 | 相同 decisionId 不会重复写文件、重复创建审计或二次恢复 |
| 通用 loop 写请求 | repo.exact_replace 被拒绝；源仓库 hash 不变 |
| 旧审批兼容 | 空 body approve 保持旧端点可用，但只做 delivery |
| 既有持久化 | completed state restart 测试仍通过 |

M0 完成门槛：完整 pytest 通过；新增测试通过；默认路径不存在未审批目标仓库写入；API/DTO 兼容；SQLite 恢复经实际测试验证。

## 7. M1：Reducer、并行采证与 CodeOps 事件流

### 7.1 Ops fan-out/fan-in

修改 src/ops_autoagent/graphs/ops.py：

1. understand_incident 后同时调度 collect_metrics、collect_logs、collect_traces；
2. 三个 node 只写各自独占字段 metrics/logs/traces，events/tool_trace 仅写增量；
3. 三路汇合到 evidence_barrier 或 retrieve_runbooks，再执行 Runbook、correlate、report；
4. tool budget 在 fan-out 前统一分配或原子预留，不能让每个分支各自超支；
5. 维持 plan-driven deny、ToolGovernance、负证据语义；
6. 第一版 supplement_evidence 仍保持顺序；只有完成独立补证任务模型和预算聚合后才引入 Send；
7. 用 feature flag OPS_PARALLEL_EVIDENCE_ENABLED 灰度开关，默认 false。

不变量：

- 主证据到达顺序可不同，但每项事件完整、可去重；
- RAG/reviewer 只能在三路完成或显式失败后开始；
- 预算不足时每一路都有可解释的 DENIED 证据；
- 并行化前后，同一 fixture 的最终诊断语义应等价。

### 7.2 事件流

Ops 从 stream_mode=values 的整 State 扫描，升级为优先消费 updates 增量并投影 TaskEvent。新增统一字段：eventId、taskId、stage、kind、attempt、status、timestamp、summary、artifactRefs。

CodeOps 新增：

- 保留原 POST submit 同步兼容端点；
- 新增 POST /api/v1/codeops/task/submit/stream 或 GET /api/v1/codeops/task/{taskId}/events；
- 流中至少出现 skill_started、skill_completed、tool_summary、sandbox_result、test_result、review_result、approval_required、resumed、completed/error；
- 断线不能取消任务；通过 taskId + Last-Event-ID 从 Store 事件投影补发；
- 不向 SSE 发送完整 prompt、密钥、原始敏感工具请求或超大响应。

### 7.3 M1 验收

- fake tools 下三路采证并发执行，总墙钟时间小于串行基线；
- 无 INVALID_CONCURRENT_GRAPH_UPDATE；
- events/tool_trace 无重复无丢失，按 eventId 可去重；
- tool calls 总量不超过 maxToolCalls；
- Ops 既有 SSE 合约仍通过；
- CodeOps 事件流可看见审批和最终状态；
- 断线重连只补漏失事件。

## 8. M2：子图与 Reviewer -> Repair 闭环

### 8.1 子图边界

| 子图 | 输入 | 输出 | 副作用 |
|---|---|---|---|
| OpsEvidenceSubgraph | incident、policy、budget | evidence bundle、provenance、review | 只读外部查询 |
| RepositoryInvestigationSubgraph | goal、repo snapshot、ops hints | localization、code evidence、scope | 只读仓库 |
| RepairProposalSubgraph | scope、snippet、failure feedback | patch、sandbox、compile result | 仅受管沙箱 |
| VerificationSubgraph | sandbox、test plan | real test result、diagnostic | 受控测试环境 |
| IndependentReviewSubgraph | evidence、patch facts、tests、knowledge | verdict、risk、retry instructions | 无写入 |

父图传递 artifact 摘要/ID，不在每个 checkpoint 复制完整日志或 prompt。完整内容放 Store 或工件目录。

### 8.2 独立 Reviewer 路由

将 Release Review 变成控制输入：

- RELEASE_READY：进入审批或交付；
- HUMAN_REVIEW：进入审批；
- RETRY_REPAIR：将 review feedback 写入 repair_feedback，回到 RepairProposalSubgraph；
- REJECT：终止或请求人工补证；
- NO_CODE_FIX：输出运维/配置建议，不进 PatchSandbox。

修复循环必须有 repairAttempt、max attempts=3、max tool/LLM budget、patchDigest 去重、scope expansion 重新审批、定位不足终止而非编造补丁。

Command(update=..., goto=...) 可以用于“原子更新决策并路由”；静态路线保持条件边，可读性优先。不要为了使用 Command 而移除清晰的 conditional edge。

## 9. M3：可观测性、评估和发布

### 9.1 新增运行时指标

- 各阶段耗时、队列等待、并行采证 latency；
- LLM 调用、模型层级、成本、结构化输出失败；
- 工具调用、拒绝、超时、重试；
- evidence 覆盖与 fixture fallback；
- repair attempts、patchDigest 变化、Scope Guard 拦截；
- 沙箱、真实 compile/test、后台任务等待；
- 审批等待、批准/拒绝/过期、stale approval；
- 未授权目标仓库写入数：必须恒为 0；
- checkpoint 恢复率、事件补发完整率、resume 成功率。

### 9.2 新增评估 Case

保留既有 50+ 业务 Case，新增：

- approval：批准、拒绝、重复决定、陈旧批准、重启恢复；
- safety：路径穿越、符号链接、直接写工具、敏感文件、非法 Maven 参数；
- concurrent：三路合并、预算耗尽、事件乱序、SSE 重连；
- reflection：编译失败、断言失败、scope 越界、Reviewer 退回、三次失败停止；
- background：任务运行中重启、终态通知丢失、重复通知；
- compatibility：旧 DTO、空审批 body、SQLite/PG checkpoint、legacy audit routes。

评估报告必须将效果指标、运行时安全、恢复可靠性、效率、人工作业负担分开报告，禁止把“未运行真实测试”归入“测试通过”。

### 9.3 发布与回滚

新增 Settings 和 .env.example，建议默认：

| 配置 | 默认 | 用途 |
|---|---|---|
| CODEOPS_HITL_APPROVAL_ENABLED | true | 目标仓库写入前中断 |
| CODEOPS_APPLY_MODE | delivery_only | 默认不写目标仓库 |
| CODEOPS_READ_ONLY_AGENT_TOOLS | true | 调查 loop 无写工具 |
| OPS_PARALLEL_EVIDENCE_ENABLED | false | 灰度启用并行 |
| CODEOPS_EVENT_STREAM_ENABLED | false | 保留同步端点优先 |
| LANGGRAPH_STATE_SCHEMA_VERSION | 2 | checkpoint 兼容 |
| CODEOPS_MAX_REPAIR_ATTEMPTS | 3 | 防止无界反思 |

发布顺序：

1. M0 在 fixture/CI 环境通过；
2. 生产先启用 interrupt + delivery_only；
3. 对比串行/并行采证后再灰度 M1；
4. 观察 checkpoint、Store、事件投影一致性；
5. 仅对明确授权的非生产仓库开放 APPLY_TO_WORKTREE；
6. 生产部署仍由 CI/CD 或人工系统执行。

回滚只关闭 feature flag，不删除 schema。对已暂停 thread 保留旧 node 名称或适配节点，直到线程完成、超时或归档。

## 10. 文件级改动清单

| 文件 | M0 | M1 | M2/M3 |
|---|---|---|---|
| graphs/codeops.py | interrupt、审批、受控 apply、resume | task events | 子图、review retry |
| graphs/ops.py | 保持兼容 | reducer、fan-out/fan-in | OpsEvidenceSubgraph |
| graphs/state_models.py | 新建 State/reducer/contracts helper | 事件/artifact schema | 子图 IO |
| schemas.py | ApprovalDecision action/decisionId | event DTO | 公开契约 |
| api.py | resume + Store 投影 + reconcile | SSE/replay | 观测管理端点 |
| persistence.py | checkpoint 摘要/恢复辅助 | event helper | state history |
| codeops/runtime.py | 禁用 generic direct write、apply checksum | 审计 | capability 边界 |
| codeops/services.py | read-only AgentLoop allowlist | 事件 | 子图适配 |
| codeops/test_verification.py | 后台幂等/恢复 | 终态事件 | worker protocol |
| config.py/.env.example | M0 flags | parallel/stream flags | metrics flags |
| tests | interrupt/restart/security | reducer/SSE | retry/eval |

建议提交顺序：

1. feat(codeops): typed approval state and flags
2. feat(codeops): durable interrupt/resume approval flow
3. fix(security): read-only generic agent loop and explicit apply boundary
4. test(codeops): restart, stale approval, idempotency, no-write coverage
5. feat(ops): reducer-backed State and parallel evidence flag
6. feat(api): CodeOps event stream and replay
7. refactor(graphs): extract subgraphs
8. feat(codeops): reviewer feedback retry loop
9. feat(eval): runtime safety/durability cases

每个提交必须说明不变量、测试结果、API/State/checkpoint 兼容性和新增副作用。

## 11. 完成定义

只有同时满足以下条件，才能称为“LangGraph 可恢复受控修复闭环”：

- 审批点真正 interrupt；
- SQLite/PostgreSQL checkpoint 能用同 taskId 恢复；
- 审批前没有目标仓库写入；
- 审批决定幂等，基线变化会阻止陈旧批准；
- 并行采证 State 无冲突、无丢失、事件可去重；
- Reviewer 能在有限预算内退回 Repair；
- CodeOps 有增量事件流和断线补发；
- LLM 决策均经结构化合同校验；
- 旧 API、DTO、Store、eval 和 pytest 均通过；
- 安全、恢复、并发和审批测试完整通过。

## 12. 参考

- https://docs.langchain.com/oss/python/langgraph/interrupts
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://docs.langchain.com/oss/python/langgraph/use-graph-api
- https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE
- https://docs.langchain.com/oss/python/langgraph/streaming

