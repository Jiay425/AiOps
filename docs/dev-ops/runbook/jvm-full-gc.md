# JVM Full GC 排查流程

## keywords

full gc, gc overhead, outofmemoryerror, heap, metaspace, jvm_memory, stop the world

## 适用场景

接口响应时间突然升高，JVM 内存持续上涨，日志出现 Full GC、OutOfMemoryError、GC overhead limit exceeded。

## 快速判断

1. 查看 heap、old gen、metaspace 使用率。
2. 查看 Full GC 次数、耗时、发生时间点。
3. 查看故障窗口内 QPS 和对象分配速率是否异常。
4. 保留 heap dump、GC 日志、线程栈。

## 临时止血

1. 降低入口流量或扩容实例。
2. 重启异常实例前先保留现场。
3. 暂停可能制造大量对象的批处理任务。
4. 对非核心功能降级。

## 长期治理

1. 建立 JVM heap、old gen、GC pause、allocation rate 告警。
2. 定期分析大对象、缓存泄漏、集合无限增长问题。
3. 对批量任务设置分页和内存上限。
4. 压测中加入长稳和内存泄漏观察。

## 详细排查资料

Full GC 类故障的关键不是看到一次 GC，而是确认 GC 对业务请求产生了可观测影响。强证据通常包括 old gen 使用率持续升高、Full GC 次数和耗时在故障窗口内突增、接口 P95/P99 同步升高、线程栈出现 STW 期间阻塞，或者日志中出现 OutOfMemoryError、GC overhead limit exceeded。仅有堆内存高水位并不一定是故障，因为某些服务缓存设计会长期占用较高堆空间但没有频繁 GC。

Prometheus 应采集 jvm_memory_used_bytes、jvm_memory_max_bytes、jvm_gc_pause_seconds_count、jvm_gc_pause_seconds_sum、process_cpu_usage、http_server_requests_seconds。ELK 应检索 Full GC、OutOfMemoryError、Metaspace、Direct buffer memory、GC overhead 等关键词。SkyWalking 应观察慢请求是否集中在 GC 时间点附近，如果所有下游 span 都正常但本服务耗时突然增加，JVM 停顿就是更强的解释。

## 判断边界

如果 Full GC 与接口延迟在时间上严格对齐，并且没有下游慢 span 或数据库异常，可以把 JVM GC 判断为高概率根因。若 GC 发生在低峰、耗时很短、接口延迟未变化，则只能作为背景信号。若日志中出现 OOM，但 Prometheus 缺少内存指标，仍可输出高概率根因，同时要求补采 heap dump 或 GC 日志用于后续复盘。若同时存在数据库连接池 pending 和 Full GC，应比较先后顺序：GC 可能导致请求堆积，也可能只是流量堆积后的伴随结果。

## 补证建议

补证应围绕 heap dump、GC 日志、对象分配速率、接口延迟时间线和发布变更展开。Agent 不应机械要求 ELK、Prometheus、SkyWalking 全部异常，只要 JVM 指标和延迟证据足够特异，就可以给出 PROBABLE_ROOT_CAUSE 或 ROOT_CAUSE_CONFIRMED。报告中应写清楚“GC 停顿解释了哪些现象，哪些现象还无法解释”。
