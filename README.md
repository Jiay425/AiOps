# Ops AutoAgent Diagnosis

基于 Python 官方 [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) 的运维诊断与 CodeOps Agent。2.0 版本已从 Spring AI/Spring Boot 运行时迁移为 Python 3.11、LangGraph 和 FastAPI。

## 核心工作流

- Ops Diagnosis Graph：事件理解 → Prometheus → ELK → SkyWalking → 证据关联 → Runbook 检索 → 报告生成。
- CodeOps Graph：规划 → 仓库理解 → 技能执行 → 测试验证 → 条件重试/人工审批 → 汇总。
- LangGraph checkpointer 为每个 diagnosis/task 保存图执行状态，支持 SQLite/PostgreSQL 与审批跨进程恢复。
- 产品数据支持 SQLite 本地运行或兼容原表的 MySQL 连接池；Runbook 向量检索支持 PGVector。
- LLM 使用 OpenAI-compatible HTTP 协议并解析原 `ai_client` 数据库配置。生产 `chat.required` 严格失败；fixture 模式才允许确定性降级。

## 本地启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.venv\Scripts\python -m ops_autoagent.main
```

默认监听 `http://127.0.0.1:8099`：

- 控制台：`/`
- OpenAPI：`/docs`
- 健康检查：`/actuator/health`
- Prometheus 指标：`/actuator/prometheus`
- Ops SSE：`POST /api/v1/ops/incident/analyze`
- CodeOps：`POST /api/v1/codeops/task/submit`

## 配置

复制 `.env.example` 后配置：

- `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`
- `PROMETHEUS_BASE_URL`、`ELK_BASE_URL`、`SKYWALKING_GRAPHQL_URL`
- `OPS_DATABASE_PATH`、`MYSQL_URL`、`PGVECTOR_URL`、`OPS_RUNBOOK_PATH`
- `OPS_FIXTURE_FALLBACK`（本地验证时是否允许 fixtures 降级）
- `LANGGRAPH_CHECKPOINT_BACKEND`（`memory`/`sqlite`/`postgres`）

## 验证

```powershell
powershell -File scripts/verify.ps1 -Python .venv/Scripts/python.exe
```

完整验证包含 33 项 pytest、42 个旧 HTTP 路由、55 个旧配置占位符、362 个旧 Java public type，以及 11+11+12 个 CodeOps/Ops/RAG 行为 fixture。现有 `docs/`、`fixtures/` 与 `samples/` 继续作为运行手册、回归事件和示例服务资产使用。
