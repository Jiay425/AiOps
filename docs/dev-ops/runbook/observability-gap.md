# 观测盲区与证据不足排查手册

## keywords

observability gap, no anomaly, missing trace, missing log, evidence insufficient, instrumentation gap, sampling, blind spot

## 适用场景

Prometheus 能看到异常现象，但 ELK、SkyWalking 或 Runbook 证据不足，无法把症状稳定映射到具体根因。

## 证据语义

强根因信号：多个关键数据源均返回 NO_ANOMALY 或 SKIPPED，同时告警现象真实存在，说明诊断链路存在观测缺口，而不是直接说明业务根因。

弱信号：单个数据源无结果，可能只是查询窗口、索引、采样率或权限问题。

反证信号：只要存在明确错误日志、异常 Trace、连接池耗尽、GC 或下游超时证据，就不应把观测盲区作为主要根因。

## 快速判断

1. 检查查询时间窗口是否覆盖告警 startsAt 和恢复时间。
2. 检查日志索引、服务名、traceId、采样率、MCP 工具权限。
3. 对比应用实际请求量与可观测数据量是否一致。
4. 如果工具已查询且无异常，报告中应明确“调查完成但根因未完全确定”或给出概率根因。

## 临时止血

1. 扩大查询窗口，补查网关、发布、服务注册和实例日志。
2. 暂时提高 Trace 采样率或错误日志采样。
3. 对核心接口增加请求 ID 和业务错误码。
4. 对诊断结论降低确定性表述，避免无证据强断言。

## 长期治理

1. 建立日志、指标、Trace 的覆盖率评测。
2. 故障复盘时记录缺失证据并补齐埋点。
3. 将观测盲区作为评测 Harness 的负向用例。
4. 对 Agent 输出增加证据充分性约束。

## 详细排查资料

观测盲区不是业务根因，但它会影响诊断可信度。典型情况包括日志索引延迟、服务名不一致、trace 采样率过低、网关未透传 traceId、Prometheus 抓取失败、时间窗口错位、MCP 工具权限不足、容器时间不同步或错误码没有结构化。Agent 在这种场景下不能简单说“没有异常”，而要区分“已查询且无异常”和“工具没有覆盖到证据”。前者是负向证据，后者是观测缺口。

排查时应检查告警 startsAt、Prometheus query start/end、ELK index 时间字段、SkyWalking query duration、服务名标签、instance 标签和 traceId 是否一致。对于跨容器环境，还要检查时区和时间同步。若 Alertmanager 告警发生在 07:09Z，而应用日志按本地时间展示 15:09，需要明确转换关系，否则容易出现“end before start”或查错窗口。

## 判断边界

如果业务指标异常但日志和 Trace 都无结果，不能直接判定根因不存在；可能是网关层失败、采样缺失、日志未打出或查询条件错误。若多个工具都正常返回 NO_ANOMALY，并且查询窗口、服务名、权限均正确，则应把这些作为负向证据，输出调查完成或概率结论，而不是无限补采。观测盲区只能作为诊断质量问题，不能替代连接池、Redis、RPC、GC 等业务根因。

## 补证建议

补证优先检查工具可用性、查询窗口、服务名映射、traceId 透传、日志索引和采样率。评测 Harness 应单独记录 evidenceCoverage、toolCoverage、negativeEvidenceHandled、needMoreEvidenceReasonable 等指标，避免模型把无结果当异常，也避免规则逻辑无限要求补证。报告中应写清楚哪些证据缺失、缺失原因和对结论置信度的影响。
