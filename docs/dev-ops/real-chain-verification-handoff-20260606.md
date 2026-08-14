# Real Alertmanager Incident-to-Fix Verification Handoff

更新时间：2026-06-06 18:30 左右

## 当前目标

完成两条真实链路验证，不再使用 fixture 作为证据：

1. `OrderServiceLatencyHigh`：真实 HTTP 延迟现象，应该判断为非代码修复。
2. `OrderServiceHttp5xxHigh`：真实 HTTP 5xx 现象，应该通过 Prometheus + Elasticsearch NPE 栈定位代码、生成 patch、编译测试、输出发布风险。

## 已经跑通的部分

### 真实环境

Docker 环境已确认包含：

- Prometheus: `127.0.0.1:9090`
- Alertmanager: `127.0.0.1:9093`
- Elasticsearch: `127.0.0.1:9200`
- Logstash: `ops-logstash`
- MySQL / PGVector / SkyWalking

`samples/order-service` 已升级为真实 Spring Boot 服务：

- 端口：`18081`
- 健康检查：`/actuator/health`
- 指标：`/actuator/prometheus`
- 5xx 接口：`POST /api/orders/submit`
- 延迟接口：`GET /api/orders/dependency-latency`

### 真实告警

已经确认过：

- `OrderServiceHttp5xxHigh` 能在 Prometheus firing，并进入 Alertmanager active。
- `OrderServiceLatencyHigh` 能在 Prometheus firing，并进入 Alertmanager active。

### 自动化脚本

新增：

- `docs/dev-ops/scripts/start-order-service-real.ps1`
- `docs/dev-ops/scripts/inject-order-service-incidents.ps1`
- `docs/dev-ops/scripts/run-real-alertmanager-incident-to-fix.ps1`

`run-real-alertmanager-incident-to-fix.ps1` 已修复：

- 会启动 AutoAgent full profile。
- 会启动 order-service。
- 会制造真实故障现象。
- 会等待 Prometheus / Alertmanager 告警。
- 会等待 CodeOps task 到终态后再保存 artifact。
- artifact 路径：`data/real-chain-runs/{timestamp}-{case}/`

## 已修复的坑

### 1. AutoAgent 启动脚本 Java 版本错误

问题：系统环境可能指向 Java 8，Spring Boot 3 插件需要 Java 17。

修复：

- `docs/dev-ops/scripts/start-runbook-rag-eval.ps1`
- 强制优先使用 `D:\Java\jdk17`。

### 2. Maven spring-boot:run 错跑父工程

问题：`spring-boot:run` 被作用到聚合父工程，提示找不到 main class。

修复：

- 改为先 `mvn -q -pl ops-autoagent-app -am -DskipTests package`
- 再 `java -jar ops-autoagent-app/target/ops-autoagent-app.jar --spring.profiles.active=full`

### 3. Logstash 不识别 Spring Boot ISO 日志

问题：老配置只识别 `26-05-31.16:xx` 格式，不识别 `2026-06-06T...`。

修复：

- `docs/dev-ops/logstash/logstash-ops-full.conf`
- multiline pattern 支持旧格式和 ISO 格式。
- grok 支持 Spring Boot 默认日志格式。
- date 支持 `ISO8601`。
- `sincedb_path` 改为 `/dev/null`，本地演练重启后可从头读日志。

当前 ES 已能搜到真实 NPE 栈：

- `NullPointerException`
- `OrderRepository.create(OrderRepository.java:14)`
- `OrderSubmitService.submit(OrderSubmitService.java:14)`
- `OrderController.submitHttpEndpoint(OrderController.java:43)`

### 4. ES Gateway 查错索引

问题：

- Logstash 写入：`ops-demo-service-log-2026.06.06`
- Gateway 查询：`ops-demo-service-log-auto-*`

修复：

- `ops-autoagent-app/src/main/resources/application-full.yml`
- `ops.integrations.elk.index-pattern` 改为 `ops-demo-service-log-*`

### 5. ES 时间窗口偏 8 小时

问题：ES `@timestamp` 是 UTC，Gateway 用本地时间但 range 没带时区，导致窗口查不到日志。

修复：

- `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/adapter/gateway/ops/ElkLogGateway.java`
- range 查询增加 `"time_zone": "+08:00"`。
- 日志样本截断从 800 扩到 1600，避免关键栈帧丢失。

### 6. 代码线索抽取识别不到 JSON 转义栈

问题：日志样本进入 raw JSON 后，`\r\n\tat ...` 是转义状态，正则匹配不到 Java 栈帧。

修复：

- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/evidence/IncidentEvidenceExtractor.java`
- 抽取前反转义 `\r\n`、`\n`、`\t`、`\"`。
- 修复方法名分组 bug。
- 过滤 `//127.0.0.1`、`/Exception`、`/graphql` 等非代码线索。

### 7. OpsDiagnosisSkill 没把日志样本显式送入抽取文本

修复：

- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/skill/OpsDiagnosisEngineeringSkill.java`
- `evidenceText` 增加 `logSamples(logEvidence)` 和 `rawLogData(logEvidence)`。

### 8. BugFix repairScope 被搜索命中污染

真实 5xx 链路最新一次已经跑到：

- OpsDiagnosis: SUCCESS，真实证据覆盖 1.0。
- CodeLocalization: HIGH，正确输出 CODE_FIX。
- codeHints 包含真实栈：
  - `OrderRepository.create`
  - `OrderSubmitService.submit`
  - `OrderController.submitHttp`
  - `OrderController.submitHttpEndpoint`
- BugFix LLM 被调用，但 PatchScopeGuard 拦截。

失败原因：

- CodeLocalization 输出正确。
- BugFixSkill 重新从搜索命中构造 repairScope，误把 `Collections.synchronizedList(...)` 当成 `OrderRepository.synchronizedList` 方法。
- Guard 正确拦截，提示 `HALLUCINATED_SCOPE`。

修复：

- `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/codeops/agent/skill/BugFixSkill.java`
- 高置信 CODE_FIX 场景，优先采用 CodeLocalization 的 `targetMethods/targetFiles`。
- 取第一个 LLM 定位出的业务栈方法作为 `STRICT_SINGLE_METHOD` 修复边界。
- 其余定位方法只放入 `candidateMethods`，不进入 Guard 允许修改范围。

已编译通过：

```powershell
$env:JAVA_HOME='D:\Java\jdk17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -q -DskipTests compile
```

## 当前阻塞点

在准备重跑修复后的 5xx 链路时，Codex 额度触顶，真实 LLM 重跑命令被拒绝。

尚未验证最后一处 repairScope 修复是否让链路进入：

1. LLM 生成最小 patch。
2. PatchScopeGuard 通过。
3. PatchSandbox 隔离应用。
4. Compile gate 通过。
5. TestVerification 通过或触发反思重试。
6. ReleaseRisk 输出风险。

## 额度恢复后的第一条命令

```powershell
& .\docs\dev-ops\scripts\run-real-alertmanager-incident-to-fix.ps1 -Case code-fix-5xx -SkipInstall
```

期望结果：

- `ops_evidence: SUCCESS`
- `code_localization: SUCCESS/NO_DIFF` 但 `localizationConfidence=HIGH`
- `code_repair: SUCCESS`
- `PatchScopeGuard.passed=true`
- patch 只改 `OrderRepository.create` 或 LLM 明确解释为何改 `OrderSubmitService.submit`
- `compileGate=true`
- `test_verification=SUCCESS`
- `release_risk=SUCCESS`

如果仍失败，优先检查最新 artifact：

```powershell
$run = Get-ChildItem -Path data\real-chain-runs -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content (Join-Path $run.FullName 'incident-view.json') -Raw
Get-Content (Join-Path $run.FullName 'trace.json') -Raw
```

## 注意事项

- 不要提交 `docs/dev-ops/runbook-rag.local.env`，它已被 `.gitignore` 忽略，里面有本地密钥。
- 不要提交 `data/` 和 `ops-autoagent-app/data/`。
- 当前 AutoAgent actuator health 可能因 mail health 为 DOWN，但 HTTP 接口可用，这不是链路失败。
- 真实链路必须从 Alertmanager webhook 进入，不要直接调用 CodeOps submit API 替代。
