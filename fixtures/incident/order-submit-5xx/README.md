# order-service 5xx incident fixture

This fixture describes a complete Incident-to-Fix scenario for the CodeOps agent chain.

## Incident

- Service: `order-service`
- Endpoint: `POST /orders/submit`
- Symptom: HTTP 5xx rate increases during order submission.
- Time window: see `alertmanager.json`
- Primary exception signal: `NullPointerException` in `OrderSubmitService.submit`

## Evidence files

- `alertmanager.json`: Alertmanager payload that starts the incident task.
- `prometheus.json`: service-level 5xx and latency metrics around the alert window.
- `es-logs.json`: application error logs with stack trace and request fields.
- `skywalking-trace.json`: trace spans for controller, service and repository calls.
- `eval-case.json`: expected localization and fix characteristics for the harness.

## Expected agent path

1. Ops evidence analysis should identify `order-service`, `POST /orders/submit`, 5xx increase and `NullPointerException`.
2. Code localization should map the stack trace to `samples/order-service/src/main/java/com/example/order/OrderSubmitService.java`.
3. Patch generation should propose a minimal guard around `OrderSubmitRequest.unitPrice` and `quantity` before amount calculation.
4. Test verification should run `OrderSubmitServiceTest` or an equivalent focused Maven test command.
5. Release risk analysis should call out API compatibility, validation behavior and order submission rollback observation.

## Validation target

The generated patch must be a proposal only. It should not be applied automatically by the agent runtime. Patch validation should check that the unified diff references existing files under `samples/order-service` and does not create files through `/dev/null`.
