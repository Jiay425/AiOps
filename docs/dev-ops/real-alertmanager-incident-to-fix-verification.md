# Real Alertmanager Incident-to-Fix Verification

## Why This Exists

Interviewers may challenge whether Incident-to-Fix can handle real online incidents if it has not been connected to production. The correct proof is not a fixture demo. The proof is an Alertmanager-compatible webhook payload entering the same alert normalization, live evidence mode, CodeOps task creation, tool gateway audit, product trace, and safety governance path.

## Verification Endpoint

`POST /api/v1/ops/alert/webhook/alertmanager/incident-to-fix/verify`

This endpoint accepts the same payload shape as Alertmanager webhook.

It does:

1. API guard and rate-limit check.
2. Alertmanager payload normalization.
3. Incident command creation.
4. Synchronous Incident-to-Fix task submission.
5. LIVE evidence mode with fixture fallback disabled.
6. Product view return with task id, stages, evidence, patch/test/risk artifacts, guardrails, approval state, and raw trace.

It does not silently fall back to fixture evidence. If Prometheus, Elasticsearch, SkyWalking, or Runbook services are unavailable, the returned view must expose missing real evidence instead of pretending that diagnosis succeeded.

## Example Payload

```json
{
  "version": "4",
  "receiver": "autoagent-codeops",
  "status": "firing",
  "groupKey": "{}:{alertname=\"HTTP_5XX_RATE_HIGH\", service=\"order-service\"}",
  "externalURL": "http://alertmanager:9093",
  "commonLabels": {
    "alertname": "HTTP_5XX_RATE_HIGH",
    "service": "order-service",
    "severity": "P1",
    "repository": "samples/order-service",
    "endpoint": "POST /api/orders/submit",
    "traceId": "trace-order-submit-001"
  },
  "commonAnnotations": {
    "summary": "order-service submit endpoint 5xx rate is above 8%",
    "description": "POST /api/orders/submit returns 5xx. Please diagnose metrics, logs, traces, code, tests and release risk.",
    "codeops.allowPatchApply": "true",
    "codeops.allowTestPatchApply": "true"
  },
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "instance": "order-service-7f8b9d",
        "pod": "order-service-7f8b9d-abc12"
      },
      "annotations": {
        "runbook": "database-connection-pool, http-5xx, order-submit"
      },
      "startsAt": "2026-06-06T10:00:00+08:00",
      "endsAt": "",
      "fingerprint": "real-chain-order-5xx-001",
      "generatorURL": "http://prometheus:9090/graph?g0.expr=sum(rate(http_server_requests_seconds_count{status=~\"5..\"}[5m]))"
    }
  ]
}
```

## What To Check In The Response

The response should contain:

- `taskId`: proves CodeOps task was created.
- `incident.source=alertmanager`: proves the entry was Alertmanager-style alert ingestion.
- `incident.evidenceMode=LIVE`: proves it is not fixture mode.
- `incident.fixtureFallbackAllowed=false`: proves demo fallback is disabled.
- `stages`: fixed product chain from evidence to approval.
- `artifacts.evidence.evidenceCoverage`: shows which real data sources are available.
- `artifacts.patch`: shows whether patch generation, scope guard, sandbox, compile gate ran.
- `artifacts.tests`: shows Maven/test execution results.
- `artifacts.releaseRisk`: shows release risk and observation items.
- `guardrails`: shows sandbox, static safety, evidence coverage, and approval status.
- `trace.workingMemorySummary.toolRuntimeTrace`: shows tool calls and their status.

## How To Answer The Interview Question

If asked "You have not connected production, how do you know it can handle online incidents?", answer:

I built a real-chain verification path that accepts the exact Alertmanager webhook schema. The payload goes through the same normalization, incident command construction, live evidence mode, CodeOps task creation, tool gateway audit, safety guardrails and product trace as the real system. I explicitly disabled fixture fallback in this path, so if Prometheus, ES or SkyWalking are not available, the response reports missing live evidence rather than fabricating evidence. This proves the integration boundary and orchestration chain are real, while production confidence still depends on connecting actual company observability endpoints and running shadow-mode validation.

## Honest Boundary

This verification does not claim production incident accuracy by itself. It proves:

- the system can receive a real Alertmanager-style alert;
- the alert can trigger the Incident-to-Fix chain;
- the chain can use live evidence gateways when configured;
- missing live dependencies are visible;
- code repair/test/risk stages are traceable and governed.

Production readiness still requires shadow traffic, real Prometheus/ES/SkyWalking endpoints, service-to-repository mapping, and historical incident replay.
