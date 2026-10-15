# 本地真实告警驱动验证

本链路只面向本机开发，不声称接入公司监控。故障由 `samples/order-service` 的请求产生；Prometheus 根据采集到的指标自行触发规则，Alertmanager 才会推送 Webhook。验证脚本不会直接提交告警，也不使用 Eval fixture 作为实时证据。

## 与 Eval 的边界

- 全栈 Compose 以 `OPS_RUNTIME_MODE=production` 运行本地真实链路，强制关闭 `OPS_FIXTURE_FALLBACK` 与演示数据自动注入。
- 只有运营者配置的 `OPS_SERVICE_REPOSITORIES_JSON` 才能为告警任务选择仓库。没有映射的 Incident 停在 `REPOSITORY_UNMAPPED`，不会改查样例仓库。
- `autonomousTriage` 要求仓库调查形成定位且明确判断 `CODE_FIX`，才允许进入 Repair。`allowPatchApply` 只允许受控沙箱应用，不能单独决定“应改代码”。
- 本地全栈默认 `CODEOPS_APPLY_MODE=delivery_only`、`CODEOPS_AUTO_APPLY_ENABLED=false`；目标仓库写入仍需另外配置与授权。
- 52 条业务 Eval 保持独立；它们可用明确标记的测试 fixture，不计入本地真实告警结果。

## 启动

1. 将 `deploy/.env.full.example` 复制为未跟踪的 `deploy/.env.full.local`，填写本地基础设施密码。将模型凭据设置在启动 Compose 的进程环境中；不要写入本文件、脚本或提交记录。
2. 在项目根目录执行：

   ```powershell
   docker compose --env-file deploy/.env.full.local -f deploy/docker-compose.full.yml up -d --build
   ```

3. 在另一个终端启动样例服务：

   ```powershell
   .\docs\dev-ops\scripts\start-order-service-real.ps1
   ```

   样例服务使用 Java，是被监控的故障目标；Agent 主体仍是 Python/LangGraph。样例日志写到 `data/log/log_error.log`，本地 Logstash 将其送入 Elasticsearch。若需要 SkyWalking Span，可传入本地 Java Agent 的 `-SkyWalkingAgentPath`。

4. 分别运行两个场景，先等待前一个告警恢复及去重窗口结束：

   ```powershell
   .\docs\dev-ops\scripts\verify-local-alert-chain.ps1 -Case no-code-latency
   .\docs\dev-ops\scripts\verify-local-alert-chain.ps1 -Case code-fix-5xx
   ```

验证脚本依次检查 Prometheus firing、Alertmanager active、新生成的 CodeOps Task，以及至少两个可达的真实观测源；保存 `result.json`、告警、任务视图、Trace 和可观测报告到被忽略的 `data/local-alert-chain/`。没有观察到新告警、新任务或足够的实时证据时，脚本以错误退出，不报告通过。

## 结果解释

- `REPOSITORY_UNMAPPED`：服务仓库没有在运营者配置中登记。检查 `OPS_SERVICE_REPOSITORIES_JSON`；不要让告警标签任意指定本地目录。
- `NO_CODE_FIX`：证据指向运行时、容量或依赖处置；不应出现生产补丁。
- `WAITING_APPROVAL`：已有可审核产物，任务暂停；只有人工决策后才恢复。
- `REVIEW_UNAVAILABLE`、`REPAIR_STOPPED` 或证据不足：保留失败原因并人工接管，不应算作真实链路成功。
- `realEvidenceCoverage` 代表数据源可达率，不等于根因判断准确率；真实结果仍要人工核对日志/Trace 与补丁行为。

本链路证明本机监控自动触发与执行边界。进入公司环境之前，还需要接入公司身份和权限系统、CMDB 服务归属、值班通知、历史事故回放及灰度验证。

## Incident 进入 Agent 后的实际执行边界

- Webhook 先 Redis 按实例去重，重复告警结束；有效告警才入库为待聚合。后台每 60 秒扫描一次待聚合记录，按环境、服务、接口、告警类型分组，无需给每组维护等待时间。本轮仅标记扫描取到的记录，新记录留到下一轮；每组的 Incident、Outbox 和原告警状态在同一事务提交。
- 聚合周期由 `OPS_INCIDENT_AGGREGATION_INTERVAL_SECONDS` 配置（默认 60）；旧 `OPS_INCIDENT_AGGREGATION_WINDOW_SECONDS` 仅作兼容后备。入口短租约尚未完成时返回可重试的 503，只有完成入库的告警才确认长去重周期。
- 调度器使用 `asyncio.PriorityQueue` 和 3 个 worker（可配置），同一服务默认仅 1 个在运行；P1/P2/P3 分别优先于后续级别。同服务等待项不会占用其他服务可运行的 worker。此限流在单个 Python 进程内生效，多副本部署仍需共享分布式并发锁。
- Alertmanager 只传告警事实，不传 Prometheus 时间序列。`ops_diagnosis` 以事故首条告警前 10 分钟到任务启动时刻为窗口，重新查询 Prometheus、Elasticsearch 和 SkyWalking；三个只读请求并发执行，缺失来源应如实标记不可用，不能替换为 Eval fixture。旧记录的无时区时间按本机本地时间解释后转换成 UTC。
- Elasticsearch 查询仅取该时间窗内、匹配服务与错误条件的最新 10 条样本，不代表窗口内全部日志；SkyWalking 通过 HTTP GraphQL 取指定 Trace 或分页抽样的慢/错 Trace，默认每类最多 10 条。
- 当前实现是一个 `CodeOpsGraph` 父图串联故障调查、受控修复、验证和发布风险/独立审查职责，领域 `StateGraph` 子图负责各自合同和 artifact 边界；**不是三个可独立运行的顶层 Agent 图**。父图持有预算、重试、审批及唯一目标仓库写入节点 `apply_approved_patch`，不能为了拆图而改变已暂停线程的节点名。
- 本地默认 `CODEOPS_APPLY_MODE=delivery_only`，测试执行开关默认关闭。生成了测试计划或完成沙箱验证，不等于真实 Maven 编译和测试已通过。
