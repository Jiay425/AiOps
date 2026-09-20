# 生产架构与验证

本项目把 LangGraph 用在单个 Incident-to-Fix 任务的控制平面，而不是用消息队列代替任务状态机。

## 运行边界

| 组件 | 职责 | 关键边界 |
| --- | --- | --- |
| LangGraph | 状态、条件路由、Checkpoint、审批中断、修复重试 | 不直接授予 LLM 生产写权限 |
| MySQL | 任务投影、事件、制品、Outbox、消费幂等键 | Task Event 与 Outbox 同事务写入 |
| Kafka | 版本化 Alert / Task Event 异步传输、Consumer Group、DLQ | Alert 与 Dispatch、Outbox 原子写入；不保存 LangGraph graph state |
| Neo4j | 服务、依赖、变更、RCA 路径查询 | RCA 只读；不能变更服务拓扑 |
| Kubernetes API | Runbook server-side dry-run | 所有请求强制 `dryRun=All` |
| OpenTelemetry / SkyWalking | HTTP Trace 与服务延迟观测 | OTLP 仅在显式配置端点时启用；不导出 Prompt 或密钥 |

## 验证顺序

1. 将 `deploy/.env.full.example` 复制为未跟踪的 `deploy/.env.full.local`，填入本地开发密码；不要提交此文件。
2. `docker compose --env-file deploy/.env.full.local -f deploy/docker-compose.full.yml up -d`。
3. 检查 Neo4j、Kafka、MySQL、Redis、Prometheus、Elasticsearch、SkyWalking、Grafana 和 App 的 healthcheck。
4. 通过受控同步命令将 CMDB / 发布系统导出的快照写入 Neo4j（Agent 没有写图权限）：
   `docker compose --env-file deploy/.env.full.local -f deploy/docker-compose.full.yml exec app ops-autoagent-topology-sync /app/fixtures/topology/order-service.json`。
   再通过 `/api/v1/topology/{service}` 和 `/correlation` 验证 Neo4j 图查询。
5. 向 Alertmanager webhook 提交一个 firing Alert，检查 MySQL 的 `alerts`、`dispatches` 与 `outbox_events` 同时出现；Outbox 从 `PENDING` 迁移为 `PUBLISHED`，Kafka consumer 随后触发 CodeOps 并写入 receipt 与 `processed_events`。
6. 重放相同 envelope；消费处理器应因相同 `idempotencyKey` 不重复执行业务处理。
7. 提交错误 payload；应在 `aiops.dlq.v1` 看到保留原始消息与错误类型的事件。

`CODEOPS_APPLY_MODE=delivery_only` 是默认设置。即便审批通过，也只交付补丁制品；切换到受控工作区写入需要另行审批，且仍只有 `apply_approved_patch` 能写入目标仓库。

## 拓扑快照契约

`ops-autoagent-topology-sync` 接受经过上游校验的 JSON。可同步的节点集合包括
`services`、`workloads`、`pods`、`nodes`、`databases`、`caches`、`topics`、
`repositories`、`modules` 与 `runbooks`。`dependencies` 的 `sourceKind` / `targetKind`
必须来自这些固定类型（默认 `service`）；`changes` 可以用 `targetKind` 关联到服务、
数据库、缓存或 Topic。标签和关系类型在代码中白名单化，绝不从快照拼接 Cypher。
