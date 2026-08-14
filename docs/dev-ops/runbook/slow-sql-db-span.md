# 慢 SQL 与数据库 Span 异常排查手册

## keywords

slow sql, slow query, mysql lock, db span, jdbc latency, table scan, missing index, innodb lock wait, database latency

## 适用场景

接口 P95/P99 延迟升高，SkyWalking 中 DB span 耗时明显高于业务 span，ELK 日志出现 slow query、lock wait、SQLTimeoutException、Deadlock found 等信息。

## 证据语义

强根因信号：DB span 与接口慢请求时间对齐，慢 SQL 指向同一表或同一查询模式，日志存在锁等待、无索引扫描或 SQL 超时。

弱信号：只有接口延迟升高，但没有 DB span 或 SQL 样本，只能说明存在慢请求现象，不能直接确认慢 SQL。

反证信号：DB span 正常、SQL 日志无异常、下游 RPC 或 JVM 指标异常更强时，慢 SQL 只能作为低优先级假设。

## 快速判断

1. 聚合故障窗口内的慢接口、慢 SQL、DB span 耗时。
2. 查看慢 SQL 是否集中在单表、单接口、单业务参数。
3. 检查 MySQL CPU、IO、锁等待、连接数和执行计划。
4. 对比发布记录，确认是否引入新 SQL、索引缺失或查询条件变化。

## 临时止血

1. 对慢接口限流，关闭高成本查询入口。
2. 对报表、批量补偿等非核心任务降级或暂停。
3. 如果存在明显缺失索引，按变更流程补索引。
4. 必要时回滚引入慢 SQL 的版本。

## 长期治理

1. 建立慢 SQL TopN、DB span P95/P99、锁等待监控。
2. 核心 SQL 上线前进行 explain 和压测。
3. 对大表查询引入分页、缓存、只读库或异步化。
4. 将慢 SQL 与 TraceId 关联，方便从接口追到具体 SQL。

## 详细排查资料

慢 SQL 需要用调用链证据确认，而不是只凭“接口慢”。典型证据包括 SkyWalking DB span duration 升高、SQL statement 集中、MySQL slow query log 命中、Rows_examined 过大、索引失效、锁等待、CPU/IO 飙升。若接口 P95 升高但 DB span 正常，应优先排查 RPC、Redis、线程池或 JVM。若 DB span 慢但只占少量请求，也可能是局部参数问题，不应扩展为全局数据库故障。

Prometheus 应观察 HTTP 延迟、DB 连接池、MySQL CPU/IO、连接数、锁等待和慢查询数量。ELK 应检索 SQL timeout、lock wait timeout、deadlock、query timeout、too many connections、JDBC exception。SkyWalking 应定位慢 span 的 endpoint、SQL 摘要、peer、duration 和 traceId。对于 MyBatis 或 JPA 项目，还应结合 Mapper 方法、参数规模和最近发布变更。

## 判断边界

如果慢接口和慢 DB span 时间对齐，SQL 模板集中，并且连接池 pending 或数据库端慢查询同步异常，可以确认慢 SQL 或数据库执行瓶颈。若只看到 Hikari active 高，但 DB span 不慢，可能是连接池配置或连接泄漏。若 SQL 慢发生在少数报表任务，而用户接口 5xx 来自业务异常，则慢 SQL 只是背景噪声。Agent 输出根因时必须说明 SQL/表/接口/时间窗口，不能只写“数据库异常”。

## 补证建议

补证优先级为慢 SQL TopN、执行计划、锁等待、DB span 样本、Mapper 方法和发布记录。若无法获取 SQL 明文，可以使用 SQL hash、traceId 和接口路径做关联。修复建议要区分临时止血和长期治理：短期限流、暂停报表、回滚；长期补索引、分页、缓存、读写分离、SQL 审核和压测准入。
