# Changelog

## 2.0.0

- Migrated the repair control plane to LangGraph with durable checkpoints,
  approval interrupt/resume, child graphs, and SSE replay.
- Added versioned event envelopes, transactional Outbox, real Kafka consumer
  idempotency and a dead-letter path.
- Added Neo4j topology correlation, deterministic/Isolation Forest anomaly
  signals, Kubernetes server-side Runbook dry-run, and full-stack Docker
  deployment with Prometheus and SkyWalking.
- Added 52 business E2E Cases and 16 runtime safety/reliability Cases.
