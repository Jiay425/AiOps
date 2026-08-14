# CPU 饱和与请求抖动排查手册

## keywords

cpu usage, process_cpu_usage, system_cpu_usage, cpu saturation, load average, hot thread, high qps, spin loop

## 适用场景

Prometheus 显示 process_cpu_usage 或 system_cpu_usage 持续升高，同时接口延迟、Tomcat busy threads 或错误率出现抖动。

## 证据语义

强根因信号：CPU 使用率持续高位，时间上与延迟或错误率同步，线程栈显示热点方法、序列化、正则、加解密、循环或批处理任务。

弱信号：只有瞬时 CPU 峰值，且接口延迟、线程池、日志、Trace 均未异常，不能直接确认 CPU 饱和。

反证信号：CPU 正常但 Hikari、RPC、Redis、MQ 信号异常更强时，应优先判断依赖或资源池问题。

## 快速判断

1. 查看 process_cpu_usage、system_cpu_usage、load average 和接口 P95/P99。
2. 抓取热点线程，检查是否集中在业务方法、序列化、日志输出或正则计算。
3. 对比 QPS 是否突增，是否存在批处理、定时任务或异常重试。
4. 检查容器 CPU limit 是否过低或发生 CPU throttling。

## 临时止血

1. 对高成本接口限流或降级。
2. 暂停批处理、全量导出、报表任务。
3. 扩容实例或提高 CPU limit。
4. 回滚近期引入 CPU 热点的代码。

## 长期治理

1. 建立 CPU、load、throttling、热点线程巡检。
2. 对高 CPU 算法增加基准测试和压测。
3. 减少同步日志、重复序列化和无界循环。
4. 对重试机制设置退避和上限。

## 详细排查资料

CPU 饱和会造成请求排队、线程调度延迟和接口 P95/P99 升高，但 CPU 高本身不一定是根因。常见原因包括流量突增、热点代码循环、正则回溯、JSON 序列化、大对象压缩、同步日志、异常重试风暴、容器 CPU limit 过低或 throttling。诊断时要把 process CPU、system CPU、load average、容器 throttling、QPS、接口延迟和线程栈合在一起看。只有当热点线程集中在同一类业务方法或基础组件，并与故障窗口对齐时，CPU 才能作为主因。

Prometheus 应观察 process_cpu_usage、system_cpu_usage、container_cpu_cfs_throttled_seconds_total、load、HTTP 延迟、线程池指标和 GC 指标。ELK 应检索 retry、loop、rate limit、serialization、too many requests、timeout repeated 等日志。SkyWalking 可帮助确认慢请求是否没有明显下游 span，而耗时主要停留在本服务。线程 dump 或 async-profiler 火焰图是确认 CPU 热点的强证据。

## 判断边界

如果 CPU 飙升和接口延迟同步，线程栈集中在业务计算或序列化，且下游 DB/RPC/Redis 正常，可以确认 CPU 热点方向。若 CPU 高但接口延迟和错误率正常，可能只是批处理或可接受负载。若 CPU 高来自异常重试，而重试又由下游超时触发，根因应指向下游故障和重试策略，而不是单纯 CPU。若容器 throttling 明显，即使 process CPU 不满，也可能因为 limit 过低导致性能下降。

## 补证建议

证据不足时补采热点线程、火焰图、容器 throttling、接口维度延迟、发布记录和重试日志。处置上先限流、暂停批处理、关闭高成本入口或扩容；长期需要把 CPU 热点纳入压测和代码评审，避免一次发布把算法复杂度、日志量或重试放大。
