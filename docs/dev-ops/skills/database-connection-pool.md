---
skillId: database-connection-pool
name: Database Connection Pool Exhaustion
category: database
matchedAlertRules: hikari, jdbc, mysql, sqltimeout, connection pool, connection is not available, database timeout, slow sql, db
symptoms: 5xx rises with SQLTimeoutException, Hikari active connections near max, DB span latency increases
recommendedTools: query_prometheus, query_skywalking_trace, query_runbook
keyMetrics: hikari_connections_active, hikari_connections_max, hikari_connections_pending, hikari_connection_timeout_total
logPatterns: SQLTimeoutException, Connection is not available, Communications link failure, hikari, jdbc, mysql
tracePatterns: jdbc, mysql, database, db, sql, hikari
rootCauseRules: pool pressure plus DB error logs indicates connection acquisition timeout; pool size or connection leak still requires separate evidence
temporaryFixes: limit high-risk traffic, pause batch jobs, check DB capacity before increasing pool size
longTermFixes: monitor Hikari active idle pending timeout acquire time, inspect slow SQL and lock waits, add DB dependency timeout and circuit breaker
runbookPath: docs/dev-ops/runbook/database-connection-pool.md
---

# Database Connection Pool Exhaustion

Structured skill metadata for database connection pool incidents.
