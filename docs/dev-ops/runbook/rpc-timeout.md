# Dubbo RPC 调用超时排查流程

## keywords

dubbo, rpc, timeout, read timed out, connect timed out, feign, resttemplate, downstream, 504, gateway

## 适用场景

日志中出现 Dubbo/RPC/Feign/RestTemplate/WebClient 超时，SkyWalking 显示下游 span 慢或错误，网关返回 504。

## 快速判断

1. 通过 traceId 找到最慢或异常的下游 span。
2. 查看下游服务错误率、P95/P99、实例健康、线程池。
3. 查看调用方是否存在重试风暴。
4. 对比上下游故障时间线是否一致。

## 临时止血

1. 对慢依赖启用熔断和降级。
2. 降低重试次数，增加退避策略。
3. 临时扩容下游服务或摘除异常实例。
4. 限制入口流量，保护核心链路。

## 长期治理

1. 建立上下游依赖拓扑和接口级 SLO。
2. 为核心 RPC 调用设置隔离线程池。
3. 统一超时、重试、熔断、降级策略。
4. 在压测中覆盖下游慢响应和失败注入。

## 详细排查资料

RPC 超时需要同时看调用方、被调方和链路中间层。调用方日志中出现 read timed out、connect timed out、FeignException、Dubbo timeout、WebClient timeout，只能证明调用方等待超时；被调方是否真正慢，需要结合 SkyWalking 下游 span、被调服务 P95/P99、实例健康和错误日志判断。如果调用方存在重试风暴，原始下游问题会被放大成线程池、连接池和 CPU 压力，因此时间线很关键。

Prometheus 应观察调用方接口延迟、5xx、线程池、HTTP client 或 RPC client 指标，以及被调服务的 QPS、错误率、延迟、实例数。ELK 应检索 timeout、connection refused、connection reset、circuit breaker、bulkhead、fallback、retry exhausted 等关键词。SkyWalking 是最关键的数据源，应定位 trace 中最慢的 span、错误 span、peer、component 和 endpoint，判断慢点在调用方、网关、网络还是下游服务。

## 判断边界

如果 Trace 明确显示某个下游 span 持续超时，并且调用方日志同步出现 RPC 超时，可以确认下游依赖异常。若只有调用方日志超时，但 Trace 缺失，仍可输出高概率根因，同时标记 Trace 采样或链路覆盖缺口。若被调方正常而调用方线程池满，可能是调用方资源瓶颈导致请求等待，并非下游根因。若网关 504 升高但应用日志无异常，应补查网关和服务注册。

## 补证建议

补证应围绕 traceId、下游服务指标、重试次数、熔断状态、实例健康和发布变更展开。不要把“下游超时”写成泛泛结论，报告必须说明是哪个下游、哪个 endpoint、耗时多少、发生在哪个时间窗口、是否与用户接口失败对齐。处置建议应包含临时降级、熔断、降低重试、摘除异常实例和限流保护。
