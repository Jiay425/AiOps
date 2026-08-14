# 数据库连接池耗尽排查手册

## keywords

hikari, jdbc, mysql, sqltimeout, connection pool, connection is not available, database timeout, slow sql

## 适用场景

接口错误率升高，日志中出现 Hikari、JDBC、SQLTimeoutException、Connection is not available、Communications link failure，或 SkyWalking 显示 DB span 耗时异常。

## 快速判断

1. 查看 Hikari active connections 是否接近 maximumPoolSize。
2. 查看 connection acquire time 是否升高。
3. 查看 MySQL 连接数、CPU、IO、锁等待、慢 SQL。
4. 查看错误发生时间是否与接口 P95/P99、DB span 耗时升高同步。

## 临时止血

1. 对高风险接口限流或降级。
2. 暂停非核心批处理、补偿任务、报表任务。
3. 如果数据库容量允许，谨慎提高连接池上限。
4. 回滚最近引入的慢 SQL 或高频查询变更。

## 长期治理

1. 为 Hikari active、idle、pending、timeout、acquire time 建立监控。
2. 为慢 SQL、无索引 SQL、锁等待建立巡检。
3. 为核心接口设置数据库依赖超时和熔断策略。
4. 将连接池耗尽故障纳入压测基线。

## 详细排查资料

数据库连接池耗尽通常不是单一指标可以解释的问题，它往往同时包含连接获取等待、SQL 执行变慢、事务未及时释放、数据库端锁等待或连接数上限不足等多种因素。诊断时应先区分“连接池本身配置过小”和“连接被慢操作长期占用”。如果 Hikari active 长时间接近 maximumPoolSize，同时 pending 持续大于 0，并伴随 connection timeout total 增长，可以认为连接获取路径存在直接异常；如果 active 升高但 pending 不明显，则更像业务流量或短暂并发峰值。

Prometheus 中重点观察 hikari_connections_active、hikari_connections_idle、hikari_connections_pending、hikari_connections_timeout_total、http_server_requests_seconds、tomcat_threads_busy。ELK 中重点检索 Connection is not available、SQLTransientConnectionException、Communications link failure、Lock wait timeout exceeded、Deadlock found、query timeout 等错误。SkyWalking 中重点查看 DB span 的耗时分布、SQL statement、实例节点、上游接口路径和 traceId。Runbook 只能提供模式参考，最终结论必须回到证据：连接池等待和 DB span 慢是否在同一故障窗口、同一服务、同一接口上对齐。

## 误判边界

只有 5xx 升高不能直接判断为连接池耗尽；只有慢 SQL 样本也不能直接说明连接池耗尽，因为慢 SQL 可能只影响少数请求。只有当连接池 pending 或 timeout 直接出现异常，或者 DB span 长耗时占用大量连接并导致上游线程等待，才可以把连接池作为高概率根因。反过来，如果 Hikari 指标正常，但 Redis、RPC、线程池或 JVM 指标异常更强，应把连接池问题降级为伴随现象。

## 补证建议

若证据不足，应优先补采连接池指标窗口、慢 SQL TopN、DB span 样本和错误日志。补证不应无限循环：如果 Prometheus、ELK、SkyWalking 都已查询且没有连接池或 DB 异常，应输出调查完成但根因未确认，或者给出更合理的概率根因。面向 Agent 的证据摘要应保留 rawEvidence、时间窗口、指标值、阈值、服务名、接口路径和 traceId，避免只给一句“数据库异常”导致模型臆测。
