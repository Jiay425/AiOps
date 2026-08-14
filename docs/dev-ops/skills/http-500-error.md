---
skillId: http-500-error
name: HTTP 500 Application Error
category: application
matchedAlertRules: 500, 5xx, exception, stack_trace, nullpointerexception, illegalargumentexception, classcastexception, application error
symptoms: 5xx rate increases, ELK contains exception stack traces, trace samples show application error spans
recommendedTools: query_elasticsearch, query_skywalking_trace, query_prometheus, query_runbook
keyMetrics: http_5xx_qps, http_5xx_rate_percent, http_server_requests_seconds_count
logPatterns: NullPointerException, IllegalArgumentException, ClassCastException, stack_trace, 500
tracePatterns: isError, exception, endpointName, serviceCode
rootCauseRules: exception logs plus error trace sample can support application failure mode; code defect needs stack top or deployment evidence
temporaryFixes: rollback recent release, add parameter guard, degrade non-core logic
longTermFixes: build exception TopN dashboard, standardize error handling, add unit tests for high-frequency exception types
runbookPath: docs/dev-ops/runbook/http-500-error.md
---

# HTTP 500 Application Error

Structured skill metadata for application 5xx incidents.
