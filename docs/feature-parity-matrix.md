# Spring AI → Python LangGraph 功能等价矩阵

本表以 `legacy-spring-ai/` 为行为基线。当前表格是迁移范围清单，不再把类型映射和少量集成测试误标为 1:1 完成。每项只有在逐方法契约、分支、字段、事件、SQL 与异常行为均有黄金测试后，才能改为“完成”。

| 能力域 | Python LangGraph 实现 | 验证证据 | 状态 |
|---|---|---|---|
| Ops 状态编排 | `OpsDiagnosisGraph` 节点、条件补证循环、实时 `astream` SSE | `test_graphs.py`、全量 fixture | 审计中 |
| 指标/日志/链路 | Prometheus、ELK、SkyWalking 异步客户端；认证、连接/请求超时；MCP 优先与 HTTP 降级 | `test_ops_services.py`、全量 fixture | 审计中 |
| 证据链 | signal 提取、候选根因、充分性审查、缺失证据与补证回路 | `test_graphs.py`、Ops 指标审计 | 审计中 |
| 调查计划 | 结构化计划、步骤状态、工具预算、Planner/Reviewer 多 Agent | `test_graphs.py`、Ops 评测 | 审计中 |
| Runbook RAG | Java 等价 Markdown 分块、BM25、PGVector、RRF、rerank、维度检查与索引重试 | 12 个 RAG fixture，Top1/3/5 与 MRR=1.0 | 审计中 |
| 运维记忆 | LangGraph checkpoint、working state、历史事件存档与召回 | `test_persistence.py` | 审计中 |
| 工具治理 | policy、禁用工具、重复次数、调用记录、敏感字段脱敏 | `test_ops_services.py` | 审计中 |
| 告警链路 | normalize、dedup、持久优先队列、按服务并发限制、后台图任务 | `test_ops_services.py` | 审计中 |
| 通知 | owner 查询、模板、SMTP STARTTLS/auth、发送记录 | `test_ops_services.py` | 审计中 |
| Ops 评测 | fixture 驱动真实图执行、证据/工具/根因与 unsupported-claim 指标 | `run_full_parity.py` | 审计中 |
| CodeOps 状态编排 | 动态 skill DAG、agent/repair 循环、条件路由 | `test_orchestrator.py`、11 个 fixture | 审计中 |
| 仓库理解 | 安全搜索、snippet、结构、diff、证据图 | `test_codeops_runtime.py` | 审计中 |
| 代码定位/修复 | 定位、策略分类、补丁生成、失败诊断、反思重试 | CodeOps 全量 fixture | 审计中 |
| 补丁安全 | scope guard、独立 sandbox/worktree、diff 分析、validation；审批只更新任务状态，不写回原仓库 | `test_codeops_runtime.py` | 审计中 |
| 测试闭环 | Maven/Gradle/Pytest 等受控命令、超时、编译/测试门禁 | `test_codeops_runtime.py` | 审计中 |
| 人工审批 | Java 等价的任务完成后审批记录、风险/质量/证据门禁、通过与拒绝状态迁移 | `test_codeops_runtime.py`、`test_api.py` | 审计中 |
| 工具运行时 | 16 个原名工具、allowlist、参数校验、权限、调用预算与记录 | `test_codeops_runtime.py` | 审计中 |
| 安全治理 | 仓库边界、命令策略、敏感文件/写入 hook、审计 | `test_codeops_runtime.py` | 审计中 |
| 模型路由/成本 | flash/pro 路由与升级开关、token/cost 估算 | `test_codeops_runtime.py` | 审计中 |
| CodeOps 记忆 | 事故成功/失败记忆、召回、上下文压缩 | `test_orchestrator.py` | 审计中 |
| 调度 | 去重、优先队列、全局与 per-service 并发限制、队列持久化 | `test_orchestrator.py` | 审计中 |
| HTTP/静态前端 | 原 42 个 Spring MVC 路由、DTO camelCase、SSE、原控制台资源 | `audit_http_parity.py`（missing=0） | 审计中 |
| 数据库 | SQLite 本地后端；原 MySQL 表读写兼容与连接池；PGVector；SQLite/Postgres checkpointer | `test_persistence.py`、迁移脚本 | 审计中 |
| API 防护 | 原受保护路由范围、`X-Ops-Token`、IP+服务限流、ALLOW/DENY 审计 | `test_api.py` | 审计中 |
| 运维部署 | `pyproject.toml`、Docker、LangGraph 配置、PowerShell/Linux/多架构脚本、`.env` | compile/start/audit 脚本 | 审计中 |

类型映射审计只能证明 362 个 Java public type 都指定了 Python 目标，不能证明行为等价。`audit_method_parity.py` 当前识别出 2056 个显式 Java 方法；每个方法必须在 `migration-method-map.json` 中同时绑定真实 Python 目标和黄金测试。在差异清零前，本项目不得宣称已完成 1:1 迁移。
