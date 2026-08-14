# order-service sample

`order-service` is a small Java service used by the CodeOps Incident-to-Fix verification flow.

The service contains normal order submission and flash-sale order submission paths. Production-like alerts may reference symptoms such as 5xx responses, duplicate request processing, order conflicts, and stock anomalies. The Agent is expected to inspect the source code, tests, logs, metrics, and traces to determine the actual faulty implementation.

## Useful commands

```powershell
mvn -q test
mvn -q -Dtest=OrderSubmitServiceTest test
```
