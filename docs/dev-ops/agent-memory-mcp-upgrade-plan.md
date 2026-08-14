# AutoAgent Memory and MCP Upgrade Plan

## Purpose

This document is the local source of truth for the next AutoAgent upgrades.
Every implementation round must read this plan first, update progress here, and only then modify code.

The goal is to make the project interview-ready as a real AIOps Agent system, not a toy workflow.
The upgrade must explicitly cover:

1. Short-term incident working memory.
2. Long-term historical incident memory.
3. MCP-based tool abstraction and governance.
4. Memory-aware planning, review, and reporting.
5. Evaluation harness for memory and tool-chain value.

## Non-Negotiable Rules

1. Do not add vague Agent concepts without code paths, data models, and evaluation criteria.
2. Do not replace deterministic observability collection with model guesses.
3. Agent reasoning must consume structured evidence, Runbook context, and memory records.
4. Short-term memory and long-term memory must be separated clearly.
5. MCP must be represented as a tool protocol and governance dimension, not just a dependency name.
6. Every phase must have compile verification before being called complete.
7. Every phase must update this document before moving to the next phase.
8. Do not implement later phases early unless this document is updated first.

## Current Baseline

The project already has:

1. Alertmanager webhook ingestion.
2. Incident State, Investigation Plan, Tool Call Log, Evidence Signal, Review Result, Diagnosis Report.
3. PlannerAgent, EvidenceReviewerAgent, ReportWriterAgent.
4. Prometheus, Elasticsearch, SkyWalking, PgVector Runbook collection.
5. Runbook RAG with chunk splitting, embedding retry, hybrid retrieval, parent-child context, and ablation evaluation.
6. Tool governance basics: whitelist, budget, failure downgrade, and call log.

The missing pieces are:

1. Memory concepts are not explicit enough for interview discussion.
2. Short-term memory is currently scattered across incident state and execution context.
3. Long-term incident memory is not modeled as reusable historical experience.
4. MCP is not visible enough in tool metadata and tool call logs.
5. Evaluation does not yet compare memory-enabled and memory-disabled diagnosis behavior.

## Phase M1: Short-Term Incident Working Memory

### Goal

Make short-term memory explicit.
One incident should have a single working memory object shared by PlannerAgent, EvidenceReviewerAgent, and ReportWriterAgent.

### Scope

Create or formalize an IncidentWorkingMemory model that contains:

1. Incident command and alert context.
2. Current investigation plan.
3. Tool call history.
4. Evidence signals and raw evidence summaries.
5. Runbook matches.
6. Reviewer decisions and supplemental plans.
7. Root-cause conclusion and report draft.

### Code Expectations

1. Add a domain model for working memory.
2. Add a service to build/update working memory from existing incident state tables.
3. Make Planner/Reviewer/Reporter input assembly read from this memory object.
4. Keep existing diagnosis tables; do not replace persistence unnecessarily.

### Acceptance Criteria

1. Code compiles.
2. Planner, Reviewer, and Reporter have a visible memory input path.
3. A single incident can reuse previously collected evidence during supplemental review.
4. No diagnosis step loses existing behavior.

### Implementation Checklist

Status: COMPLETED

1. Inspect current execution boundary:
   - `OpsIncidentExecuteStrategy`
   - `DefaultOpsAgentExecuteStrategyFactory.DynamicContext`
   - `AbstractOpsAgentExecuteSupport`
   - `OpsAgentBootstrapService`
   - `OpsAgentPlannerService`
   - `OpsEvidenceReviewerService`
   - report generation node / report writer service
2. Add `OpsIncidentWorkingMemory` domain model:
   - incident identity: diagnosisId, sessionId, serviceName, alertName, severity, time window
   - planner snapshot: plan id/json, hypotheses, required tools, expected evidence
   - evidence snapshot: metric/log/trace/runbook summaries, tool call history, missing evidence
   - reviewer snapshot: last review status, sufficiency judgment, required tools
   - report snapshot: final status/root cause/report summary
3. Add `OpsIncidentWorkingMemoryService`:
   - initialize memory from incoming incident command and current plan
   - update memory after evidence collection/review/report stages
   - serialize compact memory JSON for agent prompts
4. Wire memory into `DynamicContext`:
   - context owns one mutable working memory for the full chain
   - each node reads/writes that same object
5. Expose memory to agent prompts:
   - Planner receives current incident memory snapshot when available
   - EvidenceReviewerAgent receives compact memory plus evidence snapshot
   - ReportWriterAgent receives final memory summary
6. Acceptance command:
   - `mvn -q -DskipTests compile`

### Status

COMPLETED.

### Completed Files

1. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/memory/OpsIncidentWorkingMemory.java`
2. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/memory/OpsIncidentWorkingMemoryService.java`
3. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/service/execute/DefaultOpsAgentExecuteStrategyFactory.java`
4. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/service/OpsIncidentExecuteStrategy.java`
5. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/plan/OpsAgentPlannerService.java`
6. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/review/OpsEvidenceReviewerService.java`
7. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/service/execute/OpsStep5EvidenceCorrelationNode.java`
8. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/service/execute/OpsStep7ReportGenerationNode.java`
9. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/chat/OpsChatAgentInput.java`
10. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/chat/OpsChatAgentPrompts.java`

### Verification

`mvn -q -DskipTests compile` passed.

## Phase M2: Long-Term Historical Incident Memory

### Goal

Make historical incidents reusable as long-term memory.
New alerts should be able to retrieve similar historical incidents and previous remediation experience.

### Scope

Create HistoricalIncidentMemory records containing:

1. Service name and alert type.
2. Symptom summary.
3. Evidence summary.
4. Final root cause.
5. Remediation summary.
6. Confidence and reviewer status.
7. Similarity query text for retrieval.

### Code Expectations

1. Add a historical memory entity/model.
2. Persist compact historical summaries after diagnosis completion.
3. Add a gateway/service to retrieve similar historical incidents.
4. Feed retrieved historical incidents into Planner and EvidenceReviewer prompts as long-term memory.

### Acceptance Criteria

1. Historical memory is created after a successful diagnosis.
2. New incidents can retrieve similar historical incidents by service, alert type, or evidence terms.
3. Agent prompts distinguish Runbook knowledge from historical incident memory.
4. Compile verification passes.

### Implementation Checklist

Status: COMPLETED

1. Add `HistoricalIncidentMemoryEntity` as a compact long-term memory card:
   - diagnosis id, service name, alert rule, severity
   - symptom summary, evidence summary, final root cause, remediation summary
   - review status, confidence, tags, similarity text
2. Add repository boundary for historical memory:
   - save memory after successful diagnosis
   - query similar memories by service / alert / evidence text
3. Add infrastructure persistence:
   - PO, DAO, mapper XML and SQL DDL
   - keep `ops_incident_diagnosis` as original archive, do not overload it
4. Feed historical memory into Planner and EvidenceReviewer prompt inputs:
   - separate from Runbook RAG
   - mark as historical cases, not current direct evidence
5. Acceptance command:
   - `mvn -q -DskipTests compile`

### Status

COMPLETED.

### Completed Files

1. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/model/entity/HistoricalIncidentMemoryEntity.java`
2. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/adapter/repository/IOpsIncidentMemoryRepository.java`
3. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/memory/OpsHistoricalIncidentMemoryService.java`
4. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/dao/po/OpsHistoricalIncidentMemory.java`
5. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/dao/IOpsHistoricalIncidentMemoryDao.java`
6. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/adapter/repository/OpsIncidentMemoryRepository.java`
7. `ops-autoagent-app/src/main/resources/mybatis/mapper/ops_historical_incident_memory_mapper.xml`
8. `docs/dev-ops/mysql/sql/ops_historical_incident_memory.sql`
9. Planner, Evidence Reviewer and Report Writer prompt inputs now include `historicalMemoryJson`.

### Verification

`mvn -q -DskipTests compile` passed.

## Phase T1: MCP Tool Protocol and Governance

### Goal

Make MCP a first-class tool protocol in the operations tool layer.
The project should clearly explain which tools are HTTP, LOCAL, or MCP.

### Scope

Introduce tool protocol metadata:

1. PROMETHEUS_HTTP
2. ELASTICSEARCH_MCP
3. SKYWALKING_HTTP
4. RUNBOOK_RAG
5. LOCAL_MOCK or INTERNAL

### Code Expectations

1. Add tool protocol/type fields to tool descriptors or tool call logs.
2. Mark Elasticsearch log search as MCP-capable where applicable.
3. Tool governance should check protocol, whitelist, budget, timeout, and fallback.
4. Tool call logs should record protocol and governance decision.

### Acceptance Criteria

1. Tool logs can show whether a call is MCP or HTTP.
2. Governance decisions are traceable.
3. Existing Prometheus/ELK/SkyWalking/RAG calls still work.
4. Compile verification passes.

### Implementation Checklist

Status: COMPLETED

1. Add explicit protocol metadata for ops tools:
   - `PROMETHEUS_HTTP`
   - `ELASTICSEARCH_HTTP`
   - `ELASTICSEARCH_MCP`
   - `SKYWALKING_HTTP`
   - `RUNBOOK_RAG`
   - `LLM_CHAT_AGENT`
2. Extend tool call log domain object and persistence mapping:
   - protocol
   - logical tool name
   - governance decision
3. Enrich `saveToolCallLog` and governance denial logs with protocol/decision metadata.
4. Mark Elasticsearch MCP calls distinctly from Elasticsearch HTTP calls.
5. Update DDL for new nullable columns.
6. Acceptance command:
   - `mvn -q -DskipTests compile`

### Status

COMPLETED.

### Completed Files

1. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/governance/OpsToolProtocolResolver.java`
2. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/model/entity/OpsToolCallLogEntity.java`
3. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/dao/po/OpsToolCallLog.java`
4. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/adapter/repository/OpsGovernanceRepository.java`
5. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/adapter/gateway/ops/AbstractOpsHttpGateway.java`
6. `ops-autoagent-infrastructure/src/main/java/com/opsautoagent/infrastructure/adapter/gateway/ops/FileOpsRunbookGateway.java`
7. `ops-autoagent-app/src/main/resources/mybatis/mapper/ops_tool_call_log_mapper.xml`
8. `docs/dev-ops/mysql/sql/ops_governance_log.sql`
9. `docs/dev-ops/mysql/sql/ops_tool_call_log_protocol_upgrade.sql`

### Verification

`mvn -q -DskipTests compile` passed.

## Phase M3: Memory-Aware Agent Prompt Inputs

### Goal

Planner, Reviewer, and Report Writer should consume memory intentionally.

### Scope

Prompt inputs should include:

1. Current incident working memory.
2. Similar historical incident memory.
3. Runbook RAG matches.
4. Tool governance constraints.
5. Negative evidence and evidence gaps.

### Code Expectations

1. Update prompt input builders.
2. Add clear sections in prompt payloads: workingMemory, historicalMemory, runbookContext, toolConstraints.
3. Reviewer must not confuse historical cases with current direct evidence.

### Acceptance Criteria

1. Agent input JSON clearly separates memory types.
2. Reviewer output remains evidence-grounded.
3. Supplemental plan can cite missing current evidence instead of relying only on history.
4. Compile verification passes.

### Implementation Checklist

Status: COMPLETED

1. Add explicit `toolConstraintsJson` to `OpsChatAgentInput`.
2. Planner prompt input must include:
   - current working memory
   - historical incident memory
   - supported tool list and max tool budget
3. Evidence Reviewer prompt input must include:
   - current evidence snapshot
   - historical memories separated from current evidence
   - tool constraints for supplemental collection
4. Report Writer prompt input must include:
   - working memory and historical memories
   - no-tool-call constraint
5. Prompt definitions must list memory sections as required inputs.
6. Acceptance command:
   - `mvn -q -DskipTests compile`

### Status

COMPLETED.

### Completed Files

1. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/chat/OpsChatAgentInput.java`
2. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/chat/OpsChatAgentPrompts.java`
3. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/plan/OpsAgentPlannerService.java`
4. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/review/OpsEvidenceReviewerService.java`
5. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/service/execute/OpsStep7ReportGenerationNode.java`

### Verification

`mvn -q -DskipTests compile` passed.

## Phase E1: Memory and Tool-Chain Evaluation Harness

### Goal

Add evaluation modes that prove memory and MCP/tool governance are useful.

### Scope

Add evaluation comparison modes:

1. WITHOUT_HISTORY_MEMORY
2. WITH_HISTORY_MEMORY
3. KEYWORD_ONLY_RAG
4. HYBRID_RAG
5. TOOL_GOVERNANCE_ENABLED

### Metrics

1. Root-cause hit rate.
2. Evidence coverage.
3. Supplemental-plan reasonableness.
4. Historical memory hit rate.
5. Tool coverage.
6. Tool-call budget compliance.
7. Diagnosis latency.

### Acceptance Criteria

1. Evaluation endpoint returns comparison summaries.
2. Memory-enabled mode shows measurable benefit or at least explains no benefit.
3. Metrics are persisted using existing evaluation repository.
4. Compile verification passes.

### Implementation Checklist

Status: COMPLETED

1. Add evaluation comparison mode result object for:
   - `WITH_HISTORY_MEMORY`
   - `WITHOUT_HISTORY_MEMORY`
   - `TOOL_GOVERNANCE_ENABLED`
   - existing Runbook RAG modes
2. Extend evaluation service summary with:
   - historical memory hit count/rate
   - tool call count from state and from tool-call-log where available
   - governance decision coverage
   - memory enabled flag
3. Add controller endpoint for memory/tool-chain evaluation summary.
4. Keep evaluation runnable without external services by summarizing existing persisted data when live diagnosis cannot run.
5. Acceptance command:
   - `mvn -q -DskipTests compile`

### Status

COMPLETED.

### Completed Files

1. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/eval/OpsMemoryToolchainEvaluationSummary.java`
2. `ops-autoagent-domain/src/main/java/com/opsautoagent/domain/ops/agent/eval/OpsEvaluationService.java`
3. `ops-autoagent-trigger/src/main/java/com/opsautoagent/trigger/http/OpsEvaluationController.java`

### Verification

`mvn -q -DskipTests compile` passed.

## Final Status

All planned phases are completed:

1. Phase M1: COMPLETED
2. Phase M2: COMPLETED
3. Phase T1: COMPLETED
4. Phase M3: COMPLETED
5. Phase E1: COMPLETED

## Execution Order

Strict order:

1. Phase M1
2. Phase M2
3. Phase T1
4. Phase M3
5. Phase E1

Do not skip directly to later phases.

## Next Required Action

Start Phase M1 only.

Before implementing Phase M1:

1. Re-read this document.
2. Inspect current incident state, dynamic context, agent prompt builders, and repository models.
3. Write a small Phase M1 implementation checklist into this document.
4. Then modify code.


