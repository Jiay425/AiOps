# AutoAgent Agentic Upgrade Baseline

## 1. Document Positioning

This document is the baseline plan for upgrading the current ops AutoAgent from a fixed diagnostic workflow into an agentic diagnostic system.

From this document onward, all Agent-related upgrades must follow these rules:

1. New Agent roles, planning logic, memory/state models, tool policies, evaluation cases, and database changes must follow this document.
2. If implementation requires changing the plan, update this document first, then update SQL, code, and verification notes.
3. Do not create a parallel diagnostic flow that bypasses the existing Step1 to Step7 AutoAgent chain.
4. Do not add vague "Agent" concepts that cannot be observed through state, tool calls, review decisions, or evaluation results.
5. The target is an ops-domain vertical Agent system, not a general chat assistant.

In short: this document is the execution baseline for the Agentic AutoAgent upgrade.

---

## 2. Current State

The project already has these capabilities:

1. Alertmanager can trigger the system through webhook.
2. Alerts can be normalized, deduplicated, dispatched, persisted, and traced.
3. The existing diagnostic chain supports metrics, logs, traces, evidence correlation, root cause analysis, Runbook retrieval, and report generation.
4. Prometheus, Elasticsearch, SkyWalking, PGVector, and MySQL have been integrated.
5. Diagnosis records, tool call logs, alert events, dispatch records, and notification records can be persisted.

The current system is best described as:

```text
Continuous observability + alert-driven intelligent diagnostic workflow
```

The next target is:

```text
Continuous observability + alert-driven agentic diagnostic system
```

---

## 3. Upgrade Goal

The upgrade must add real Agent capabilities on top of the existing engineering workflow.

The target closed loop is:

```text
Alert received
-> Alert normalized and deduplicated
-> Incident state initialized
-> Planner Agent creates investigation plan
-> Executor dispatches tools according to the plan
-> Reviewer Agent checks evidence sufficiency
-> If evidence is insufficient, Planner creates supplementary actions
-> Root Cause Agent ranks candidates
-> Report Writer Agent generates final report
-> Evaluation Harness records quality metrics
-> Notification and audit records are persisted
```

The key difference from the current workflow is:

1. The system should decide which evidence to collect based on the alert and current state.
2. The system should support multiple reasoning rounds when evidence is insufficient.
3. The system should expose Agent decisions as structured state, not only final text.
4. The system should be testable through repeatable incident cases.

---

## 4. Core Principles

1. Keep the existing alert-driven production loop.
2. Keep the existing Step1 to Step7 diagnostic capability.
3. Add Agent capabilities as planning, review, memory, and evaluation layers.
4. Every Agent decision must be structured and auditable.
5. Every tool call must be governed by allowlists, budgets, timeout rules, and audit logs.
6. RAG Runbooks should evolve into domain skills, not only plain retrieval documents.
7. Automated remediation is out of scope until diagnosis quality can be evaluated.

---

## 5. Target Architecture

### 5.1 Logical Layers

The upgraded system has seven layers:

1. Alert Ingestion Layer
   - Receives Alertmanager and future webhook events.
2. Alert Governance Layer
   - Normalizes, deduplicates, debounces, and dispatches alerts.
3. Incident State Layer
   - Maintains structured state for each diagnosis.
4. Agent Planning Layer
   - Plans investigation steps based on alert type and current evidence.
5. Tool Execution Layer
   - Executes Prometheus, Elasticsearch, SkyWalking, PGVector, and MCP tools.
6. Agent Review Layer
   - Reviews evidence sufficiency and requests supplementary actions.
7. Report, Notification, and Evaluation Layer
   - Generates reports, sends notifications, and records evaluation results.

### 5.2 Agent Roles

The system should introduce these roles in phases:

1. Planner Agent
   - Input: alert event, existing incident state, available skills, tool policy.
   - Output: structured investigation plan.
2. Metrics Agent
   - Responsible for Prometheus metrics selection and interpretation.
3. Logs Agent
   - Responsible for Elasticsearch query planning and log pattern extraction.
4. Trace Agent
   - Responsible for SkyWalking trace and span evidence extraction.
5. Runbook Agent
   - Responsible for Runbook skill matching and actionable suggestion extraction.
6. Evidence Reviewer Agent
   - Responsible for judging whether evidence is enough and whether more tools are needed.
7. Root Cause Agent
   - Responsible for ranking root cause candidates with confidence.
8. Report Writer Agent
   - Responsible for final report generation with evidence constraints.

### 5.3 Non-Goals

The upgrade must not do these things at the current stage:

1. Do not build a generic chat assistant.
2. Do not let the model execute unrestricted commands.
3. Do not add automatic rollback, scaling, restart, or release actions.
4. Do not replace deterministic alert governance with LLM decisions.
5. Do not remove existing persistence and audit records.

---

## 6. Incident State Model

Add a central state model named `OpsIncidentState`.

### 6.1 Required Fields

Recommended fields:

1. `diagnosisId`
2. `sessionId`
3. `eventId`
4. `serviceName`
5. `severity`
6. `alertRule`
7. `timeWindow`
8. `currentRound`
9. `maxRounds`
10. `planJson`
11. `metricsEvidenceJson`
12. `logEvidenceJson`
13. `traceEvidenceJson`
14. `runbookEvidenceJson`
15. `candidateRootCausesJson`
16. `missingEvidenceJson`
17. `toolHistoryJson`
18. `reviewStatus`
19. `finalReport`
20. `status`

### 6.2 State Rules

1. All Agent nodes read and write the same `OpsIncidentState`.
2. Step outputs must update state instead of only passing local variables.
3. Every reasoning round must increment `currentRound`.
4. `maxRounds` must default to 2 or 3 to avoid infinite loops.
5. State must be persisted at key checkpoints.

### 6.3 State Status

Allowed statuses:

1. `INIT`
2. `PLANNED`
3. `COLLECTING`
4. `REVIEWING`
5. `NEED_MORE_EVIDENCE`
6. `ANALYZING`
7. `REPORTING`
8. `SUCCESS`
9. `FAILED`
10. `DEGRADED`

---

## 7. Planning Model

Add a structured investigation plan named `OpsInvestigationPlan`.

### 7.1 Plan Fields

Recommended fields:

1. `planId`
2. `diagnosisId`
3. `round`
4. `alertType`
5. `hypotheses`
6. `steps`
7. `requiredTools`
8. `expectedEvidence`
9. `riskLevel`
10. `budget`

### 7.2 Step Fields

Each plan step should include:

1. `stepId`
2. `agentRole`
3. `toolName`
4. `queryIntent`
5. `inputConstraints`
6. `successCriteria`
7. `timeoutSeconds`

### 7.3 Planning Rules

1. `5xx` alerts prioritize logs and traces, then metrics.
2. Latency alerts prioritize metrics and traces, then logs.
3. DB pool alerts prioritize Hikari metrics, database spans, and database Runbooks.
4. JVM alerts prioritize JVM/GC metrics and memory-related logs.
5. Dependency timeout alerts prioritize trace spans, dependency error logs, and RPC/Redis/DB Runbooks.
6. If alert type is unknown, use a conservative baseline plan: metrics -> logs -> traces -> runbooks.

---

## 8. Evidence Review Loop

The upgraded system must support a bounded review loop:

```text
Plan
-> Execute tools
-> Review evidence
-> If insufficient and round < maxRounds, create supplementary plan
-> Otherwise generate root cause ranking and report
```

### 8.1 Review Output

The Evidence Reviewer Agent must output structured JSON:

```json
{
  "evidenceSufficient": false,
  "confidence": 0.62,
  "missingEvidence": [
    "missing slow trace sample",
    "missing error log cluster"
  ],
  "nextActions": [
    "query_skywalking_trace",
    "query_elasticsearch"
  ],
  "stopReason": ""
}
```

### 8.2 Stop Rules

Stop the loop when any rule is met:

1. Evidence is sufficient.
2. `currentRound >= maxRounds`.
3. Tool budget is exhausted.
4. Required tool is unavailable.
5. The alert is resolved and severity is below the configured threshold.

---

## 9. Tool Governance

All tools must be governed by policy.

### 9.1 Tool Policy Fields

Recommended fields:

1. `toolName`
2. `enabled`
3. `agentRole`
4. `maxCallsPerDiagnosis`
5. `timeoutSeconds`
6. `requiredSeverity`
7. `allowAutoExecute`
8. `requiresApproval`

### 9.2 Tool Rules

1. Read-only tools can run automatically.
2. Write or remediation tools must require approval.
3. Tool failures must be recorded as evidence, not swallowed silently.
4. Every tool call must be persisted in `ops_tool_call_log`.
5. Tool call summaries must be added to `OpsIncidentState.toolHistoryJson`.

### 9.3 Current Allowed Tools

The first Agentic version only allows:

1. Prometheus HTTP query
2. Elasticsearch HTTP query
3. Elasticsearch MCP search
4. SkyWalking GraphQL query
5. File Runbook search
6. PGVector Runbook search

---

## 10. Runbook Skill Upgrade

Runbooks should evolve from simple RAG documents into domain skills.

### 10.1 Skill Format

Each ops skill should include:

1. `skillId`
2. `name`
3. `matchedAlertRules`
4. `symptoms`
5. `recommendedTools`
6. `keyMetrics`
7. `logPatterns`
8. `tracePatterns`
9. `rootCauseRules`
10. `mitigationSteps`
11. `longTermFixes`

### 10.2 Initial Skills

Initial skills:

1. `database-connection-pool`
2. `http-500-error`
3. `jvm-full-gc`
4. `mq-backlog`
5. `redis-timeout`
6. `rpc-timeout`

### 10.3 Skill Rules

1. Skills can be stored as markdown first.
2. Skills can later be loaded into PGVector with structured metadata.
3. Planner Agent can use matched skills to generate tool plans.
4. Report Writer Agent must cite matched skills in the final report.

---

## 11. Evaluation Harness

Add an incident evaluation harness before claiming Agent quality improvement.

### 11.1 Goal

The harness must answer:

1. Did the Agent identify the expected root cause?
2. Did the Agent call the right tools?
3. Did the Agent avoid unsupported conclusions?
4. How long did the diagnosis take?
5. How many tool calls and model calls were used?

### 11.2 Evaluation Case Fields

Recommended fields:

1. `caseId`
2. `caseName`
3. `serviceName`
4. `alertPayloadJson`
5. `expectedRootCause`
6. `expectedEvidenceTypes`
7. `expectedTools`
8. `goldenSummary`
9. `severity`
10. `tags`

### 11.3 Metrics

Required metrics:

1. `top1RootCauseHit`
2. `top3RootCauseHit`
3. `requiredEvidenceCoverage`
4. `unsupportedConclusionCount`
5. `toolCallCount`
6. `diagnosisLatencyMs`
7. `finalStatus`

### 11.4 Initial Cases

Initial cases:

1. HTTP 5xx caused by application exception.
2. HTTP latency caused by downstream RPC timeout.
3. DB connection pool exhausted.
4. JVM Full GC causing request latency.
5. Redis timeout causing partial failures.
6. MQ backlog causing delayed processing.

---

## 12. Database Upgrade Plan

Add tables in phases.

### 12.1 Phase A Tables

1. `ops_incident_state`
   - Stores current Agent state.
2. `ops_investigation_plan`
   - Stores Planner Agent plans.
3. `ops_agent_review`
   - Stores Evidence Reviewer decisions.

### 12.2 Phase B Tables

1. `ops_agent_skill`
   - Stores structured Runbook skill metadata.
2. `ops_tool_policy`
   - Stores tool governance configuration.

### 12.3 Phase C Tables

1. `ops_eval_case`
   - Stores evaluation cases.
2. `ops_eval_run`
   - Stores evaluation execution records.
3. `ops_eval_metric`
   - Stores metric details.

---

## 13. Code Placement Rules

### 13.1 Domain Layer

Add packages:

1. `com.opsautoagent.domain.ops.agent`
2. `com.opsautoagent.domain.ops.agent.plan`
3. `com.opsautoagent.domain.ops.agent.review`
4. `com.opsautoagent.domain.ops.agent.state`
5. `com.opsautoagent.domain.ops.agent.skill`
6. `com.opsautoagent.domain.ops.agent.eval`

### 13.2 Infrastructure Layer

Add repository/DAO support for:

1. incident state
2. investigation plan
3. agent review
4. skill metadata
5. evaluation cases and runs

### 13.3 Trigger Layer

Trigger layer can add:

1. evaluation run endpoint
2. incident state query endpoint
3. diagnosis replay endpoint

Trigger layer must not contain Agent reasoning logic.

---

## 14. Implementation Phases

### Phase A: Agent State and Planner

Goal:

Upgrade from fixed execution input to stateful planned diagnosis.

Deliverables:

1. `OpsIncidentState` model.
2. `OpsInvestigationPlan` model.
3. `OpsAgentPlannerService`.
4. `ops_incident_state.sql`.
5. `ops_investigation_plan.sql`.
6. State initialization from alert dispatch.
7. Planner Agent prompt and structured output parser.
8. Plan persistence and audit logging.

Acceptance:

1. Each alert diagnosis creates an incident state record.
2. Each diagnosis creates a structured investigation plan.
3. The plan can be inspected from the database.
4. The existing Step1 to Step7 chain still runs.

### Phase A.1: Plan-Driven Executor Routing

Goal:

Make the persisted investigation plan control the actual evidence collection route.

Deliverables:

1. Load the latest `OpsInvestigationPlan` before the Step1 to Step7 chain starts.
2. Put the plan and planned tool list into the execution `DynamicContext`.
3. Add plan gates to evidence collection nodes:
   - `OpsStep2PrometheusMetricNode` -> `query_prometheus`
   - `OpsStep3ElkLogNode` -> `query_elasticsearch`
   - `OpsStep4SkyWalkingTraceNode` -> `query_skywalking_trace`
   - `OpsStep6RootCauseAnalysisNode` -> `query_runbook`
4. If a tool is not selected by the plan, skip the tool call and continue the chain.
5. Write each `EXECUTED` or `SKIPPED` tool decision into `OpsIncidentState.toolHistoryJson`.
6. Keep Step5 evidence correlation and Step7 report generation always enabled.

Acceptance:

1. `ops_investigation_plan.required_tools_json` affects actual tool execution.
2. A tool not selected by the plan is not called.
3. Each executed or skipped tool is visible in `ops_incident_state.tool_history_json`.
4. Existing diagnosis records are still generated.
5. If no plan exists, the system falls back to the legacy full Step1 to Step7 chain.

### Phase B: Evidence Review Loop

Goal:

Add bounded plan-act-observe-review behavior.

Deliverables:

1. `OpsEvidenceReviewerService`.
2. `OpsAgentReview` model.
3. `ops_agent_review.sql`.
4. Missing evidence model.
5. Supplementary plan generation.
6. Round limit and tool budget control.

Acceptance:

1. Reviewer can mark evidence sufficient or insufficient.
2. If evidence is insufficient, the system can create one supplementary plan.
3. The loop stops according to configured rules.
4. Review decisions are persisted.

### Phase C: Tool Policy and Specialist Agents

Goal:

Make tool usage explicit and governed.

Deliverables:

1. `OpsToolPolicy` model.
2. `ops_tool_policy.sql`.
3. Metrics Agent.
4. Logs Agent.
5. Trace Agent.
6. Runbook Agent.
7. Tool policy enforcement.

Acceptance:

1. Agents can only call enabled tools.
2. Tool call counts and timeouts are controlled.
3. Tool history is written back to incident state.

### Phase D: Runbook Skill System

Goal:

Upgrade Runbook RAG into structured ops skills.

Deliverables:

1. `OpsAgentSkill` model.
2. `ops_agent_skill.sql`.
3. Skill markdown template.
4. Skill metadata loader.
5. Planner uses matched skills.
6. Report cites matched skills.

Acceptance:

1. Existing Runbooks can be mapped to skills.
2. Planner can select recommended tools from skills.
3. Final report contains skill-backed suggestions.

### Phase E: Evaluation Harness

Goal:

Make Agent quality measurable.

Deliverables:

1. `OpsEvalCase` model.
2. `OpsEvalRun` model.
3. `OpsEvalMetric` model.
4. SQL for evaluation tables.
5. Evaluation runner.
6. Six initial evaluation cases.
7. Metric calculation logic.

Acceptance:

1. One command or endpoint can run evaluation cases.
2. Each run produces metrics.
3. Root cause hit rate and evidence coverage can be reported.

---

## 15. Configuration Plan

Add configuration under `ops.agent`.

Example:

```yaml
ops:
  agent:
    enabled: true
    max-rounds: 2
    max-tool-calls: 12
    planner:
      enabled: true
    reviewer:
      enabled: true
      min-confidence: 0.75
    tool-policy:
      enabled: true
    evaluation:
      enabled: true
```

Rules:

1. Agent configuration must be placed under `ops.agent`.
2. Alert and notification configuration must remain under `ops.alert` and `ops.notify`.
3. Feature flags must allow fallback to the existing stable workflow.

---

## 16. Verification Rules

Every phase must be verified by:

1. Maven compile.
2. At least one alert-driven full-chain run.
3. Database record inspection.
4. Log inspection.
5. Documentation update.

For Agent phases, also verify:

1. State record exists.
2. Plan record exists.
3. Tool history exists.
4. Review record exists when reviewer is enabled.
5. Evaluation metrics exist when harness is enabled.

---

## 17. Resume Positioning

After Phase A and Phase B, the resume can say:

```text
Designed a stateful Planner-Executor-Reviewer agent workflow for ops diagnosis. The system initializes incident memory from alert events, generates structured investigation plans, executes observability tools under policy constraints, reviews evidence sufficiency, and performs bounded supplementary evidence collection before root cause ranking and report generation.
```

After Phase E, the resume can add:

```text
Built an incident evaluation harness with replayable cases for 5xx, latency, connection pool exhaustion, Full GC, Redis timeout, and MQ backlog. The harness measures root cause hit rate, evidence coverage, unsupported conclusions, tool call count, and diagnosis latency.
```

---

## 18. Execution Order

Future Agentic upgrade work must follow this order:

```text
Update this document if needed
-> Add or update SQL
-> Add domain models
-> Add repository/DAO
-> Add service logic
-> Integrate into dispatch/execution flow
-> Compile
-> Run alert-driven verification
-> Record verification notes
```

This order is mandatory.

---

## 19. Phase A Implementation Record

### 19.1 Implemented Scope

Phase A has added the first agentic layer while keeping the existing alert-driven diagnosis chain unchanged.

Implemented items:

1. Added `ops_incident_state` SQL.
2. Added `ops_investigation_plan` SQL.
3. Added `OpsIncidentState` domain model.
4. Added `OpsInvestigationPlan` domain model.
5. Added Agent repository interface and infrastructure implementation.
6. Added MyBatis DAO and mapper files for state and plan persistence.
7. Added `OpsAgentBootstrapService` to initialize incident state from accepted alert events.
8. Added `OpsAgentPlannerService` to generate a structured rule-based investigation plan.
9. Added `OpsAgentStateService` to update state during diagnosis execution.
10. Integrated Agent bootstrap into `OpsDiagnosisDispatchService`.
11. Integrated state lifecycle updates into `OpsDiagnosisJobExecutor`.
12. Added `ops.agent` configuration in `application-full.yml`.

### 19.2 Planner Scope

The current Planner is `RULE_BASED`.

It classifies alerts into:

1. `HTTP_5XX`
2. `LATENCY`
3. `DB_POOL`
4. `JVM_GC`
5. `REDIS_TIMEOUT`
6. `MQ_BACKLOG`
7. `UNKNOWN`

The Planner outputs:

1. hypotheses
2. investigation steps
3. required tools
4. expected evidence
5. risk level
6. budget

This avoids making Phase A depend on external model availability. LLM-based planning can be added later behind the same `OpsAgentPlannerService` boundary.

### 19.3 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification still requires applying the two new SQL files to MySQL, restarting the application, and triggering one Alertmanager test alert.

Expected database records after a successful alert-driven run:

1. `ops_alert_event` has a new alert event.
2. `ops_diagnosis_dispatch` has a new dispatch record.
3. `ops_incident_state` has a new state record with status moving from `INIT` to `PLANNED` to `COLLECTING` to `SUCCESS` or `FAILED`.
4. `ops_investigation_plan` has a new structured plan record.
5. Existing `ops_incident_diagnosis` continues to receive the diagnosis result.

---

## 20. Phase A.1 Implementation Record

### 20.1 Implemented Scope

Phase A.1 has connected the investigation plan to the actual execution route.

Implemented items:

1. Added `OpsAgentPlanExecutionService`.
2. Loaded latest `OpsInvestigationPlan` into execution `DynamicContext` before Step1 to Step7 starts.
3. Parsed `required_tools_json` into the planned tool set.
4. Added plan-driven gates to:
   - `OpsStep2PrometheusMetricNode`
   - `OpsStep3ElkLogNode`
   - `OpsStep4SkyWalkingTraceNode`
   - `OpsStep6RootCauseAnalysisNode`
5. Added `ops.agent.plan-driven.enabled` configuration.
6. Added `OpsAgentStateService.updateToolHistory`.
7. Persisted `EXECUTED` and `SKIPPED` tool decisions into `ops_incident_state.tool_history_json`.
8. Preserved fallback behavior: if no plan exists, the legacy full chain still runs.

### 20.2 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification requires restarting the application and triggering one alert.

Expected runtime behavior:

1. `ops_investigation_plan.required_tools_json` controls which evidence tools run.
2. Tools not selected by the plan are skipped and recorded as `SKIPPED`.
3. Tools selected by the plan are called and recorded as `EXECUTED`.
4. `ops_incident_state.tool_history_json` contains the tool decisions for the incident.
5. Step5 evidence correlation and Step7 report generation still execute after planned collection.

---

## 21. Phase B Implementation Record

### 21.1 Implemented Scope

Phase B adds the first reflection loop after plan-driven evidence collection.

Implemented items:

1. Added `OpsEvidenceReviewResult`.
2. Added `OpsEvidenceReviewerService`.
3. Enabled `ops.agent.reviewer.enabled`.
4. Extended `OpsAgentPlanExecutionService` with supplemental tool injection.
5. Extended `OpsAgentStateService` with review snapshot persistence.
6. Inserted Evidence Reviewer control into `OpsStep5EvidenceCorrelationNode`.
7. Persisted review result into `ops_incident_state.missing_evidence_json`.
8. Persisted review status into `ops_incident_state.review_status`.
9. Persisted current telemetry snapshot and candidate root causes into `ops_incident_state`.
10. Added reflection-driven supplemental collection for:
    - `query_prometheus`
    - `query_elasticsearch`
    - `query_skywalking_trace`
11. Rebuilt root-cause candidates after supplemental collection.
12. Re-ran reviewer after supplemental collection.
13. Recorded supplemental tool decisions as `SUPPLEMENTED` or `FAILED` in `tool_history_json`.

### 21.2 Runtime Semantics

The execution loop is now:

1. Planner generates required tools.
2. Plan-driven execution collects selected metric/log/trace evidence.
3. Step5 builds root-cause candidates.
4. Evidence Reviewer audits evidence sufficiency.
5. If evidence is sufficient, the chain continues to Runbook and Report.
6. If evidence is insufficient and `current_round < max_rounds`, Reviewer requests missing tools.
7. Missing metric/log/trace evidence is collected in the next round.
8. Root-cause candidates are rebuilt.
9. Reviewer audits again.
10. The chain continues even when final evidence is insufficient, but the report must treat the root cause as hypothesis and list missing evidence.

### 21.3 Review Status Values

Reviewer can produce:

1. `SUFFICIENT`
2. `NEED_MORE_EVIDENCE`
3. `INSUFFICIENT_FINAL`
4. `BYPASSED`

State-level transient status can also be:

1. `SUPPLEMENTING`
2. `REVIEWED`

### 21.4 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification requires restarting the application and triggering alerts that intentionally omit at least one source from the original plan.

Expected runtime behavior:

1. `ops_incident_state.review_status` becomes `SUFFICIENT`, `NEED_MORE_EVIDENCE`, or `INSUFFICIENT_FINAL`.
2. `ops_incident_state.missing_evidence_json` stores the reviewer result.
3. `ops_incident_state.current_round` moves from `1` to `2` when supplemental collection happens.
4. `ops_incident_state.tool_history_json` contains `SUPPLEMENTED` entries when Reviewer asks for missing telemetry.
5. SSE contains `evidence_reviewer_agent` events before Runbook and Report generation.

### 21.5 Remaining Phase B Limits

This Phase B implementation is deterministic and rule-based.

Still pending for later phases:

1. LLM-based reviewer JSON output with schema validation.
2. Reviewer-driven Runbook re-query after Step6.
3. Tool-call budget enforcement by policy table.
4. Multi-scenario evaluation harness.

---

## 22. Phase C Implementation Record

### 22.1 Implemented Scope

Phase C adds Tool Governance before external evidence tools are invoked.

Implemented items:

1. Added `OpsToolGovernanceDecision`.
2. Added `OpsToolGovernanceService`.
3. Enabled `ops.agent.tool-policy.enabled`.
4. Added `ops.agent.tool-policy.max-repeat-per-tool`.
5. Added `ops.agent.tool-policy.disabled-tools`.
6. Added a shared `requestToolAccess` guard in `AbstractOpsAgentExecuteSupport`.
7. Integrated Tool Governance with first-round calls:
   - `query_prometheus`
   - `query_elasticsearch`
   - `query_skywalking_trace`
   - `query_runbook`
8. Integrated Tool Governance with Phase B supplemental calls:
   - supplemental Prometheus metrics
   - supplemental ELK logs
   - supplemental SkyWalking traces
9. Persisted denied calls as `DENIED` in `ops_incident_state.tool_history_json`.
10. Persisted denied calls into `ops_tool_call_log` with `success=false` and status code `429`.
11. Preserved graceful degradation: if a tool is denied, the diagnosis chain continues with unavailable evidence instead of failing the whole incident.

### 22.2 Policy Rules

The current Tool Governance implementation enforces:

1. global max tool-call budget: `ops.agent.max-tool-calls`
2. per-tool repeat limit: `ops.agent.tool-policy.max-repeat-per-tool`
3. disabled tool list: `ops.agent.tool-policy.disabled-tools`

Only actual external tool attempts consume budget. Planner-level `SKIPPED` decisions do not consume budget.

### 22.3 Runtime Semantics

The execution loop is now:

1. Planner selects required tools.
2. Plan-driven execution checks whether a tool should run.
3. Tool Governance checks budget, repeat count, and disabled list.
4. If allowed, the tool is invoked.
5. If denied, the tool is not invoked and unavailable evidence is attached.
6. Denied decisions are written to state and tool-call logs.
7. Evidence Reviewer can still request supplemental tools, but supplemental tools must also pass Tool Governance.

### 22.4 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification can be done by temporarily setting:

```yaml
ops:
  agent:
    tool-policy:
      enabled: true
      max-repeat-per-tool: 1
      disabled-tools: "query_skywalking_trace"
```

Expected runtime behavior:

1. `query_skywalking_trace` is denied before gateway invocation.
2. `ops_incident_state.tool_history_json` contains `DENIED`.
3. `ops_tool_call_log` contains a failed record for `query_skywalking_trace`.
4. Diagnosis continues and reports missing trace evidence rather than crashing.

### 22.5 Remaining Phase C Limits

This Phase C implementation uses application config as the policy source.

Still pending for later phases:

1. database-backed policy table
2. per-tool timeout enforcement wrapper
3. retry strategy by tool type
4. privilege levels for read-only versus mutating tools
5. human approval gates for destructive tools

---

## 23. Phase D Implementation Record

### 23.1 Implemented Scope

Phase D upgrades plain Runbook retrieval into a structured ops skill system.

Implemented items:

1. Added `OpsAgentSkill` domain model.
2. Added file-based `OpsAgentSkillService` metadata loader.
3. Added `ops_agent_skill.sql`.
4. Added skill markdown template under `docs/dev-ops/skills`.
5. Added six initial ops skills:
   - `database-connection-pool`
   - `http-500-error`
   - `jvm-full-gc`
   - `mq-backlog`
   - `redis-timeout`
   - `rpc-timeout`
6. Mapped existing Runbook markdown files to structured skills.
7. Integrated matched skills into `OpsAgentPlannerService`.
8. Planner now adds skill-recommended tools into `requiredTools`.
9. Planner now writes `matchedSkills` into `plan_json`.
10. Integrated matched skills into `OpsStep6RootCauseAnalysisNode`.
11. Runbook evidence now includes `[Skill]` matches in addition to File or PGVector Runbook matches.
12. Local skill matching still works when external `query_runbook` is skipped or denied by Tool Governance.
13. Added `ops.agent.skill` configuration.

### 23.2 Runtime Semantics

The Runbook layer now works in two levels:

1. External Runbook retrieval
   - File Runbook or PGVector Runbook search.
   - Governed by `query_runbook` policy.
2. Local structured skill matching
   - Reads markdown skill metadata from `docs/dev-ops/skills`.
   - Matches alert rules, symptoms, metrics, logs, traces, and candidate root causes.
   - Provides recommended tools and remediation guidance.
   - Does not require external tool permission because it is local metadata.

Planner behavior:

1. The Planner classifies alert type.
2. The Planner matches local ops skills.
3. The Planner merges skill-recommended tools into the investigation plan.
4. The persisted plan contains `matchedSkills`.

Report behavior:

1. Step6 combines normal Runbook matches and structured skill matches.
2. Step7 receives the combined Runbook context.
3. Final report generation can cite skill-backed suggestions through `[Skill]` Runbook entries.

### 23.3 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification requires restarting the application and triggering one alert.

Expected database checks:

1. `ops_investigation_plan.plan_json` contains `matchedSkills`.
2. `ops_investigation_plan.required_tools_json` contains tools recommended by matched skills.
3. `ops_incident_diagnosis.runbook_json` contains entries whose `runbookId` starts with `skill:`.
4. Final report contains skill-backed mitigation or long-term fix suggestions.

### 23.4 Remaining Phase D Limits

This Phase D implementation uses markdown files as the primary skill source.

Still pending for later phases:

1. database-backed skill loader from `ops_agent_skill`
2. skill vectorization with structured metadata filters
3. LLM-based skill selection with schema validation
4. skill quality scoring in the evaluation harness

---

## 24. Phase E Implementation Record

### 24.1 Implemented Scope

Phase E adds a replayable evaluation harness for measuring AutoAgent diagnosis quality.

Implemented items:

1. Added `ops_eval_case`, `ops_eval_run`, and `ops_eval_metric` SQL.
2. Added six initial evaluation cases:
   - HTTP 5xx caused by application exception
   - HTTP latency caused by downstream RPC timeout
   - DB connection pool exhausted
   - JVM Full GC causing request latency
   - Redis timeout causing partial failures
   - MQ backlog causing delayed processing
3. Added `OpsEvalCase`, `OpsEvalRun`, `OpsEvalMetric`, and `OpsEvaluationSummary` domain models.
4. Added `IOpsEvaluationRepository`.
5. Added MyBatis DAO, PO, mapper XML, and repository implementation.
6. Added `OpsEvaluationService`.
7. Added `OpsEvaluationController` endpoint:

```text
POST /api/v1/ops/evaluation/run
```

8. Evaluation runs now execute the real diagnosis chain through `OpsIncidentExecuteStrategy`.
9. Evaluation runs initialize Agent state and Planner records through `OpsAgentBootstrapService`.
10. Each case persists a run record and metric records.
11. Added aggregate summary metrics:
    - `top1RootCauseHitRate`
    - `top3RootCauseHitRate`
    - `averageEvidenceCoverage`
    - `averageToolCallCount`
    - `averageLatencyMs`
12. Enabled `ops.agent.evaluation.enabled`.

### 24.2 Runtime Semantics

The evaluation flow is:

```text
Load enabled eval cases
-> Build synthetic IncidentCommand and AlertEvent
-> Initialize Agent state and investigation plan
-> Execute the real AutoAgent diagnosis chain
-> Query diagnosis record and incident state
-> Score root cause hit, evidence coverage, tool count, latency, and unsupported conclusions
-> Persist run and metric records
-> Return batch summary
```

This harness intentionally uses the same diagnosis chain as production diagnosis. It is not a text-only scorer.

### 24.3 Metrics

Current metrics:

1. `top1RootCauseHit`
2. `top3RootCauseHit`
3. `requiredEvidenceCoverage`
4. `unsupportedConclusionCount`
5. `toolCallCount`
6. `diagnosisLatencyMs`
7. `finalStatus`

The first implementation uses deterministic keyword matching against diagnosis reports, candidate root causes, telemetry JSON, and state snapshots.

### 24.4 Verification

Compile verification:

```text
mvn -q -DskipTests compile
```

Result:

```text
PASS
```

Runtime verification requires applying `ops_eval_harness.sql`, restarting the application, and calling:

```text
POST http://127.0.0.1:8099/api/v1/ops/evaluation/run
```

Expected database records:

1. `ops_eval_case` contains the six seed cases.
2. `ops_eval_run` contains one record per executed case.
3. `ops_eval_metric` contains metric records for every run.
4. `ops_incident_state` contains state records for evaluation diagnoses.
5. `ops_investigation_plan` contains plans for evaluation diagnoses.
6. `ops_incident_diagnosis` contains generated diagnosis records.

### 24.5 Remaining Phase E Limits

The evaluation harness is now executable and persistent, but the scoring logic is still deterministic.

Still pending for later improvement:

1. semantic root-cause matching instead of keyword matching
2. expected tool coverage as a first-class persisted metric
3. unsupported conclusion detection with an LLM judge
4. case-level fixtures for mocked Prometheus, ELK, and SkyWalking data
5. CI-friendly evaluation mode without live external dependencies

### 24.6 Post-Review Hardening

After static review, these fixes were applied before closing Phase E:

1. Planner classification now checks DB, JVM, Redis, MQ, and RPC domain keywords before generic latency or timeout keywords.
2. Planner supports `RPC_TIMEOUT` as a first-class alert type.
3. Plan steps now include `inputConstraints`.
4. Skill-recommended tools now create corresponding investigation steps instead of only being appended to `requiredTools`.
5. Step6 now writes Runbook and Skill evidence back to `ops_incident_state.runbook_evidence_json`.

---

## 25. Parallel Track: Multi ChatClient Agent Skeleton

### 25.1 Implemented Scope

In parallel with Phase E, the project now has a minimal service boundary for true multi ChatClient Agents.

Implemented items:

1. Added Agent role model for:
   - Planner Agent
   - Evidence Reviewer Agent
   - Report Writer Agent
2. Added Chat Agent input and output wrappers.
3. Added ChatClient resolver boundary.
4. Added Spring AI ChatClient adapter.
5. Added role-specific prompt/schema templates.
6. Added `OpsMultiChatAgentService`.
7. Added a design note under `docs/ops-multi-chatclient-agent-design.md`.

### 25.2 Current Positioning

This track does not replace the existing rule-based Planner, Reviewer, or Report generation yet.

It provides the next integration boundary:

1. `OpsAgentPlannerService` can delegate to Planner Chat Agent behind a feature flag.
2. `OpsEvidenceReviewerService` can delegate to Reviewer Chat Agent behind a feature flag.
3. `OpsStep7ReportGenerationNode` can delegate to Report Writer Chat Agent behind a feature flag.

### 25.3 Remaining Integration Work

Pending work:

1. Add `ops.agent.chat.enabled`.
2. Wire Planner Chat Agent with rule-based fallback.
3. Wire Reviewer Chat Agent with JSON schema validation and fallback.
4. Wire Report Writer Chat Agent with evidence-constrained prompt.
5. Persist each Chat Agent invocation as auditable Agent decision records.

---

## 26. Remaining Upgrade Execution Plan

This section is the mandatory execution plan for the unfinished Agentic upgrade work after Phase E.

From this point forward, remaining upgrades must follow this order:

```text
R1 Review Observability
-> R2 Supplemental Plan Persistence
-> R3 DB-backed Tool Policy
-> R4 Multi ChatClient Agent Integration
-> R5 Evaluation Harness Hardening
-> R6 Runtime Smoke Verification and Resume Wording
```

Parallel implementation is allowed only when write scopes are separated. Main-thread integration must compile the whole project after each merged stage.

### R1 Review Observability

Goal:

Make every Evidence Reviewer decision auditable instead of only keeping the latest snapshot in `ops_incident_state`.

Deliverables:

1. `OpsAgentReview` domain model.
2. `ops_agent_review.sql`.
3. DAO, PO, mapper XML, and repository methods.
4. Persist every first review and re-review.
5. Include `diagnosisId`, `stateId`, `planId`, `round`, `reviewStatus`, `confidence`, `missingEvidenceJson`, `nextActionsJson`, `stopReason`, and `reviewerType`.

Acceptance:

1. One diagnosis can have multiple review records.
2. `审查 -> 补证 -> 复评` can be reconstructed from database records.
3. `ops_incident_state` remains the latest state snapshot only.

### R2 Supplemental Plan Persistence

Goal:

Make Reflection Loop produce a persisted supplementary plan instead of only mutating in-memory planned tools.

Deliverables:

1. Create a new `OpsInvestigationPlan` when Reviewer requests supplementary tools.
2. The supplementary plan must use `round = currentRound + 1`.
3. The supplementary plan must include only the requested missing tools and their reasons.
4. State status must move through `NEED_MORE_EVIDENCE` and `SUPPLEMENTING`.
5. Tool history must link supplemented calls to the supplementary plan round.

Acceptance:

1. `ops_investigation_plan` contains a second plan for insufficient-evidence cases.
2. Supplemented tool calls are traceable to that second plan.
3. If max round is reached, no supplementary plan is created.

### R3 DB-backed Tool Policy

Goal:

Upgrade Tool Governance from config-only limits to database-backed per-tool policy.

Deliverables:

1. `OpsToolPolicy` domain model.
2. `ops_tool_policy.sql`.
3. DAO, PO, mapper XML, and repository methods.
4. `OpsToolGovernanceService` reads DB policy first, then falls back to config.
5. Policy fields must include `toolName`, `enabled`, `agentRole`, `maxCallsPerDiagnosis`, `timeoutSeconds`, `requiredSeverity`, `allowAutoExecute`, and `requiresApproval`.

Acceptance:

1. Disabling a tool in DB prevents gateway invocation.
2. Per-tool max calls are enforced.
3. Denied tool calls are persisted into `ops_tool_call_log`.
4. Config fallback still works when the policy table is empty.

### R4 Multi ChatClient Agent Integration

Goal:

Move from role-named services to feature-flagged, independent ChatClient Agents.

Deliverables:

1. Add `ops.agent.chat.enabled`.
2. Add per-role client config:
   - `planner-client-id`
   - `reviewer-client-id`
   - `report-writer-client-id`
3. Integrate `OpsMultiChatAgentService` into:
   - `OpsAgentPlannerService`
   - `OpsEvidenceReviewerService`
   - `OpsStep7ReportGenerationNode`
4. Keep deterministic fallback for every role.
5. Persist Chat Agent invocation input/output summary as auditable decisions.

Acceptance:

1. When `ops.agent.chat.enabled=false`, current rule-based flow remains unchanged.
2. When enabled and ChatClient is available, Planner/Reviewer/Report Writer can call independent ChatClients.
3. Failed ChatClient calls fall back without breaking diagnosis.
4. The final report can state whether rule-based or ChatClient Agent output was used.

### R5 Evaluation Harness Hardening

Goal:

Make evaluation measure tool selection and evidence quality more directly.

Deliverables:

1. Add expected tool coverage calculation from `expectedToolsJson`.
2. Persist `expectedToolCoverage` into `ops_eval_metric`.
3. Add case-level scoring details for missed tools and missing evidence.
4. Add an option to run only one case by `caseId`.
5. Add CI-friendly mode later with mocked telemetry fixtures.

Acceptance:

1. Evaluation summary includes tool coverage.
2. Each run records missed expected tools.
3. A single case can be replayed without running all six cases.

### R6 Runtime Smoke Verification and Resume Wording

Goal:

Make the project demonstrable and avoid overclaiming.

Deliverables:

1. Apply all new SQL files.
2. Start the app with `full` profile.
3. Trigger one synthetic Alertmanager alert.
4. Run one evaluation case.
5. Record verification SQL and observed results.
6. Update resume wording to distinguish:
   - completed rule-based Agentic workflow
   - feature-flagged multi ChatClient Agent integration
   - remaining limitations

Acceptance:

1. Alert-driven diagnosis still creates diagnosis, state, plan, review, tool history, and report records.
2. Evaluation run creates `ops_eval_run` and `ops_eval_metric`.
3. Resume wording is technically defensible under interview questioning.

