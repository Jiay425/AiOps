---
skillId: redis-timeout
name: Redis Timeout
category: redis
matchedAlertRules: redis, timeout, jedis, lettuce, command timeout, connection timeout, hot key
symptoms: Redis span latency rises, timeout logs appear, cache-dependent endpoints slow down
recommendedTools: query_skywalking_trace, query_elasticsearch, query_runbook
keyMetrics: redis_command_latency, redis_connected_clients, redis_ops_per_sec, redis_memory_used
logPatterns: RedisCommandTimeoutException, jedis, lettuce, read timed out, connect timed out
tracePatterns: redis, cache, command, timeout
rootCauseRules: Redis timeout logs plus Redis trace span supports Redis dependency timeout; hot key or memory pressure needs Redis-side metrics
temporaryFixes: enable fallback cache, limit hot traffic, increase timeout only after capacity check
longTermFixes: monitor command latency and hot keys, isolate cache pools, add circuit breaker and bulkhead
runbookPath: docs/dev-ops/runbook/redis-timeout.md
---

# Redis Timeout

Structured skill metadata for Redis timeout incidents.
