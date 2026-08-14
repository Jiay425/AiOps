# 完整生产化链路启动说明

这一套用于启动完整智能运维诊断链路：

```text
MySQL
Prometheus
Grafana
ELK Stack
SkyWalking
PgVector
Runbook 向量化 RAG
旧 Agent 自动装配
旧 RAG Advisor
MCP 客户端初始化
```

## 1. 启动完整基础设施

在项目根目录执行：

```bash
docker compose -f docs/dev-ops/docker-compose-ops-full.yml up -d
```

服务地址：

| 服务 | 地址 | 账号 |
| --- | --- | --- |
| MySQL | `127.0.0.1:13306` | `root / 123456` |
| PgVector | `127.0.0.1:15432` | `postgres / postgres` |
| PgAdmin | `http://127.0.0.1:5050` | `admin@qq.com / admin` |
| Prometheus | `http://127.0.0.1:9090` | 无 |
| Grafana | `http://127.0.0.1:4000` | `admin / admin` |
| Elasticsearch | `http://127.0.0.1:9200` | 无 |
| Kibana | `http://127.0.0.1:5601` | 无 |
| Logstash | `127.0.0.1:9600` | 无 |
| SkyWalking OAP | `http://127.0.0.1:12800` | 无 |
| SkyWalking UI | `http://127.0.0.1:18080` | 无 |

## 2. 使用 full 配置启动应用

启动 Spring Boot 时指定：

```bash
--spring.profiles.active=full
```

full 配置会打开：

```text
spring.datasource.pgvector.enabled=true
spring.ai.agent.auto-config.enabled=true
ops.runbook.vector.enabled=true
ops.runbook.vector.search-enabled=true
```

启动成功后，应用会：

```text
连接 MySQL
连接 PgVector
初始化 vectorStore
向量化 docs/dev-ops/runbook 下的故障手册
自动装配旧 Agent Client
初始化旧 RAG Advisor
初始化旧 MCP Client
连接 Prometheus / ELK / SkyWalking 查询网关
```

## 3. Runbook 向量化范围

启动时会把这些手册写入 PgVector：

```text
docs/dev-ops/runbook/database-connection-pool.md
docs/dev-ops/runbook/redis-timeout.md
docs/dev-ops/runbook/jvm-full-gc.md
docs/dev-ops/runbook/rpc-timeout.md
docs/dev-ops/runbook/http-500-error.md
docs/dev-ops/runbook/mq-backlog.md
```

可以用 PgAdmin 或 psql 检查：

```sql
SELECT metadata ->> 'runbookId' AS runbook_id, COUNT(1)
FROM vector_store_openai
WHERE metadata ->> 'source' = 'ops-runbook'
GROUP BY metadata ->> 'runbookId';
```

## 4. 打开诊断台

```text
http://127.0.0.1:8099/ops-console.html
```

点击：

```text
检查环境
```

应该能看到：

```text
应用：正常
MySQL：正常
PgVector：正常
Prometheus：正常
采集目标：正常
ELK：正常
SkyWalking：正常
```

再点击：

```text
一键演示闭环
```

完整链路会变成：

```text
制造 500 / 慢请求 / 数据库连接占用
-> Prometheus 采集指标
-> Logstash 采集本地 ERROR 日志
-> Elasticsearch 查询异常日志
-> SkyWalking 查询 trace / service metrics
-> PgVector 检索 runbook
-> LLM 汇总分析
-> SSE 输出诊断报告
-> MySQL 落库复盘记录
```

## 5. SkyWalking Trace 采集说明

Docker Compose 已启动 SkyWalking OAP 和 UI，并且应用 full 配置已连接：

```text
http://127.0.0.1:12800/graphql
```

要让 SkyWalking 真的出现 Java 调用链，需要应用 JVM 带 SkyWalking Java Agent 启动，例如：

```bash
-javaagent:/path/to/skywalking-agent/skywalking-agent.jar
-Dskywalking.agent.service_name=ops-demo-service
-Dskywalking.collector.backend_service=127.0.0.1:11800
```

没有 Java Agent 时，SkyWalking 服务会正常启动，但 trace 数据为空；诊断流程会明确提示 trace 数据不足。

## 6. MCP 初始化说明

full 配置会启用旧 Agent 自动装配：

```text
spring.ai.agent.auto-config.enabled=true
```

客户端配置来自 MySQL 初始化表：

```text
ai_client
ai_client_api
ai_client_model
ai_client_advisor
ai_client_tool_mcp
ai_client_config
```

其中 MCP 客户端会按数据库中的 `sse` 或 `stdio` 配置初始化。stdio 类 MCP 需要本机具备对应命令环境，例如 `npx`、`docker`。

## 7. 停止完整环境

```bash
docker compose -f docs/dev-ops/docker-compose-ops-full.yml down
```

清空所有数据：

```bash
docker compose -f docs/dev-ops/docker-compose-ops-full.yml down -v
```

