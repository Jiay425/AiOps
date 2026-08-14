# Multi ChatClient Ops Agent Design

This document records the parallel upgrade path for turning the current rule-based ops agent roles into independently runnable ChatClient agents.

## Goal

The current diagnosis chain already has agent-like responsibilities:

- Planner: decide what to investigate.
- Evidence Reviewer: judge whether evidence is sufficient.
- Report Writer: produce the final diagnosis report.

The first implementation boundary is intentionally additive. It does not replace `OpsAgentPlannerService`, `OpsEvidenceReviewerService`, or `OpsStep7ReportGenerationNode`. It adds a new `com.opsautoagent.domain.ops.agent.chat` package that the main diagnosis chain can call later.

## Runtime Boundary

`OpsMultiChatAgentService` exposes three role-specific methods:

- `plan(input)`
- `reviewEvidence(input)`
- `writeReport(input)`

Each method accepts `OpsChatAgentInput` and returns `OpsChatAgentOutput`. The output includes success status, fallback flag, resolved bean name, resolution source, content, error message, and latency.

## ChatClient Resolution

`DefaultOpsChatClientResolver` resolves a role in this order:

1. Use configured dynamic AI client bean:
   - `ops.agent.chat.planner-client-id`
   - `ops.agent.chat.reviewer-client-id`
   - `ops.agent.chat.report-writer-client-id`
2. If no role-specific bean exists, build a role-scoped ChatClient from `openAiChatModel`.
3. If neither path is available, return an unavailable fallback result without throwing into the main chain.

This keeps compatibility with the existing `ai_client_{id}` dynamic Bean registration pattern.

## Prompt Contracts

`OpsChatAgentPrompts` defines one system prompt and one JSON output schema per role.

- Planner output: investigation plan, required tools, hypotheses, expected evidence.
- Evidence Reviewer output: sufficiency status, confirmed facts, weak evidence, missing evidence, required tools, report constraints.
- Report Writer output: markdown-ready report fields constrained by evidence and runbooks.

## R4 Feature-Flag Integration

`ops.agent.chat.enabled` controls whether the main diagnosis chain tries the independent ChatClient agents.

Default:

```yaml
ops:
  agent:
    chat:
      enabled: false
      planner-client-id: ""
      reviewer-client-id: ""
      report-writer-client-id: ""
```

Runtime behavior:

1. When disabled, the existing rule-based Planner, deterministic Reviewer, and existing Step7 report generation path remain unchanged.
2. When enabled, `OpsAgentPlannerService` first builds the stable rule-based plan, then asks the Planner Chat Agent for a JSON plan. A valid response is persisted with `plannerType=CHAT_AGENT`; invalid or unavailable output falls back to the rule plan with `plannerType=RULE_BASED_FALLBACK`.
3. When enabled, `OpsEvidenceReviewerService` first builds the deterministic review result, then asks the Reviewer Chat Agent for a JSON review. Invalid or unavailable output returns the deterministic result and appends a fallback marker to the rationale.
4. When enabled, `OpsStep7ReportGenerationNode` asks the Report Writer Chat Agent for JSON containing `finalReportMarkdown`. Invalid or unavailable output falls back to the existing report generation path.
5. Chat Agent failures never abort diagnosis. They only mark the output as `CHAT_AGENT` or fallback and let the stable workflow continue.

## Remaining Integration Points

1. Persist each role invocation to `ops_tool_call_log` or a future `ops_agent_chat_call_log`.
2. Add evaluation harness cases that compare rule-based and ChatClient-based role outputs.
3. Move JSON-schema validation from lightweight parsing to explicit typed contracts once the prompt format stabilizes.

