---
skillId: jvm-full-gc
name: JVM Full GC Pressure
category: jvm
matchedAlertRules: full gc, gc overhead, outofmemoryerror, heap, metaspace, jvm_memory, stop the world, memory
symptoms: latency rises with GC pause, heap usage grows, OOM or GC overhead appears in logs
recommendedTools: query_prometheus, query_elasticsearch, query_runbook
keyMetrics: jvm_gc_pause_seconds, jvm_memory_used_bytes, process_cpu_usage, jvm_threads_live
logPatterns: Full GC, OutOfMemoryError, GC overhead limit exceeded, heap, metaspace
tracePatterns: slow endpoint, long response time, application span latency
rootCauseRules: GC metrics and OOM logs can confirm JVM pressure; leak or allocation source needs heap dump or allocation evidence
temporaryFixes: reduce traffic, scale service instances, preserve heap dump before restart
longTermFixes: monitor heap old gen GC pause allocation rate, add memory leak analysis to release checks, cap batch memory usage
runbookPath: docs/dev-ops/runbook/jvm-full-gc.md
---

# JVM Full GC Pressure

Structured skill metadata for JVM memory and Full GC incidents.
