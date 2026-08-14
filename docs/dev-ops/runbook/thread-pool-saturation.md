# 线程池饱和与队列堆积排查手册

## keywords

executor_active_threads, tomcat_threads_busy, thread pool, queue backlog, rejected execution, bulkhead, async executor

## 适用场景

Tomcat busy threads、业务线程池 active threads 持续接近上限，接口延迟升高，日志出现 RejectedExecutionException 或 queue backlog。

## 证据语义

强根因信号：线程池活跃线程接近最大值，队列堆积或拒绝任务同步出现，Trace 显示请求等待线程或下游调用阻塞。

弱信号：只有线程数升高，没有队列、拒绝、延迟或阻塞证据，只能作为容量压力信号。

反证信号：线程池正常但 DB/RPC/Redis 等依赖出现长耗时，应优先判断依赖阻塞导致的上游症状。

## 快速判断

1. 查看 tomcat_threads_busy、executor_active_threads、queue size 和 rejected count。
2. 分析线程栈，确认阻塞在数据库、RPC、Redis、锁竞争还是业务计算。
3. 对比 QPS、接口耗时、线程池指标是否同一时间窗口变化。
4. 检查线程池核心数、队列长度、拒绝策略和超时配置。

## 临时止血

1. 对入口流量限流，降低队列积压。
2. 暂停非核心异步任务和批处理。
3. 扩容服务实例，必要时调整线程池上限。
4. 对阻塞依赖启用超时、熔断或降级。

## 长期治理

1. 为所有关键线程池暴露 active、queue、rejected、completed 指标。
2. 将线程池容量纳入压测基线。
3. 避免无界队列和无限等待。
4. 将业务线程池按隔离域拆分，避免互相拖垮。

## 详细排查资料

线程池饱和经常是“结果”而不是“根因”。入口 Tomcat 线程满可能来自下游慢调用、数据库等待、Redis 超时、锁竞争、CPU 饱和或无界队列堆积。业务线程池 active 高也可能只是流量突增后的正常容量上限。因此诊断时不能只看到 active_threads 高就判断线程池问题，而要看 queue size、rejected count、task duration、线程栈阻塞点、接口延迟和下游 span。

Prometheus 应观察 tomcat_threads_busy、executor_active_threads、executor_queued_tasks、executor_completed_tasks、executor_rejected_tasks、http P95/P99 和 QPS。ELK 应检索 RejectedExecutionException、Task rejected、thread pool exhausted、timeout waiting for executor、bulkhead full 等错误。SkyWalking 应确认请求耗时是否消耗在本服务等待、下游 span、数据库 span 或 Redis span 上。线程 dump 是判断阻塞点最有价值的补充证据。

## 判断边界

如果线程池队列持续增长、拒绝数增加，并且异常日志出现任务拒绝，可以确认线程池容量或隔离问题。若线程池 active 高但队列和拒绝为零，可能只是高负载，不一定故障。若大量线程阻塞在 JDBC/RPC/Redis，根因应指向依赖慢，而线程池饱和只是放大效应。若 CPU 饱和且线程栈集中在计算逻辑，则线程池扩容反而可能恶化 CPU 竞争。

## 补证建议

证据不足时补采线程池 queue/rejected、线程 dump、接口级耗时、下游 span 和限流日志。处置建议要谨慎：可以临时限流、隔离线程池、降低队列长度、设置超时和熔断，但不要盲目把线程数调大。长期治理应建立线程池命名、指标暴露、容量评估、拒绝策略和压测基线。
