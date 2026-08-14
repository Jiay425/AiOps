param(
    [string]$ElasticsearchUrl = "http://127.0.0.1:9200",
    [string]$Index = "ops-demo-service-log-verify",
    [string]$ServiceName = "ops-demo-service",
    [string]$TraceId = "trace-ops-verify-001",
    [string]$Timestamp = ""
)

if ([string]::IsNullOrWhiteSpace($Timestamp)) {
    $Timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
}

$docs = @(
    @{
        "@timestamp" = $Timestamp
        "serviceName" = $ServiceName
        "application" = $ServiceName
        "traceId" = $TraceId
        "level" = "ERROR"
        "message" = "SQLTimeoutException: HikariPool-1 - Connection is not available, request timed out after 30000ms"
        "exception" = "java.sql.SQLTimeoutException"
        "stack_trace" = "java.sql.SQLTimeoutException: Connection is not available`n at com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:200)"
    },
    @{
        "@timestamp" = $Timestamp
        "serviceName" = $ServiceName
        "application" = $ServiceName
        "traceId" = $TraceId
        "level" = "ERROR"
        "message" = "Mock order create failed: database connection timeout"
        "exception" = "java.lang.IllegalStateException"
        "stack_trace" = "java.lang.IllegalStateException: database connection timeout`n at com.opsautoagent.trigger.http.OpsMockFaultController.createOrder"
    }
)

foreach ($doc in $docs) {
    $body = $doc | ConvertTo-Json -Depth 8
    Invoke-RestMethod -Method Post -Uri "$ElasticsearchUrl/$Index/_doc" -ContentType "application/json" -Body $body | Out-Null
}

Invoke-RestMethod -Method Post -Uri "$ElasticsearchUrl/$Index/_refresh" | Out-Null

Write-Host "Seeded ELK evidence documents."
Write-Host "index=$Index serviceName=$ServiceName traceId=$TraceId timestamp=$Timestamp"

