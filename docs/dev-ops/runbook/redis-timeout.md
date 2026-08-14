# Redis 超时处理方案

## keywords

redis, lettuce, jedis, redis timeout, command timed out, connection refused, cache

## 适用场景

日志中出现 Redis command timeout、Lettuce/Jedis 超时，接口延迟升高，缓存命中率下降或 Redis CPU/内存异常。

## 快速判断

1. 查看 Redis CPU、内存、连接数、慢查询、网络延迟。
2. 查看是否存在大 key、热 key、批量删除或 keys 命令。
3. 查看应用 Redis 连接池 active、idle、pending。
4. 查看故障窗口是否有缓存预热、批量任务或流量突增。

## 临时止血

1. 限制高频缓存查询入口。
2. 暂停批量缓存删除、全量刷新任务。
3. 对非核心缓存读取失败做降级。
4. 必要时扩容 Redis 或迁移热 key。

## 长期治理

1. 禁止线上使用 keys 等阻塞命令。
2. 建立大 key、热 key 巡检。
3. 为 Redis 超时、连接池等待、慢查询建立告警。
4. 核心缓存增加本地缓存或多级缓存保护。

## 详细排查资料

Redis 超时可能来自 Redis 服务端压力、网络抖动、客户端连接池耗尽、热 key 访问、慢命令阻塞或缓存击穿。诊断时不能只看“Redis timeout”四个字，而要判断超时发生的位置。客户端日志出现 RedisCommandTimeoutException、JedisConnectionException、Command timed out，说明调用方已经感知失败；如果 Redis slowlog 同步出现大 key、keys、hgetall、大批量删除，则服务端阻塞更可疑；如果应用连接池 active/pending 升高，则客户端资源池不足也可能是根因。

Prometheus 应关注接口延迟、Redis 客户端连接池、错误率、CPU、内存、网络和命令耗时。ELK 应检索 lettuce、jedis、redis command timeout、connection refused、read timed out、MOVED、ASK、OOM command not allowed 等日志。SkyWalking 应查看 Redis span 的 duration、peer、command、错误标记和上游接口路径。Runbook 命中 Redis 只是提供故障模式，最终仍需要 Agent 判断这些证据能否解释用户感知的 5xx 或慢请求。

## 判断边界

如果 Redis timeout 日志集中、Redis span 慢、接口延迟同步升高，可以确认 Redis 方向。若只有缓存命中率下降但没有超时、错误或慢命令，只能说明缓存效果变差。若 Redis 日志正常但数据库连接池和慢 SQL 明显异常，Redis 只能作为非主因。若 ELK 有 Redis 超时但 Prometheus 未采到 Redis 指标，也可以输出高概率根因，并说明监控缺口。

## 补证建议

证据不足时补采 Redis slowlog、客户端连接池指标、热点 key、大 key 扫描、Redis 实例 CPU 和网络延迟。若工具已经查询且无异常，不应重复补采 Redis；应转向下游数据库、RPC 或线程池。处置时优先保护核心链路：缓存读失败要有降级策略，缓存重建要限流，批量删除和预热任务要分片执行。
