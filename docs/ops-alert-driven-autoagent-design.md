# 告警驱动自动诊断与通知系统落地方案

## 1. 文档定位

本文档是 `ops-autoagent-diagnosis` 项目中“告警驱动自动诊断与通知系统”的正式落地基线文档。

从本文档落地开始，后续所有相关改造必须满足以下规则：

1. 新增功能、表结构、接口、配置、任务流转，必须以本文档为准。
2. 若实现过程中发现需要调整方案，必须先更新本文档，再修改代码。
3. 不允许绕开本文档直接增加“临时逻辑”“演示分支逻辑”或与本文档冲突的实现。
4. 本文档覆盖范围内的后续开发，以“告警自动触发 -> AutoAgent 自动诊断 -> 自动通知”的生产闭环为唯一目标，不再沿用“纯人工触发”的思路扩散实现。

一句话：**本文档是后续改造的唯一执行基线。**

---

## 2. 背景与目标

当前项目已经具备以下能力：

1. 可通过 `POST /api/v1/ops/incident/analyze` 手动触发诊断。
2. AutoAgent 已具备 Step1 ~ Step7 的执行链路：
   - Step1 故障意图识别
   - Step2 Prometheus 指标采集
   - Step3 ELK 日志采集
   - Step4 SkyWalking 链路采集
   - Step5 证据交叉分析
   - Step6 根因判断与 Runbook 检索
   - Step7 多 Agent 审核与诊断报告生成
3. 诊断结果可落库到 `ops_incident_diagnosis`，工具调用过程可落库到 `ops_tool_call_log`。
4. 已接入真实 Prometheus、ELK、SkyWalking、PgVector，具备真实证据采集能力。

当前缺失的关键能力是：

1. 线上告警自动接入。
2. 告警自动标准化为诊断指令。
3. 告警去重、防抖、并发控制。
4. 异步触发 AutoAgent，而不是由页面或人工直接调用。
5. 根据服务负责人自动发送邮件/IM 通知。
6. 对诊断任务和通知任务做状态治理、失败重试和审计留痕。

### 目标

将当前“人工触发的自动诊断系统”升级为：

**告警驱动的自动诊断与通知系统**

完整闭环如下：

```text
监控/日志平台发现异常
-> Webhook 发送告警到本系统
-> 告警事件标准化
-> 告警去重/防抖/并发控制
-> 异步触发 AutoAgent 自动诊断
-> 诊断结果落库
-> 根据服务负责人自动通知
-> 支持升级提醒、失败重试与审计留痕
```

---

## 3. 总体架构

### 3.1 逻辑分层

系统升级后分为五层：

1. **告警接入层**
   - 接收 Alertmanager/Grafana/ELK 等外部告警 Webhook。
2. **告警治理层**
   - 完成标准化、去重、防抖、服务归属路由、严重级别决策。
3. **诊断调度层**
   - 将告警转换为内部诊断任务，并异步触发现有 AutoAgent。
4. **诊断执行层**
   - 复用现有 Step1 ~ Step7 执行链路。
5. **通知编排层**
   - 根据服务负责人和故障级别，自动发送邮件/IM，记录通知结果。

### 3.2 核心原则

1. **复用现有 AutoAgent 主链路，不推翻已有诊断内核。**
2. **新增能力优先放在 AutoAgent 前后，不侵入证据采集主逻辑。**
3. **所有自动触发必须异步化，不允许在 Webhook 请求线程中长时间执行诊断。**
4. **所有外部事件、诊断任务、通知结果必须持久化。**
5. **所有自动化动作必须可审计、可回溯、可去重。**

---

## 4. 目录与代码落位规范

后续代码按以下位置落地，不允许随意分散：

### 4.1 Trigger 层

路径：

`ops-autoagent-trigger/src/main/java/com/opsautoagent/trigger/http`

新增类：

- `OpsAlertWebhookController`

职责：

- 对外提供告警接收接口。
- 仅做鉴权、请求校验、调用应用服务，不写复杂业务逻辑。

### 4.2 Domain 层

路径：

`ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops`

新增包建议：

- `service.alert`
- `service.dispatch`
- `service.notify`
- `model.entity.alert`
- `model.valobj.alert`

新增核心类建议：

- `OpsAlertIngestService`
- `OpsAlertNormalizeService`
- `OpsAlertDedupService`
- `OpsIncidentCommandFactory`
- `OpsDiagnosisDispatchService`
- `OpsDiagnosisJobExecutor`
- `OpsServiceOwnerService`
- `OpsNotificationService`
- `OpsNotificationTemplateService`

### 4.3 Infrastructure 层

路径：

`ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure`

新增方向：

- Webhook 数据落库 DAO
- 服务负责人 DAO
- 通知记录 DAO
- 邮件发送网关
- 企业微信/钉钉发送网关

### 4.4 Docs 与 SQL

路径约束：

- 设计文档统一写在 `docs/`
- SQL 统一写在 `docs/dev-ops/mysql/sql/`

新增 SQL 文件建议：

- `docs/dev-ops/mysql/sql/ops_alert_event.sql`
- `docs/dev-ops/mysql/sql/ops_diagnosis_dispatch.sql`
- `docs/dev-ops/mysql/sql/ops_service_owner.sql`
- `docs/dev-ops/mysql/sql/ops_notification_record.sql`

---

## 5. 功能范围

### 5.1 本次必须落地

第一阶段必须实现：

1. Alertmanager Webhook 接入。
2. 统一告警模型 `OpsAlertEvent`。
3. 告警自动转换为 `IncidentCommandEntity`。
4. 告警去重与防抖。
5. 异步触发现有 AutoAgent 诊断。
6. 自动通知服务负责人邮箱。
7. 告警、调度、通知三类记录落库。

### 5.2 第二阶段扩展

第二阶段扩展能力：

1. Grafana Alert Webhook。
2. ELK 告警接入。
3. 企业微信/钉钉通知。
4. 升级提醒与恢复通知。
5. 通知失败重试与补偿。
6. 任务队列化与更细粒度并发控制。

### 5.3 本次明确不做

当前阶段不做以下内容：

1. 自动扩容、自动熔断、自动回滚等“自动处置”动作。
2. 跨租户、跨环境复杂权限模型。
3. 多区域、多集群全局调度。
4. 面向所有告警源的一次性全量适配。

说明：当前目标是先把“自动触发 + 自动诊断 + 自动通知”闭环做扎实。

---

## 6. 统一事件模型设计

新增统一事件模型：

### 6.1 `OpsAlertEvent`

建议字段：

- `eventId`
- `source`
- `serviceName`
- `alertRule`
- `severity`
- `status`
- `fingerprint`
- `traceId`
- `startsAt`
- `endsAt`
- `labelsJson`
- `annotationsJson`
- `rawPayload`

### 6.2 设计原则

1. 外部告警源格式各不相同，但进入系统后必须统一转换为 `OpsAlertEvent`。
2. 后续调度、去重、通知均基于 `OpsAlertEvent`，不直接依赖原始外部 payload。
3. 原始 payload 必须原样保留，便于排障与审计。

---

## 7. 接口设计

### 7.1 Alertmanager 接入接口

```http
POST /api/v1/ops/alert/webhook/alertmanager
Content-Type: application/json
X-Ops-Token: xxx
```

职责：

1. 接收 Alertmanager Webhook。
2. 校验 token。
3. 将原始告警转换为 `OpsAlertEvent`。
4. 投递到调度服务。
5. 立即返回 200，避免阻塞告警系统。

### 7.2 后续预留接口

```http
POST /api/v1/ops/alert/webhook/grafana
POST /api/v1/ops/alert/webhook/elk
```

---

## 8. 告警转诊断规则

新增 `OpsIncidentCommandFactory`，把 `OpsAlertEvent` 转换为现有 `IncidentCommandEntity`。

### 8.1 映射规则

- `serviceName <- alertEvent.serviceName`
- `traceId <- alertEvent.traceId`
- `startTime <- startsAt 往前扩展 10 分钟`
- `endTime <- 当前时间或告警时间`
- `problem <- 根据告警规则生成模板化问题描述`
- `diagnosisId <- 系统生成`
- `sessionId <- 系统生成`

### 8.2 问题模板

示例：

```text
ops-demo-service 在最近 10 分钟触发告警 [HTTP_5XX_RATE_HIGH]，请分析 Prometheus 指标、ELK 日志、SkyWalking 链路与运维 Runbook，判断根因候选，并给出临时止血与长期优化建议。
```

规则要求：

1. 问题描述必须结构化。
2. 问题描述必须包含服务名、故障窗口、告警规则。
3. 问题描述统一由系统生成，不依赖人工输入。

---

## 9. 去重、防抖与并发控制

### 9.1 去重键

统一 dedup key：

```text
serviceName + alertRule + fingerprint + severity
```

### 9.2 去重规则

1. 同一 dedup key 在 5 分钟内仅触发一次诊断。
2. 若已有相同服务的诊断任务处于 `RUNNING` 状态，则新告警只记录，不再新建诊断。
3. 若告警已恢复（resolved），不再触发新诊断，只更新事件状态并决定是否发送恢复通知。

### 9.3 并发规则

1. 单服务同一时刻最多仅允许 1 个诊断任务执行。
2. 系统设置全局最大诊断并发数，例如 5。
3. 当并发打满时，新任务进入 `PENDING` 队列，按优先级调度。

### 9.4 优先级规则

建议：

- P1 最高
- P2 次高
- P3 常规
- P4 低优先级

---

## 10. 调度与执行设计

### 10.1 当前阶段调度方式

第一阶段采用：

**MySQL 任务表 + Spring 异步线程池**

原因：

1. 与现有项目集成成本最低。
2. 易于演示、排查、落库和审计。
3. 后续可平滑升级为 MQ。

### 10.2 调度流程

```text
Webhook 收到告警
-> 保存 ops_alert_event
-> 生成 dedup key
-> 去重/防抖判断
-> 创建 ops_diagnosis_dispatch
-> 异步执行 OpsIncidentExecuteStrategy
-> 更新 dispatch 状态
```

### 10.3 与现有诊断链的关系

现有类不重写：

- `OpsIncidentExecuteStrategy`
- `OpsRootNode`
- `AbstractOpsAgentExecuteSupport`

只新增调度入口，在生成 `IncidentCommandEntity` 后复用现有主链路。

这是本方案的硬约束：**不允许重新造一个平行诊断流程。**

---

## 11. 通知设计

### 11.1 通知对象

基于 `serviceName -> owner` 路由。

负责人来源：

- `ops_service_owner` 表

建议字段：

- `serviceName`
- `ownerName`
- `ownerEmail`
- `ownerWecom`
- `ownerDingTalk`
- `backupOwnerEmail`
- `enabled`

### 11.2 第一阶段通知通道

第一阶段仅实现：

- 邮件通知

### 11.3 第二阶段通知通道

后续扩展：

- 企业微信
- 钉钉

### 11.4 通知触发规则

1. 诊断完成且成功生成报告后发送通知。
2. 若诊断失败但已生成 fallback 报告，也要发送通知，但需标记为降级结果。
3. 若重复告警命中去重规则，不重复发送同类型通知。

### 11.5 通知内容要求

通知至少包含：

1. 服务名
2. 告警规则
3. 严重级别
4. 诊断时间
5. 根因候选 TopN
6. 临时止血建议
7. 诊断 ID
8. 诊断详情访问入口

---

## 12. 数据库设计

### 12.1 `ops_alert_event`

用途：

- 记录外部告警原始事件

建议字段：

- `event_id`
- `source`
- `service_name`
- `alert_rule`
- `severity`
- `status`
- `fingerprint`
- `trace_id`
- `labels_json`
- `annotations_json`
- `raw_payload`
- `received_time`

### 12.2 `ops_diagnosis_dispatch`

用途：

- 记录告警到诊断任务的调度过程

建议字段：

- `dispatch_id`
- `event_id`
- `diagnosis_id`
- `dedup_key`
- `dispatch_status`
- `skip_reason`
- `start_time`
- `end_time`

### 12.3 `ops_service_owner`

用途：

- 记录服务与负责人关系

建议字段：

- `id`
- `service_name`
- `owner_name`
- `owner_email`
- `owner_wecom`
- `owner_dingtalk`
- `backup_owner_email`
- `enabled`
- `create_time`
- `update_time`

### 12.4 `ops_notification_record`

用途：

- 记录通知发送结果

建议字段：

- `notification_id`
- `diagnosis_id`
- `service_name`
- `channel`
- `receiver`
- `severity`
- `send_status`
- `retry_count`
- `error_message`
- `send_time`

---

## 13. 配置设计

新增配置统一放在 `application-full.yml` 与后续环境配置中。

建议结构：

```yaml
ops:
  alert:
    enabled: true
    webhook:
      token: your-token
    dedup:
      enabled: true
      window-seconds: 300
    dispatch:
      enabled: true
      max-concurrent: 5
      queue-size: 100
  notify:
    enabled: true
    email:
      enabled: true
      from: ops@company.com
    wecom:
      enabled: false
    dingtalk:
      enabled: false
```

配置规则：

1. 所有新配置必须归档到 `ops.alert` 或 `ops.notify`。
2. 不允许把告警、调度、通知配置散落在其他命名空间。

---

## 14. 分阶段实施计划

### Phase 1：告警自动触发最小闭环

目标：

- 从手动触发升级到告警自动触发
- 自动邮件通知负责人

交付项：

1. `OpsAlertWebhookController`
2. `OpsAlertEvent` 统一模型
3. `OpsIncidentCommandFactory`
4. `ops_alert_event.sql`
5. `ops_diagnosis_dispatch.sql`
6. `ops_service_owner.sql`
7. `ops_notification_record.sql`
8. 邮件通知实现
9. 去重防抖实现
10. AutoAgent 自动调度接入

验收标准：

1. 外部 Alertmanager 告警能够自动进入系统。
2. 系统可自动触发现有 AutoAgent 诊断。
3. MySQL 中可看到告警事件、调度记录、诊断记录、通知记录。
4. 负责人邮箱可收到诊断通知。

### Phase 2：生产治理增强

交付项：

1. Grafana/ELK Webhook 接入
2. 企业微信/钉钉通知
3. 升级提醒
4. 恢复通知
5. 失败重试
6. 更细粒度并发治理

### Phase 3：高级自动化

交付项：

1. MQ 化调度
2. 更复杂的告警聚合
3. 自动处置预案接口预留

---

## 15. 验收口径

后续每次改造都按以下口径验收：

1. 是否符合本文档中的模块边界。
2. 是否复用了现有 AutoAgent 主链路，而不是新造平行流程。
3. 是否新增了落库与审计。
4. 是否具备去重、防抖、并发控制。
5. 是否能够把结果发送给正确的服务负责人。
6. 是否补充了文档和 SQL，而不是只改代码。

如果不满足以上任何一项，则视为“跑偏实现”。

---

## 16. 变更控制规则

从现在开始，涉及以下范围的改动，必须先更新本文档再改代码：

1. 新增告警接入方式
2. 修改告警调度策略
3. 修改 dedup 规则
4. 修改通知路由规则
5. 修改新增表结构
6. 改变目录落位与模块边界

后续代码开发时的执行顺序固定为：

```text
先更新本文档
-> 再补 SQL
-> 再改代码
-> 再补验证说明
```

不允许反过来。

---

## 17. 当前实施起点

本文档生效时，项目当前状态如下：

1. 现有 Step1 ~ Step7 AutoAgent 诊断链已可运行。
2. 手动诊断入口已可运行。
3. Prometheus、ELK、SkyWalking、PgVector 真实采集链路已打通。
4. 诊断报告与工具调用日志已可落库。

因此下一步开发从 **Phase 1：告警自动触发最小闭环** 开始，不再继续扩散手动演示逻辑。



