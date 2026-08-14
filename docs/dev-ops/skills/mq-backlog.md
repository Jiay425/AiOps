---
skillId: mq-backlog
name: MQ Backlog
category: mq
matchedAlertRules: mq, backlog, kafka, rocketmq, consumer lag, retry, consumer error
symptoms: consumer lag rises, processing rate falls, retry or consumer error logs increase
recommendedTools: query_prometheus, query_elasticsearch, query_runbook
keyMetrics: mq_consumer_lag, kafka_consumer_lag, rocketmq_consumer_lag, consumer_processing_qps
logPatterns: consumer error, retry, commit failed, rebalance, message deserialize
tracePatterns: mq, kafka, rocketmq, consumer, producer
rootCauseRules: backlog metric plus consumer error logs supports consumer-side failure; producer spike needs publish rate evidence
temporaryFixes: scale consumers, pause low-priority producers, isolate poison messages
longTermFixes: monitor consumer lag and retry rate, add dead-letter handling, tune batch size and concurrency
runbookPath: docs/dev-ops/runbook/mq-backlog.md
---

# MQ Backlog

Structured skill metadata for MQ backlog incidents.
