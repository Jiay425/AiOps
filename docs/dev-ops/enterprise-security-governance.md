# Enterprise Security and Governance

## Goal

Incident-to-Fix can diagnose, generate patches, run compile/test gates, and produce release risk analysis. Enterprise usage needs explicit safety boundaries so the agent is auditable and cannot silently perform high-risk operations.

## Governance Layers

The CodeOps security model currently includes:

1. API security: ops endpoints support token verification and rate limiting.
2. Tool Gateway audit: every repository search, file read, diff load, and Maven command is traced.
3. Secret redaction: tool summaries and metadata mask API keys, tokens, passwords, secrets, and `sk-*` keys.
4. Command allowlist: only build/test/inspect commands such as Maven and Git are allowed.
5. Dangerous pattern denylist: destructive shell/database/file patterns are blocked before execution.
6. Repository-scoped write policy: source writes must stay inside the target repository `src/**`.
7. Patch guard: patches are checked before apply for scope violations.
8. Sandbox and rollback: patches are applied in an isolated workspace and can be rolled back from snapshots.
9. Compile/test gates: generated fixes must pass deterministic build/test verification.
10. Human approval: high-risk or guardrail-triggering patches enter an approval state before completion.

## Product APIs

Global governance summary:

`GET /api/v1/codeops/task/security/governance`

Returns:

- permission policy summary
- allowed commands and blocked patterns
- enterprise controls
- recent denied tool calls
- recent tool audit counts

Task-level governance summary:

`GET /api/v1/codeops/task/{taskId}/security`

Returns:

- task permission policy
- guardrail summary
- approval status and reasons
- task tool audit
- risk posture

## Risk Posture

The task security view classifies posture as:

- `LOW`: no denied tools, no pending approval, guardrails look healthy.
- `MEDIUM`: guardrails indicate non-isolated sandbox or static safety issues.
- `HIGH`: human approval is pending or at least one tool call was denied.

This is intentionally deterministic. The LLM can recommend actions, but safety gates are enforced by code.

## Interview Story

The project does not let the Agent freely execute arbitrary tools. All actions go through a Tool Gateway and a permission policy. The Agent can propose and reason, but command execution, patch application, file writes, compile/test gates, rollback, and approval are deterministic. This keeps the system closer to enterprise Agent platforms where observability, auditability, and human-in-the-loop controls matter as much as reasoning ability.
