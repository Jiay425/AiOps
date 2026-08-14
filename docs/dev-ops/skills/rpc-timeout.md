---
skillId: rpc-timeout
name: RPC or Downstream Timeout
category: downstream
matchedAlertRules: rpc, dubbo, feign, resttemplate, webclient, downstream, 504, timeout, dependency
symptoms: latency rises, downstream spans are slow or error, timeout logs appear
recommendedTools: query_skywalking_trace, query_elasticsearch, query_prometheus, query_runbook
keyMetrics: http_avg_latency_seconds, http_p95_latency_seconds, http_p99_latency_seconds, http_5xx_rate_percent
logPatterns: Read timed out, connect timed out, 504, Dubbo, Feign, RestTemplate, WebClient
tracePatterns: dubbo, feign, resttemplate, webclient, downstream, rpc, 504
rootCauseRules: timeout logs plus downstream error span supports dependency timeout; naming final downstream owner requires trace peer evidence
temporaryFixes: enable fallback, reduce retries, apply circuit breaker, limit affected traffic
longTermFixes: define dependency SLOs, monitor downstream P95 P99 and error rate, tune retry backoff and timeout budgets
runbookPath: docs/dev-ops/runbook/rpc-timeout.md
---

# RPC or Downstream Timeout

Structured skill metadata for RPC and downstream timeout incidents.
