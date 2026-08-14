# 消息队列堆积处理流程

## keywords

mq, rocketmq, kafka, rabbitmq, backlog, consumer lag, message accumulation, retry, dead letter

## 适用场景

消息消费延迟升高，Kafka consumer lag、RocketMQ 堆积量、RabbitMQ ready/unacked 上升，业务异步流程延迟。

## 快速判断

1. 查看 topic/consumer group 的堆积量和消费速率。
2. 查看消费者错误日志和重试次数。
3. 查看是否有单条毒消息导致持续失败。
4. 查看消费者线程池、数据库、下游依赖是否瓶颈。

## 临时止血

1. 扩容消费者实例或提高消费线程数。
2. 隔离毒消息，转入死信队列或人工补偿。
3. 暂停非核心生产者流量。
4. 如果下游瓶颈明显，先保护下游再恢复消费。

## 长期治理

1. 为消费延迟、堆积量、失败率建立告警。
2. 消费逻辑保证幂等和可重试。
3. 对大消息、慢消费、毒消息建立治理规则。
4. 关键异步链路增加端到端延迟监控。

## 详细排查资料

MQ 堆积的本质是生产速率持续大于消费速率，或者消费失败导致消息反复重试。诊断时要先分清是生产突增、消费者吞吐下降、单分区热点、毒消息阻塞，还是下游依赖拖慢消费。只看到 lag 增长不能直接说明消费者代码有问题，因为下游数据库、RPC、Redis、线程池饱和都会让消费端变慢。关键是把 consumer lag、consume rate、error rate、retry count、dead letter count 和消费者日志放在同一个时间窗口分析。

Prometheus 应观察 topic lag、consumer rate、consumer error、线程池 active/queue/rejected、数据库和下游依赖指标。ELK 应检索 consume failed、retry、dead letter、deserialization、duplicate key、timeout、batch consume 等日志。SkyWalking 可用于确认消费处理链路中最慢的 span，例如数据库写入、RPC 调用或 Redis 操作。对于 Kafka 还要关注 partition 分布是否倾斜；对于 RocketMQ 和 RabbitMQ 还要看 ready、unacked、retry queue 和死信队列。

## 判断边界

如果 lag 上升同时消费者错误日志集中在某类消息或某个业务异常，可以确认毒消息或消费失败方向。若 lag 上升但消费者没有错误，Trace 显示下游 DB/RPC 慢，则根因更可能是下游依赖瓶颈。若生产流量突增且消费者吞吐达到容量上限，则是容量不足，不一定是程序异常。若只有 MQ 指标异常但没有业务影响，应输出风险预警而不是故障根因。

## 补证建议

证据不足时补采 consumer group lag 明细、失败消息样本、死信队列、消费者线程池、下游 span 和生产速率。处置应避免盲目扩容：如果下游数据库已经瓶颈，扩容消费者会把压力继续打到下游。更稳的做法是先隔离毒消息、降低生产速率、保护下游，再恢复消费吞吐。
