# 接口 500 故障排查流程

## keywords

500, exception, stack_trace, nullpointerexception, illegalargumentexception, classcastexception, application error

## 适用场景

服务接口出现大量 500，ELK 日志中存在异常堆栈，或用户反馈某个业务操作失败。

## 快速判断

1. 按服务名、接口路径、时间窗口检索 ERROR 日志。
2. 聚合异常类型和栈顶类名，确认是否集中在单个方法。
3. 通过 traceId 串联一次完整调用链。
4. 对比故障前后是否存在发布、配置变更、流量突增。

## 临时止血

1. 如果是新版本引入，优先回滚。
2. 如果是异常入参，增加参数校验和兜底分支。
3. 如果影响核心链路，临时降级非核心逻辑。
4. 保留异常样本、请求参数、traceId 和发布记录。

## 长期治理

1. 建立异常类型 TopN 看板。
2. 核心接口统一错误码和异常处理。
3. 对空指针、参数异常、集合越界等高频问题补充单元测试。
4. 发布系统记录版本、配置和变更人，便于故障复盘。

## 详细排查资料

HTTP 500 是典型现象指标，它说明请求在服务端失败，但不等于已经定位到根因。真正的根因可能是业务代码异常、参数兼容问题、数据库连接池耗尽、下游 RPC 超时、Redis 超时、线程池拒绝、JVM Full GC 或网关转发失败。诊断时必须把 500 指标拆成三类证据：入口层的失败比例、应用层的异常日志、调用链中的失败 span。只有把这三类证据放在同一个时间窗口里比较，才能避免把现象误写成根因。

ELK 检索时应按 serviceName、traceId、接口路径、异常类型、状态码和时间窗口组合查询，不应只搜 ERROR。常见异常包括 NullPointerException、IllegalArgumentException、BusinessException、SQLTimeoutException、RedisCommandTimeoutException、FeignException、SocketTimeoutException。SkyWalking 侧应关注异常 span 的 endpoint、component、isError、duration、peer 和 stack。Prometheus 侧应关注 http 5xx rate、P95/P99 延迟、QPS、线程池和资源指标。

## 判断边界

如果只有 Prometheus 5xx 升高，但 ELK 和 Trace 都没有异常，这并不代表无法输出结论，而是说明只能给出“HTTP 层失败现象成立，具体根因需结合网关、日志采样、错误码覆盖率判断”的概率结论。若 Runbook 命中通用 500 手册，Agent 可以输出 PROBABLE_ROOT_CAUSE，但不能强行说是数据库、Redis 或 JVM。若错误日志中异常类型高度集中，并且 Trace endpoint 与 5xx 接口一致，则可以确认应用异常或下游异常方向。

## 补证建议

证据不足时优先补采接口维度错误日志、traceId 样本、发布记录、网关访问日志和错误码分布。如果 ELK 和 SkyWalking 已查询且返回 NO_ANOMALY，它们是负向证据，不应继续重复补采同一工具；应转向网关层、日志采样配置、服务注册变更或业务错误码缺失。报告中要明确“已查但未发现”的数据源，避免面试中被追问为什么没有多源证据。
