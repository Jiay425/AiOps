# 缓存雪崩、击穿与热点 Key 排查手册

## keywords

cache avalanche, hot key, redis timeout, cache penetration, cache breakdown, big key, ttl, local cache, bloom filter

## 适用场景

业务接口突然变慢，Redis QPS、CPU、网络流量、连接数或超时升高，数据库查询量同步升高。日志可能出现 RedisCommandTimeoutException、hot key、big key、cache miss storm、database fallback spike。

缓存问题要区分雪崩、击穿、穿透和热点 key。雪崩是大量 key 同时失效，击穿是单个热点 key 失效导致并发打到数据库，穿透是不存在的数据持续绕过缓存，热点 key 是单 key 流量过大导致 Redis 分片或网络瓶颈。

## 精确定位片段：大量 key 同时过期

如果 cache miss rate、DB QPS 和 Redis expired_keys 在同一窗口升高，且 key TTL 分布集中，优先判断缓存雪崩。常见原因是批量预热时设置了相同 TTL，或发布后清空缓存。

修复方案是 TTL 加随机抖动、分批预热、热点 key 永不过期加异步刷新。不要简单把 TTL 调大，因为这可能带来数据陈旧风险。核心价格、库存、权限类缓存必须明确一致性策略。

## 精确定位片段：热点 key 击穿

如果 Redis 慢日志和业务日志都集中在单个 skuId、couponId、activityId 或 configKey，且该 key 失效时 DB 查询峰值出现，说明可能是热点击穿。修复通常需要 singleflight、本地锁、互斥重建或逻辑过期。

互斥锁必须设置短 TTL，并且未拿到锁的请求应返回旧值或等待短时间，不能全部阻塞到数据库。逻辑过期适合允许短暂陈旧的数据，例如商品详情、活动配置，不适合余额和库存强一致写路径。

## 精确定位片段：缓存穿透

如果大量请求查询不存在的 id，缓存 miss 之后数据库也返回空，且请求参数高度离散，可能是缓存穿透。修复方式包括缓存空值、布隆过滤器、参数合法性校验和风险限流。

缓存空值要设置较短 TTL，避免真实数据创建后长期读不到。布隆过滤器要考虑重建、误判和多租户隔离。参数明显异常时应在网关或业务入口提前拒绝。

## 临时止血

热点 key 可临时开启本地缓存、手动预热、限流或降级展示。缓存雪崩可先暂停批量失效任务，分批恢复热点数据。Redis 超时严重时，要保护数据库，宁可返回降级数据也不要让所有 miss 打到 DB。

## 长期治理

为核心缓存建立 key 画像：QPS、value size、TTL、miss rate、重建耗时、依赖 DB 查询。缓存 SDK 应提供 singleflight、TTL jitter、空值缓存、热点探测、本地缓存和降级模板。

## 误判边界

Redis timeout 不一定是 Redis 根因，也可能是下游数据库慢导致缓存重建线程堆积，或应用线程池耗尽导致 Redis 响应无法及时消费。必须同时看 Redis 指标、应用线程、DB QPS 和 trace 依赖链。
