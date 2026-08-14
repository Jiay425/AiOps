# Incident-to-Fix Product API

## Goal

Incident-to-Fix is exposed as a product-level workflow instead of a raw internal skill trace. The API returns a fixed stage view that is suitable for frontend display, demo replay, and interview explanation.

## Submit Incident

`POST /api/v1/codeops/task/incident/submit`

Example body:

```json
{
  "serviceName": "order-service",
  "alertRule": "HTTP_5XX_RATE_HIGH",
  "severity": "P1",
  "problem": "POST /api/orders/submit 5xx error rate increased to 8.2%",
  "endpoint": "POST /api/orders/submit",
  "traceId": "trace-order-submit-001",
  "repository": "samples/order-service",
  "allowPatchApply": true,
  "allowTestPatchApply": true,
  "fixtureFallbackAllowed": false,
  "labels": {
    "service": "order-service",
    "alertname": "HTTP_5XX_RATE_HIGH"
  }
}
```

The endpoint creates an `INCIDENT_TO_FIX` task and returns the product view directly.

## Query Product View

`GET /api/v1/codeops/task/incident/{taskId}`

The response contains:

- `currentStage`: current product stage.
- `progressPercent`: stage-level progress.
- `requiresApproval` and `approvalStatus`.
- `incident`: normalized incident metadata.
- `guardrails`: safety gates such as sandbox, static safety, evidence coverage, and approval state.
- `artifacts`: aggregated evidence, localization, patch, test, and release-risk outputs.
- `stages`: fixed Incident-to-Fix stage list.
- `trace`: original detailed task trace.

## Product Stages

The fixed stages are:

1. `ops_evidence`: metrics, logs, trace, and Runbook evidence.
2. `code_localization`: suspicious files, methods, and code snippets.
3. `knowledge_rag`: engineering knowledge and Runbook matches.
4. `code_repair`: LLM patch generation, scope guard, sandbox, compile gate.
5. `test_verification`: Maven/test commands, execution results, failure feedback.
6. `release_risk`: observation metrics, rollback concerns, human approval points.
7. `human_approval`: high-risk patch approval gate.

## Approval

Product approval endpoints are available under the task API:

- `GET /api/v1/codeops/task/{taskId}/approval`
- `POST /api/v1/codeops/task/{taskId}/approval/approve`
- `POST /api/v1/codeops/task/{taskId}/approval/reject`

Reject body:

```json
{
  "reason": "Patch touches payment status update and needs DBA review first."
}
```

The older evaluation approval endpoints remain available for compatibility, but product integrations should use the task endpoints.
