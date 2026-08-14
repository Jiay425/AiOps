# 支付回调幂等与订单状态不一致排查手册

## keywords

payment callback, idempotency, duplicate notify, order status, transaction, distributed lock, optimistic lock, callback retry, pay success, refund risk

## 适用场景

支付渠道、清结算平台或消息队列重复投递回调后，订单服务出现重复状态流转、重复发货、重复积分、重复优惠券核销，或者支付成功但订单仍停留在待支付状态。告警常见表现包括支付回调接口 5xx 升高、重复 requestId、订单状态冲突、唯一键冲突、库存或权益重复扣减。

该类问题不能简单归因于“支付渠道重复通知”，因为支付回调天然至少一次投递。系统必须能够承受重复回调、乱序回调和延迟回调。诊断时要区分三类根因：接口缺少幂等保护、状态机流转条件错误、事务边界覆盖不足。

## 关键证据

Prometheus 中优先查看 payment_callback_total、payment_callback_5xx_rate、payment_callback_retry_total、order_status_conflict_total、db_transaction_rollback_total。若回调量和重试量同时升高，需要判断是外部渠道重试导致流量放大，还是本服务异常导致渠道重试。

Elasticsearch 中优先检索 duplicate callback、already paid、order status conflict、OptimisticLockException、DuplicateKeyException、payment notify retry、transaction rollback。若日志包含同一个 paymentNo 或 tradeNo 多次进入核心状态更新逻辑，说明幂等保护位置过晚。

SkyWalking 中重点看支付回调 trace 是否重复执行扣库存、发货、发券、积分入账、消息发送等副作用 span。若数据库更新成功后消息发送失败导致接口返回 5xx，渠道会继续重试，这类问题通常需要 outbox 或事务消息治理。

## 精确定位片段：幂等保护位置

如果日志显示同一个 paymentNo 多次进入 `markPaid`、`grantBenefit`、`sendDeliveryMessage`，说明幂等校验可能放在副作用之后。正确结构应该是在进入业务副作用之前完成幂等占位，例如先插入 `payment_callback_record(payment_no, channel, status=PROCESSING)`，利用唯一键保证只有一个线程获得处理权。

危险写法是先查询再插入：`if not exists then insert`。在并发回调下两个线程都可能查到不存在，随后同时进入业务逻辑。更可靠的方式是用唯一键插入、数据库原子更新或 Redis setnx 加短 TTL。若使用 Redis 锁，仍要以数据库唯一约束兜底，防止锁过期、实例重启或网络分区。

## 精确定位片段：状态机条件

订单状态流转必须是条件更新，而不是无条件覆盖。支付成功只能从 `WAIT_PAY` 或允许的中间态流转到 `PAID`。SQL 应类似：

```sql
UPDATE order_main
SET status = 'PAID', paid_time = ?
WHERE order_id = ? AND status = 'WAIT_PAY'
```

如果 update affected rows 为 0，需要读取当前状态判断是重复回调、超时关闭、已退款还是非法乱序。不能直接抛 500，否则支付渠道会重试并扩大告警。重复成功回调应该返回成功响应，但记录为 idempotent replay。

## 精确定位片段：事务边界

支付回调通常包含订单状态更新、支付流水更新、权益发放、消息发送。数据库事务只能覆盖本地数据库操作，不能直接覆盖 MQ 发送和 RPC 调用。若事务提交后 MQ 发送失败，会出现订单已支付但下游未感知；若 MQ 发送成功后事务回滚，会出现下游收到错误事件。

推荐使用 outbox 表：本地事务内写订单状态和待发送事件，事务外由可靠投递任务发送 MQ。回调接口只保证本地事实落库，不把外部副作用放在请求线程的强一致路径里。

## 临时止血

如果重复发货或重复权益风险已经出现，先按 paymentNo、orderId、channelTradeNo 维度冻结重复处理路径。可以临时关闭非核心副作用任务，例如积分、优惠券、通知消息，保留支付状态落库。对渠道回调接口要返回成功给已处理订单，避免渠道持续重试。

对于正在扩散的重复处理，先加数据库唯一约束或临时幂等表，优先保证“不重复副作用”。如果状态已经错乱，需要通过审计流水回放修正，不能直接批量改订单状态。

## 长期治理

支付回调必须建立三层保护：请求幂等、状态机幂等、副作用幂等。每个副作用都应有业务唯一键，例如 shipmentNo、couponGrantNo、pointFlowNo。即使上游重复调用，下游也应通过唯一键保证重复请求返回同一结果。

需要建立回调重试率、幂等命中率、状态冲突率、事务回滚率、outbox 堆积量监控。回归测试必须覆盖重复回调、并发回调、乱序回调、事务失败、消息发送失败、渠道超时重试。

## 误判边界

只有支付渠道重复通知不能说明系统异常；重复通知是正常机制。只有当重复通知导致副作用重复、状态冲突、接口 5xx 或人工对账差异，才说明服务幂等或事务边界存在缺陷。

如果回调接口返回 200 且幂等命中率升高，但没有状态冲突和副作用重复，通常不需要修代码，可能只需要扩容或渠道侧限流。如果日志只有 DuplicateKeyException 但业务返回成功，也可能是正确的唯一键幂等兜底。
