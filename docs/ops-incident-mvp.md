# 智能故障诊断系统说明

## 目标

当前方案已经覆盖从 MVP 到准生产化的主链路：

```text
用户输入问题
-> API Token 权限校验、限流、审计
-> 查询 Prometheus/Grafana 指标
-> 查询 ELK 日志
-> 查询 SkyWalking trace
-> 生成结构化证据链
-> 检索运维 runbook 知识库
-> LLM 基于证据链汇总分析
-> SSE 输出诊断报告
-> 诊断记录、审计日志、工具调用日志落库
```

外部系统未配置时不会中断流程，会返回“未配置/跳过查询”的可解释证据，方便先完成端到端演示。

## 接口

### 诊断分析

```http
POST /api/v1/ops/incident/analyze
Accept: text/event-stream
Content-Type: application/json
X-Ops-Token: your-token
X-Ops-User: operator-id
```

请求示例：

```json
{
  "serviceName": "ops-demo-service",
  "startTime": "2026-04-17 14:00:00",
  "endTime": "2026-04-17 14:10:00",
  "problem": "接口大量 500 错误，请分析原因并给出修复建议",
  "traceId": "",
  "maxStep": 6
}
```

curl 示例：

```bash
curl -N -X POST "http://localhost:8099/api/v1/ops/incident/analyze" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-Ops-Token: your-token" \
  -H "X-Ops-User: zhangsan" \
  -d '{
    "serviceName": "ops-demo-service",
    "startTime": "2026-04-17 14:00:00",
    "endTime": "2026-04-17 14:10:00",
    "problem": "接口大量 500 错误，请分析原因并给出修复建议",
    "traceId": "",
    "maxStep": 6
  }'
```

### 查询复盘记录

```http
GET /api/v1/ops/incident/record/{diagnosisId}
X-Ops-Token: your-token
X-Ops-User: operator-id
```

返回内容包含请求参数、指标证据、日志证据、链路证据、证据链、runbook、最终报告和错误信息。

## 配置

```yaml
ops:
  security:
    enabled: true
    api-token: your-token
  rate-limit:
    enabled: true
    max-requests: 20
    window-seconds: 60
  runbook:
    base-path: docs/dev-ops/runbook
  integrations:
    http:
      connect-timeout-seconds: 5
      request-timeout-seconds: 15
    prometheus:
      base-url: http://127.0.0.1:9090
      username:
      password:
    elk:
      base-url: http://127.0.0.1:9200
      index-pattern: logs-*
      username:
      password:
    skywalking:
      graphql-url: http://127.0.0.1:12800/graphql
      username:
      password:
```

说明：

- 大模型 `api-key` 建议使用环境变量，例如 `OPENAI_API_KEY`，不要直接写入配置文件。
- `ops.security.enabled=false` 时不校验 `X-Ops-Token`，适合本地演示。
- `ops.security.enabled=true` 后，请求头 `X-Ops-Token` 必须等于 `ops.security.api-token`。
- `X-Ops-User` 会进入审计日志，未传时记为 `anonymous`。
- 限流维度为 `clientIp + serviceName`。
- Prometheus、ELK、SkyWalking 的 HTTP 调用统一使用可配置连接超时和请求超时。

## 数据采集维度

### Prometheus / Grafana

当前会按服务名和时间窗口采集以下指标维度：

- 流量：QPS。
- 错误：5xx QPS、5xx 错误率。
- 延迟：平均 RT、P95、P99。
- 资源：进程 CPU、系统 CPU、JVM 内存。
- JVM：存活线程数、GC 平均暂停时间。
- Web 容器和线程池：Tomcat busy threads、executor active threads。
- 数据库连接池：Hikari active、max、usage percent、pending、timeout total。

PromQL 会兼容常见 Micrometer 标签：`application`、`job`、`service`。如果项目中的标签名不同，可以在 `PrometheusMetricGateway` 里继续加一组 label fallback。

### SkyWalking

当前会按服务名和时间窗口采集以下链路维度：

- traceId 精查：如果请求带 `traceId`，查询完整 span、tag、log、peer、component、error 标记。
- 服务级指标：service CPM、响应时间、SLA、Apdex。
- 异常链路样本：按时间窗口查询错误 trace。
- 慢链路样本：按 duration 排序查询慢 trace。

SkyWalking GraphQL 在不同版本中字段可能略有差异，所以这些查询采用 best-effort 方式：某个查询失败不会中断诊断，会进入 `spans` 和工具调用日志，方便复盘时判断是字段兼容问题、数据缺失还是服务异常。

## SSE 事件类型

| type | subType | 说明 |
| --- | --- | --- |
| analysis | intent | 诊断启动 |
| metric | prometheus | Prometheus/Grafana 指标证据 |
| log | elk | ELK 日志证据 |
| trace | skywalking | SkyWalking 链路证据 |
| analysis | evidence_chain | 结构化根因候选与证据链 |
| rag | runbook | 运维手册召回结果 |
| analysis | root_cause | 基于证据链开始根因分析 |
| report | diagnosis_report | 大模型汇总报告 |
| review | diagnosis_record | 诊断复盘记录已保存 |
| complete | diagnosis_completed | 诊断完成 |
| error | diagnosis_error | 诊断异常 |

## 第二阶段：证据链增强

每个根因候选都必须携带证据，结构如下：

```json
{
  "cause": "数据库或连接池异常导致接口错误",
  "category": "database",
  "confidence": 91,
  "reasoning": "日志、指标或链路中出现数据库、JDBC、连接池、SQL 超时等信号",
  "evidences": [
    {
      "source": "elk",
      "category": "log",
      "title": "数据库异常日志",
      "detail": "SQLTimeoutException...",
      "confidence": 85
    }
  ],
  "remediationSuggestions": [
    "检查 Hikari active/idle/max 连接数、连接等待时间和数据库慢 SQL"
  ]
}
```

LLM 报告会收到 `evidence_chain`，并被要求只能基于证据链给出根因判断；没有证据支撑的内容只能放到“待验证假设”。

## 第三阶段：运维知识库 RAG

当前先以本地 Markdown runbook 作为 RAG 知识源，目录为：

```text
docs/dev-ops/runbook
```

已内置以下手册：

- 数据库连接池耗尽排查手册
- 接口 500 故障排查流程
- Redis 超时处理方案
- JVM Full GC 排查流程
- Dubbo/RPC 调用超时排查流程
- 消息队列堆积处理流程

runbook 只作为修复知识，不作为生产故障证据；最终根因仍必须引用 `evidence_chain`。后续可以将 `FileOpsRunbookGateway` 替换为 PgVector 检索，实现真正的向量 RAG。

## 第四阶段：自动化报告和复盘

诊断完成后会生成 `diagnosisId`，并保存到 MySQL 表 `ops_incident_diagnosis`。

落库内容包括：

- 原始请求参数
- Prometheus/Grafana 指标证据
- ELK 日志证据
- SkyWalking 链路证据
- 结构化根因候选和证据链
- 召回的 runbook 知识
- 最终诊断报告
- 执行状态和异常信息

建表脚本：

```text
docs/dev-ops/mysql/sql/ops_incident_diagnosis.sql
```

诊断完成后 SSE 会推送：

```json
{
  "type": "review",
  "subType": "diagnosis_record",
  "content": "Diagnosis review record saved. diagnosisId=diag-..."
}
```

之后可以通过 `GET /api/v1/ops/incident/record/{diagnosisId}` 查询完整复盘记录。

## 第五阶段：准生产化

本阶段新增以下能力：

- 权限：`OpsApiGuard` 支持 `X-Ops-Token` 校验，可通过配置开关启用。
- 审计：诊断请求和复盘查询都会写入 `ops_audit_log`，记录操作者、IP、动作、资源、结果和原因。
- 脱敏：请求、SSE 输出、诊断报告、复盘记录、审计日志、工具调用日志会统一遮蔽访问令牌、密码、密钥、OpenAI key、手机号、邮箱。
- 限流：按 `clientIp + serviceName` 做固定窗口限流，防止故障期间重复触发大量诊断。
- 超时：Prometheus、ELK、SkyWalking HTTP 调用支持连接超时和请求超时配置。
- 工具调用日志：Prometheus、ELK、SkyWalking、runbook、LLM 调用都会写入 `ops_tool_call_log`，记录工具名、目标、请求摘要、响应摘要、耗时、状态码和错误信息；未配置或主动跳过也会记录，便于复盘时判断缺失证据的原因。

治理日志建表脚本：

```text
docs/dev-ops/mysql/sql/ops_governance_log.sql
```

