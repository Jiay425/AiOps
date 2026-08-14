# Tool Gateway Standardization

## Goal

Tool Gateway is the unified boundary between agents and executable capabilities. Every tool call should be auditable before it affects repository code, local commands, observability systems, or knowledge retrieval.

## Standard Tool Definition

Each tool definition contains:

- `toolName`: stable tool id used by prompts and policies.
- `description`: short behavior description.
- `category`: repository, command, observability, knowledge, or artifact.
- `riskLevel`: human-facing risk label.
- `accessLevel`: read-only, command execution, source write, external call, and so on.
- `sourceType`: local repository, local command, real gateway, fixture fallback, or sandbox.
- `timeoutMillis`: default runtime budget.
- `enabled`: whether the tool can be selected.

## Runtime Trace

`ToolRuntimeService` records every gateway call with:

- task, trace, execution, and skill identity.
- tool name, category, access level, and source type.
- request summary and response summary.
- status, success flag, error type, error message, and cost.
- sanitized metadata.

The runtime trace is written to task context as `toolRuntimeTrace`. Each step also receives the subset of tool calls for its `executionId` under `toolRuntime`, so the trace page can explain which agent step used which tool.

## Current Engineering Gateway Coverage

`EngineeringToolGateway` now standardizes:

- `repo.create_snapshot`
- `repo.search_text`
- `repo.read_file_snippet`
- `repo.git_diff`
- `repo.maven`

The tool catalog also reserves observability tools:

- `ops.query_prometheus`
- `ops.search_logs`
- `ops.query_trace`

## Next Gateway Targets

The next hardening pass should connect the same runtime protocol to real observability gateways:

- Prometheus metric queries: record PromQL, time window, target service, source type, and returned series count.
- Elasticsearch log search: record index, query DSL summary, time window, hit count, and whether fixture fallback was used.
- SkyWalking trace query: record endpoint, trace id/span id, time window, and span count.
- Runbook RAG: record query, vector score, BM25 score, rerank score, chunk id, runbook version, and final rank.

No raw secrets or full source file contents should be stored in tool summaries.
