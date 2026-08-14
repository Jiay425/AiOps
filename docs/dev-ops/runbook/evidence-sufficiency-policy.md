# 运维 Agent 证据充分性判断准则

## keywords

evidence sufficiency, direct evidence, multi source support, temporal alignment, entity alignment, negative evidence, probable root cause

## 判断原则

Agent 不应机械要求所有数据源同时异常。不同根因需要不同证据强度：

1. 特异性强的单源证据可以支持高概率根因，例如 Hikari pending/timeout、Full GC、Redis timeout、RejectedExecutionException。
2. 现象型指标只能确认症状，例如 5xx、P95 延迟、QPS 波动，需要结合日志、Trace、Runbook 或上下文解释。
3. NO_ANOMALY 不是自动补证理由。如果工具已查询且无异常，它是负向证据，应参与判断而不是无限补采。
4. 只有存在真实可补采证据时才返回 NEED_MORE_EVIDENCE，例如缺少 TraceId、缺少网关日志、缺少发布信息、缺少 DB span。
5. 如果能形成合理但非完全确定的解释，应输出 PROBABLE_ROOT_CAUSE，而不是强行确认或无限补证。

## 输出语义

ROOT_CAUSE_CONFIRMED：直接证据强、时间对齐、实体对齐，且无强反证。

PROBABLE_ROOT_CAUSE：证据能解释主要现象，但存在观测缺口或部分负向证据。

INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED：已查询关键数据源，没有足够证据支撑任何合理根因。

NEED_MORE_EVIDENCE：缺少关键且可获得的证据，必须明确 requiredTools 和补采原因。

## 证据语义解释

证据充分性不是简单地数数据源数量。一个强特异性单源证据，例如 Hikari timeout_total 增长、RejectedExecutionException、Full GC 长暂停、RedisCommandTimeoutException，可以支撑高概率根因；一个弱现象指标，例如 5xx rate、P95 延迟、QPS 波动，只能说明症状，需要结合 Runbook、上下文、负向证据和可补采证据判断。Agent 的任务是解释证据语义，而不是机械要求 Prometheus、ELK、SkyWalking 同时异常。

directEvidence 表示是否存在能直接说明某类故障的证据，例如错误日志、异常 span、连接池 pending、GC pause。multiSourceSupport 表示是否有多个独立来源互相印证，但它不是确认根因的硬条件。temporalAlignment 表示证据时间是否覆盖告警窗口。entityAlignment 表示服务名、实例、接口、traceId 是否一致。runbookSupport 表示检索到的手册是否能解释证据组合。noContradiction 表示没有更强反证。missingCriticalEvidence 只记录真实可获得且关键的缺口，不能把已查询且无异常的数据源重复列为缺失。

## 状态选择规则

ROOT_CAUSE_CONFIRMED 适合直接证据强、时间和实体对齐、没有强反证的场景。PROBABLE_ROOT_CAUSE 适合证据能解释主要现象但仍有观测缺口的场景，例如 Prometheus 5xx 异常、Runbook 命中 HTTP 5xx、ELK 和 Trace 无异常，此时可以给出 HTTP 层失败或网关/应用观测盲区的概率结论，但不能强行指向数据库。INVESTIGATION_COMPLETE_ROOT_CAUSE_UNRESOLVED 适合关键工具已查、没有任何合理候选的场景。NEED_MORE_EVIDENCE 只适合存在明确补采动作且补采后可能改变判断的场景。

## 评测关注点

评测时应分别统计根因命中率、证据覆盖率、工具覆盖率、补证合理率、负向证据处理率和多智能体链路通过率。若 Agent 在证据不足时直接确认根因，应计为臆测；若 Agent 在工具已查询且无异常时仍不断要求补采同一工具，也应计为补证不合理。优秀的运维 Agent 不是永远给出确定答案，而是在证据强时敢确认，在证据弱时给概率结论，在证据缺口真实存在时才补证。
