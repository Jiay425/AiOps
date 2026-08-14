# Ops real-chain verification

This verification is for the production-like diagnosis chain. It does not treat mock text as evidence. It checks that the application can read from real Prometheus, Elasticsearch, SkyWalking GraphQL, and PgVector.

In the `full` profile, Prometheus and Elasticsearch are read through MCP tools first:

```yaml
ops.integrations.mcp.prefer=true
ops.integrations.mcp.fallback-http=false
ops.integrations.mcp.grafana.mcp-id=5008
ops.integrations.mcp.elasticsearch.mcp-id=5007
```

So Prometheus metrics are collected through the Grafana MCP client, and ELK logs are collected through the Elasticsearch MCP client. If the MCP client is not initialized or the tool call fails, the full-chain verification fails instead of silently using HTTP.

## Start infrastructure

```powershell
docker compose -f docs\dev-ops\docker-compose-ops-full.yml up -d
```

## Start the application

Use the `full` profile:

```powershell
$env:JAVA_HOME='D:\Java\jdk17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -pl ops-autoagent-app -am spring-boot:run -Dspring-boot.run.profiles=full
```

For real SkyWalking trace data, start the JVM with the SkyWalking Java Agent. The agent is stored in:

```text
docs/dev-ops/skywalking-agent/skywalking-agent.jar
```

If the directory is missing, prepare it from the official Docker image:

```powershell
.\docs\dev-ops\prepare-skywalking-agent.ps1
```

Recommended start command:

```powershell
.\docs\dev-ops\start-app-full-skywalking.ps1
```

Equivalent Maven profile:

```powershell
$env:JAVA_HOME='D:\Java\jdk17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
mvn -pl ops-autoagent-app -am spring-boot:run -Pfull-skywalking
```

Equivalent JVM arguments for IntelliJ IDEA VM Options:

```powershell
-javaagent:E:\DeskTop\java_project\ops-autoagent-diagnosis-3-14-agent-2-prometheus\ops-autoagent-diagnosis-3-14-agent-2-prometheus\docs\dev-ops\skywalking-agent\skywalking-agent.jar
-Dskywalking.agent.service_name=ops-demo-service
-Dskywalking.collector.backend_service=127.0.0.1:11800
-Dskywalking.agent.instance_name=local-full-8099
```

Without the Java Agent, SkyWalking OAP can be reachable, but trace data will be empty. The verification endpoint reports this separately.

## Seed and verify

```powershell
.\docs\dev-ops\verify-ops-full-chain.ps1
```

The script does four things:

1. Checks the app environment endpoint.
2. Inserts real ERROR documents into Elasticsearch.
3. Calls the mock fault endpoint to generate Prometheus metrics and SkyWalking spans.
4. Calls `POST /api/v1/ops/verify/full-chain`.

Expected readiness:

```json
{
  "prometheus": {"sourceReachable": true},
  "elk": {"sourceReachable": true},
  "skywalking": {"sourceReachable": true},
  "pgvector": {"sourceReachable": true},
  "overallReady": true
}
```

`sourceReachable=true` means the application read from the real middleware. Evidence-specific fields such as `hasIncidentSamples`, `hasTraceData`, and `hasAnomaly` show whether the seeded incident produced usable diagnosis evidence.

MCP tool calls are persisted in `ops_tool_call_log` with `tool_name = 'mcp'`, including the MCP id, tool name, arguments, response summary, success flag, and cost.

## Run full AI diagnosis

After the source-readiness verification passes, run the complete AI diagnosis flow:

```powershell
.\docs\dev-ops\run-ops-ai-diagnosis.ps1
```

This script seeds Elasticsearch, generates real application traffic, calls the SSE diagnosis endpoint, extracts the `diagnosisId`, and queries the persisted MySQL review record.

The final checks must include:

```text
status = SUCCESS
hasMetricEvidence = true
hasLogEvidence = true
hasEvidenceChain = true
hasRunbook = true
hasReport = true
```

If the LLM provider is unavailable, the platform still persists a fallback report and records the failed `llm` tool call in `ops_tool_call_log`. When the LLM provider is reachable, `ops_tool_call_log` should contain a successful `tool_name = 'llm'` record for the same `diagnosisId`.

