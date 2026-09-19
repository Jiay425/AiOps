<div align="right">

[English](README-en.md) · [中文](README.md)

</div>

<div align="center">

# Ops AutoAgent Diagnosis

### 从告警到自动修复的 LangGraph Agent

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#快速开始)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2.9-1C3C3C?logo=langchain&logoColor=white)](#langgraph-运行时)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](#api-接口)
[![Checkpoints](https://img.shields.io/badge/执行-SQLite%20%7C%20PostgreSQL-336791?logo=postgresql&logoColor=white)](#持久化执行)
[![Eval](https://img.shields.io/badge/评测-52%2B%20业务案例-6E40C9)](#评测体系)

**从告警开始，完成故障诊断、代码定位、补丁生成、测试验证和人工审批。**

</div>

---

## 它是怎么工作的？

线上出现告警后，Agent 会先读取监控指标、日志、调用链和 Runbook，
再让 LLM 判断最可能的故障原因。接着，它会去代码仓库中定位相关文件和方法，
在受控沙箱里生成补丁，执行编译和测试，最后交给独立的代码审查 Agent。
只有人工审批后，补丁才可能被应用到目标仓库。

这个项目是从 Spring AI/Spring Boot 迁移到 Python + LangGraph 的版本。
LangGraph 负责保存每一步状态、控制 Agent 循环、暂停等待审批，以及在中断后继续执行：

~~~text
故障 / Issue / Code Task
          │
          ▼
证据包 ──► 结构化诊断 ──► 仓库调查
  │                              │
  │                              ▼
  └──────────────────────────► 补丁提案
                                     │
                                     ▼
                            编译 / 测试验证
                                     │
                                     ▼
                            独立代码审查
                                     │
                             人工审批 / 仅交付
~~~

简单说，它就是一个“从告警到自动修复”的 Agent，但每一步都能查看、回放和限制。
模型只能提出修改建议，不能直接改生产代码。

## 核心亮点

| | 能力 | 含义 |
| --- | --- | --- |
| 🧭 | 先看证据再下结论 | 先采集 Metrics、Logs、Traces 和 Runbook，再生成故障诊断。 |
| 🔎 | 自动定位代码 | Agent 通过只读工具搜索仓库、查看调用关系和相关测试。 |
| 🛠️ | 自动生成补丁 | 补丁先在受控沙箱中生成和校验，不直接写入目标仓库。 |
| 🧪 | 编译和测试 | 修改后自动执行验证，并把结果交给审查 Agent。 |
| ⏸️ | 人工审批 | <code>interrupt()</code> 可以暂停流程，审批后使用同一个 <code>thread_id</code> 继续。 |
| 🔐 | 安全写入边界 | 只有 <code>apply_approved_patch</code> 可以真正修改目标仓库。 |
| 📊 | 可复盘评测 | 52 条业务 E2E Case、10 条安全/可靠性 Case，以及完整的事件和运行时指标。 |

## 架构

项目包含两个顶层图入口。<code>CodeOpsGraph</code> 负责从故障诊断走到代码修复；
<code>OpsDiagnosisGraph</code> 是独立的运维诊断入口，负责采集线上信息、生成诊断报告
和输出 SSE 事件。两者可以共享证据结果，但线上观测工具不会直接修改代码仓库。

~~~mermaid
flowchart LR
    U[告警 / Issue / API 请求] --> F[FastAPI]
    F --> C[CodeOpsGraph]

    C --> P[plan]
    P --> O[orchestrate]
    O --> D[诊断阶段]
    D --> E[OpsEvidenceSubgraph]
    E --> X[Metrics / Logs / Traces / Runbooks]
    D --> I[RepositoryInvestigationSubgraph]
    I --> R[修复阶段]
    R --> S[RepairProposalSubgraph]
    S --> V[VerificationSubgraph]
    V --> Q[IndependentReviewSubgraph]

    Q -->|ACCEPT / HUMAN_REVIEW| H[interrupt: 人工审批]
    Q -->|RETRY_REPAIR| R
    Q -->|REJECT / NO_CODE_FIX| Z[summarize]
    H -->|审批交付| L[deliver_patch]
    H -->|审批应用| A[apply_approved_patch]
    H -->|拒绝| N[rejected]
    L --> Z
    A --> Z
    N --> Z

    C -. 持久化状态 .-> K[(LangGraph Checkpointer)]
    C -. 事件 / 制品 / 指标 .-> M[(Store)]
    A -. 唯一生产写入 .-> T[目标仓库]

    OD[OpsDiagnosisGraph] --> OE[固定观测采集]
    OE --> OR[证据审查 / 报告]
    OR --> OS[SSE Stream]
~~~

### 三类 Agent 职责

即使实现层通过多个专用 Skill Node 进行路由，业务职责仍然明确分为三类：

| 角色 | 输入 | 输出 | 副作用边界 |
| --- | --- | --- | --- |
| **Diagnosis Agent** | 故障上下文和固定线上观测源 | 证据包、根因候选、置信度、缺失证据和修复约束 | 只读 |
| **Repair Agent** | 诊断契约和仓库上下文 | 定位文件/方法、补丁提案、Patch Digest、验证计划 | 仅受管 Patch Sandbox |
| **Review Agent** | 补丁事实、Scope Guard、编译/测试结果和风险上下文 | 结构化发布结论、重试约束和人工审批点 | 只读 |

父图统一负责时序、预算、审批、重试策略和副作用。Agent 不能通过直接调用
工具绕过这些策略。

## 图拓扑

### <code>CodeOpsGraph</code> —— 自动修复流程

~~~text
START
  → plan
  → orchestrate
      ├─ ops_diagnosis
      ├─ agent_loop_investigation
      ├─ repo_understanding
      ├─ engineering_knowledge_rag
      ├─ bug_fix
      ├─ test_verification
      ├─ release_risk_analysis
      └─ pr_review
  → finish
      ├─ retry_repair → repair_feedback → bug_fix
      ├─ approval → prepare_approval → human_approval
      │                         ├─ deliver_patch
      │                         ├─ apply_approved_patch
      │                         └─ rejected
      └─ summarize → END
~~~

Orchestrator 根据任务类型、Working Memory、已执行 Skill、关注范围和剩余预算
选择下一步。每个路由最终都回到父图，因此任务状态、尝试次数和副作用始终由
父图统一维护。

### 可复用子图

每个子图都是一个独立编译的 <code>StateGraph</code>，拥有小而稳定的状态契约。
父图以任务级 <code>thread_id</code> 调用子图，只把经过校验的输出和 Artifact
引用挂回父状态。

| 子图 | 内部流程 | 职责 |
| --- | --- | --- |
| <code>OpsEvidenceSubgraph</code> | <code>prepare_input → collect_and_review_evidence → publish_contract</code> | 规范化线上证据和证据充分性，不写入代码。 |
| <code>RepositoryInvestigationSubgraph</code> | <code>prepare_input → readonly_investigation → publish_contract</code> | 只读执行仓库搜索、快照、文件片段、Diff、历史、测试和工程知识检索。 |
| <code>RepairProposalSubgraph</code> | <code>prepare_input → sandbox_repair_proposal → publish_contract</code> | 在受管 Patch Sandbox 内生成并校验补丁提案。 |
| <code>VerificationSubgraph</code> | <code>prepare_input → run_verification → publish_contract</code> | 把编译、测试和后台任务事实规范化为验证契约。 |
| <code>IndependentReviewSubgraph</code> | <code>prepare_input → review_patch_facts → publish_contract</code> | 校验独立代码审查结果，并对不安全的发布结论降级。 |

这种拆分让工作流可以被检查：一个 Checkpoint 同时可以展示父图当前节点和
子图产生的领域制品。

## LangGraph 运行时

本项目使用 LangGraph 构建持久化状态机，而不是只把它当作 Prompt Router。

| LangGraph 能力 | 项目中的位置 | 作用 |
| --- | --- | --- |
| Typed Graph State | <code>CodeOpsState</code>、<code>OpsState</code>、<code>SubgraphState</code> | Node 之间传递结构化状态，而不是不透明的聊天文本。 |
| Conditional Edge | <code>orchestrate</code>、<code>finish</code>、<code>human_approval</code>、<code>apply_approved_patch</code> | 路由决策显式化、可测试化。 |
| Reducer | <code>events</code>、<code>tool_trace</code>、<code>effect_log</code> 使用追加型 List Reducer | 并行分支和重复尝试追加历史，而不是覆盖历史。 |
| Fan-out / Fan-in | Metrics、Logs、Traces 并行采集和 Evidence Barrier | 独立证据源可以并发执行，并在屏障节点确定性汇合。 |
| Nested Subgraph | 五个 CodeOps 领域子图 | 子图保持领域契约内聚，父图仍拥有全局策略控制权。 |
| Checkpoint | Memory、SQLite、PostgreSQL Saver | 任务可以被检查、恢复，并跨进程对账。 |
| Interrupt / Resume | <code>interrupt()</code> 和 <code>Command(resume=...)</code> | 人工审批暂停图执行，同时保留完整运行状态。 |
| Streaming | <code>astream(..., stream_mode="updates")</code> 和 FastAPI SSE | 客户端接收 Node 事件并回放任务时间线。 |
| 有界反馈循环 | Repair Feedback、轮次限制、Tool Budget、Retry Budget | Agent 自主性受到确定性预算约束。 |

### 持久化执行

每个 CodeOps 任务都使用任务 ID 作为 LangGraph 的 <code>thread_id</code>。
Checkpointer 保存当前节点、待处理 Interrupt、Approval Identity、Patch Digest
和重试上下文：

~~~json
{
  "threadId": "task-id",
  "currentNode": ["human_approval"],
  "status": "WAITING_APPROVAL",
  "approvalId": "approval-id",
  "interruptPending": true
}
~~~

通过配置选择 Checkpoint 后端：

~~~text
memory      进程内测试和短生命周期开发运行
sqlite      默认的本地持久化部署
postgres    多进程 / 服务化部署
~~~

终态的内存 Checkpoint 会被清理；可恢复的审批 Checkpoint 会保留。这样既能让
本地开发保持可预测，也不会改变持久化后端契约。

## 数据与安全边界

模型提出意图，运行时负责校验并物化副作用：

~~~text
LLM 意图
  → 类型化诊断 / 调查 / 补丁 / 审查契约
  → Scope Guard + Patch Validation
  → 受管 Patch Sandbox
  → 编译和测试验证
  → 独立代码审查
  → 人工审批 Interrupt
  → 交付制品或 apply_approved_patch
  → 目标仓库
~~~

- 模型不会获得直接写入生产仓库的工具。
- 补丁生成隔离在 <code>RepairProposalSubgraph</code> 和 <code>PatchSandbox</code> 后。
- <code>apply_approved_patch</code> 是唯一的生产仓库变更边界。
- <code>CODEOPS_APPLY_MODE=delivery_only</code> 保持为保守默认值。
- Scope Guard、Patch Validation、Baseline Digest 和 Patch Digest 防止审批后
  目标仓库状态发生未察觉的漂移。
- Review 输出必须经过结构化校验，并与确定性的补丁/测试事实对照；不安全的
  <code>RELEASE_READY</code> 结论会降级为人工审查。
- Trace、SSE、Event 和 Eval Projection 都经过脱敏和长度限制。
- 密钥只允许放在未跟踪的本地 <code>.env</code> 或部署平台 Secret Manager 中，
  不进入源码、Fixture 或提交后的评测输出。

## 证据层

~~~text
Prometheus Metrics ─┐
ELK Logs           ─┼──► Evidence Signals ───► Diagnosis Contract
SkyWalking Traces  ─┤
Runbook Retrieval  ─┘
~~~

证据会保留来源、可用性、负证据和审查约束。一个数据源如果成功查询但没有发现
异常，会被保留为负证据，而不会被静默地当成缺失数据。

CodeOps 父图把诊断契约作为仓库调查的输入。独立 Ops 图还支持串行/并行采集、
Evidence Barrier、由审查结果驱动的补充采集，以及面向事件诊断的 SSE Stream。

## 评测体系

仓库提供的是 Case Catalog 和执行 Harness，而不是一个手工挑选的 Demo：

| 套件 | 覆盖范围 | 状态 |
| --- | --- | --- |
| 业务 E2E | 52 条：16 条历史基线 + 36 条扩展 Case | 标记为 <code>E2E_BUSINESS</code> |
| 运行时安全 / 可靠性 | 10 条 | 与业务效果单独报告 |
| Fixture Provenance | 脱敏事件遥测和样例仓库 | 每条 Case 记录 Fixture 引用及复用关系 |

业务 Case 覆盖分布式一致性、数据库与基础设施故障、配置问题、代码质量、
Issue-to-Patch、发布风险、范围治理、验证和 Reviewer 反馈。

评测报告将以下维度明确拆开：

~~~text
证据质量
根因 / 代码定位质量
补丁提案质量
验证结果
审查结论
修复尝试次数与预算
Checkpoint / SSE Replay 行为
Scope Guard 与未授权写入安全性
~~~

Fixture 运行会显式标记来源。如果真实 LLM 或外部服务不可用，Harness 会报告
对应阶段不可用，而不会伪造 Reviewer、Maven 或线上观测成功。

## API 接口

FastAPI 将图暴露为标准 HTTP 和 SSE 契约：

| 能力 | Endpoint |
| --- | --- |
| 服务健康检查 | <code>GET /actuator/health</code> |
| OpenAPI | <code>GET /docs</code> |
| Ops 诊断流 | <code>POST /api/v1/ops/incident/analyze</code> |
| CodeOps 任务提交 | <code>POST /api/v1/codeops/task/submit</code> |
| CodeOps 任务事件 / 回放 | <code>GET /api/v1/codeops/task/{task_id}/events</code> |
| 审批状态 | <code>GET /api/v1/codeops/evaluation/approval/{task_id}</code> |
| 业务评测 Catalog | <code>GET /api/v1/codeops/evaluation/cases</code> |
| 评测汇总 | <code>GET /api/v1/codeops/evaluation/summary</code> |
| 运行时安全 Case | <code>GET /api/v1/codeops/evaluation/runtime/cases</code> |
| Prometheus 指标 | <code>GET /actuator/prometheus</code> |

SSE Projection 只包含有界的事件摘要、Artifact 引用、Subgraph/Node 身份、
Attempt 编号和回放元数据，不暴露完整 Prompt、密钥或不受限制的工具响应。

## 仓库结构

~~~text
ops-autoagent-diagnosis-python/
├── src/ops_autoagent/
│   ├── graphs/
│   │   ├── codeops.py       # CodeOps 父图和路由策略
│   │   ├── ops.py           # 独立事件诊断图
│   │   ├── subgraphs.py     # 领域子图和契约
│   │   └── state_models.py  # Reducer、Digest 和持久化状态辅助函数
│   ├── codeops/             # 工具、修复服务、评测器和策略
│   ├── ops/                 # 观测、Runbook 和事件服务
│   ├── api.py               # FastAPI、SSE、审批和评测接口
│   ├── persistence.py       # LangGraph Checkpoint 后端选择
│   ├── schemas.py            # Pydantic 契约
│   └── config.py             # 基于环境变量的配置
├── tests/                    # 图、API、安全和回归测试
├── fixtures/incident/        # 脱敏事件和评测 Fixture
├── samples/                  # 样例仓库和验证资产
├── docs/                     # 迁移、架构和 Runbook 文档
├── pyproject.toml
└── .env.example
~~~

## 快速开始

要求 Python 3.11 或更高版本。

~~~powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv/Scripts/python.exe -m ops_autoagent.main
~~~

默认本地服务监听 <code>http://127.0.0.1:8099</code>：

~~~text
console      /
OpenAPI      /docs
health       /actuator/health
metrics      /actuator/prometheus
~~~

只在本地 <code>.env</code> 中配置凭证和服务地址。默认开发姿态是支持 Fixture、
启用 Checkpoint、并保持 Delivery-only。

## 配置

| 变量 | 用途 |
| --- | --- |
| <code>OPENAI_BASE_URL</code> / <code>OPENAI_API_KEY</code> / <code>OPENAI_MODEL</code> | OpenAI-compatible 模型服务地址、密钥和模型 |
| <code>LANGGRAPH_CHECKPOINT_BACKEND</code> | <code>memory</code>、<code>sqlite</code> 或 <code>postgres</code> |
| <code>LANGGRAPH_CHECKPOINT_PATH</code> | SQLite Checkpoint 文件 |
| <code>LANGGRAPH_CHECKPOINT_POSTGRES_URL</code> | PostgreSQL Checkpoint 连接 |
| <code>CODEOPS_HITL_APPROVAL_ENABLED</code> | 是否在交付/应用前启用审批 Interrupt |
| <code>CODEOPS_APPLY_MODE</code> | 保守默认值：<code>delivery_only</code> |
| <code>CODEOPS_SUBGRAPHS_ENABLED</code> | 是否启用领域子图契约 |
| <code>OPS_FIXTURE_FALLBACK</code> | 本地验证时是否允许确定性 Fixture |
| <code>PROMETHEUS_BASE_URL</code> / <code>ELK_BASE_URL</code> / <code>SKYWALKING_GRAPHQL_URL</code> | 外部观测数据源 |
| <code>OPS_RUNBOOK_PATH</code> / <code>PGVECTOR_URL</code> | Runbook 来源和可选向量检索 |

不要把生产凭证、私钥或 Bearer Token 放进仓库。使用被 Git 忽略的
<code>.env</code> 或部署平台的 Secret Manager。

## 验证

执行仓库验证脚本：

~~~powershell
powershell -File scripts/verify.ps1 -Python .venv/Scripts/python.exe
~~~

也可以执行重点检查：

~~~powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m compileall src
git diff --check
~~~

仓库记录的最近一次迁移/评测验证已通过 Python 测试、编译检查、Diff 检查和
样例 Maven 验证。真实 LLM、Prometheus、ELK、SkyWalking 和 Maven 的结果仍然
取决于运行环境，不会从 Fixture 的存在推断出来。

## 迁移说明

项目保留了原 Spring AI 版本的领域意图——事件证据、Runbook 上下文、仓库修复、
风险审查和验证，同时把执行语义迁移到 Python 和 LangGraph：

~~~text
Spring AI / Spring Boot Services
              ↓ 迁移
Python 3.11 + LangGraph + FastAPI
~~~

重要变化不只是编程语言。State、Routing、Checkpoint、Interrupt、Subgraph、
Streaming 和 Effect Boundary 现在都直接呈现在图中，因此每次运行更容易检查、
回放、测试和解释。

## 项目范围

Ops AutoAgent Diagnosis 是工程自动化和决策支持 Harness。它不是无人值守的生产
发布器，不替代 SRE 审批，也不保证每个故障都存在代码修复。当证据、模型访问、
仓库上下文或验证能力不足时，运行时会保留限制信息，并在合适的边界停止。
