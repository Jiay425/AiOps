# 网关与 HTTP 层 5xx 排查手册

## keywords

gateway 5xx, route 5xx, upstream reset, bad gateway, http layer, nginx 502, gateway timeout, no application error

## 适用场景

Prometheus 显示服务或入口 5xx 上升，但应用 ELK 错误日志较少，SkyWalking 无明显异常 Trace，怀疑网关、路由、连接重置或观测链路缺失。

## 证据语义

强根因信号：网关访问日志存在 502/503/504、upstream reset、connect timeout，并且应用侧没有对应请求日志或 Trace。

弱信号：只有 Prometheus 5xx 升高，没有网关日志或应用异常，只能确认 HTTP 层失败现象。

反证信号：应用 ELK 有集中异常堆栈或 Trace 显示业务异常，应优先判断应用异常。

## 快速判断

1. 对比入口网关 5xx、应用 5xx、应用错误日志和 Trace 数量。
2. 检查网关 upstream、路由规则、连接池、超时、健康检查。
3. 查看故障窗口是否发生发布、服务注册、实例摘除或网络波动。
4. 如果应用侧没有日志和 Trace，标记为观测盲区，补查网关日志与接入层指标。

## 临时止血

1. 摘除异常实例或回滚路由配置。
2. 放宽不合理的网关超时或连接池限制。
3. 临时降级非核心接口。
4. 开启网关访问日志采样，补充请求 ID 透传。

## 长期治理

1. 打通网关日志、TraceId、服务日志三方关联。
2. 为入口 5xx、upstream reset、connect timeout 建立告警。
3. 发布和服务注册变更纳入故障时间线。
4. 对无应用日志的 5xx 建立专门排障流程。

## 详细排查资料

网关 5xx 与应用 5xx 的区别非常关键。网关层可能因为 upstream connection reset、connect timeout、read timeout、路由配置错误、实例摘除、服务注册不一致、TLS 握手失败或负载均衡异常而返回 502/503/504。此时应用侧可能没有任何 ERROR 日志和 Trace，因为请求根本没有进入应用，或者进入后未被采样。诊断链路必须把入口网关、应用服务和下游依赖分层，否则会把所有 5xx 都误判为应用异常。

Prometheus 应观察网关 upstream 5xx、应用 http 5xx、实例健康、服务注册数量、连接池、超时和 QPS。ELK 应检索网关 access log、upstream reset、no healthy upstream、connect timeout、read timeout、route not found、service unavailable。SkyWalking 如果缺少应用 Trace，反而可能是证据：它说明失败发生在接入层、采样缺口或请求未进入应用。Runbook 匹配到网关 5xx 时，Agent 应优先解释“为什么应用日志/Trace 为空仍然可能有故障”。

## 判断边界

如果网关日志明确出现 no healthy upstream、connect timeout 或 502/504，且应用侧无对应请求日志，可以确认网关或服务发现方向。若应用日志中有集中异常堆栈，Trace 也显示应用错误，则根因应回到应用。若只有 Prometheus 的入口 5xx 升高，没有网关日志和应用日志，应输出概率根因并要求补查网关访问日志、服务注册和发布记录，而不是硬判具体组件。

## 补证建议

补证应围绕网关 access log、upstream 状态、服务注册中心、实例健康、发布变更、DNS/TLS/网络日志展开。处置上可以摘除异常实例、回滚路由、放宽不合理超时、恢复健康检查或临时切换流量。长期必须让网关日志携带 traceId 或 requestId，并与应用日志打通。
