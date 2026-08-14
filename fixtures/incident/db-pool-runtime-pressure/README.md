# DB Pool Runtime Pressure Incident

This fixture models a runtime/configuration incident rather than a code bug.

The alert only says the order-service submit endpoint has rising 5xx errors while Hikari active connections are at max, pending acquisition is high, and connection acquisition times out. Prometheus, logs, and trace evidence all point to database pool saturation under traffic pressure.

Expected behavior:

1. Incident Triage Agent should classify the strategy as CONFIG_FIX or RUNTIME_ACTION.
2. The workflow should not enter code repair or test verification.
3. Release Risk Agent should produce mitigation, rollback, and observation guidance for connection pool saturation.
