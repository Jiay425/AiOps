# Python LangGraph migration

## Runtime boundary

生产入口是 `python -m ops_autoagent.main`。根目录 `pyproject.toml`、`Dockerfile` 与 `langgraph.json` 定义唯一活动运行时。原 Spring AI 多模块源码完整保留在 `legacy-spring-ai/`，用于契约、类型及行为审计，但不再参与打包部署。

## Graphs and state

- `OpsDiagnosisGraph`：意图与计划 → 指标 → 日志 → Trace → 证据关联/条件补证 → Runbook → 报告。SSE 直接消费 LangGraph `astream`，不是事后回放。
- `CodeOpsGraph`：规划 → 仓库理解 → 动态技能 DAG → 测试验证 → 失败反思/重试 → LangGraph 人工审批 → 汇总。
- diagnosis ID/task ID 同时作为 LangGraph `thread_id`。checkpointer 支持内存、SQLite 和 PostgreSQL；审批可跨进程恢复。

## Compatibility

FastAPI 保留全部旧 `/api/v1/ops/**`、`/api/v1/codeops/**` 路由、camelCase DTO、SSE、控制台和 actuator 表面。MySQL 模式可读写原 MyBatis 表，并使用 JSON sidecar 保留 LangGraph 扩展状态；本地可用 SQLite。PGVector、Prometheus、Elasticsearch、SkyWalking、SMTP 与 Streamable HTTP/SSE MCP 均由异步 Python 客户端实现。

动态 `ai_client` 的 model/API/prompt 关系继续从原 MySQL 表解析，Planner、Evidence Reviewer、Report Writer 使用原 4101/4102/4103 client ID。生产模式下 `OPS_AGENT_CHAT_REQUIRED=true` 会严格失败；只有显式启用 `OPS_FIXTURE_FALLBACK=true` 的离线回归允许确定性降级。

## Verification

`scripts/verify.ps1` 依次执行编译、33 项 pytest、42 路由契约审计、55 个旧配置占位符审计、362 public type 映射/证据审计，以及全部 CodeOps/Ops/RAG fixtures。`legacy-spring-ai/` 是审计基线，不是隐藏的运行时回退。
