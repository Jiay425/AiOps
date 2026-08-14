param(
    [string]$AppUrl = "http://127.0.0.1:8099",
    [string]$ElasticsearchUrl = "http://127.0.0.1:9200",
    [string]$ServiceName = "ops-demo-service",
    [string]$TraceId = "trace-ops-verify-001",
    [int]$PrometheusWaitSeconds = 20
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startTime = (Get-Date).AddMinutes(-10).ToString("yyyy-MM-dd HH:mm:ss")
$seedTime = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")

Write-Host "1. Check application environment..."
Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/environment" | ConvertTo-Json -Depth 8

Write-Host "2. Seed real Elasticsearch evidence..."
& "$scriptDir/seed-elk-ops-evidence.ps1" -ElasticsearchUrl $ElasticsearchUrl -ServiceName $ServiceName -TraceId $TraceId -Timestamp $seedTime

Write-Host "3. Generate application traffic for Prometheus and SkyWalking..."
1..8 | ForEach-Object {
    try { Invoke-WebRequest -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=error" -UseBasicParsing | Out-Null } catch {}
}
1..4 | ForEach-Object {
    Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=slow&sleepMillis=1500" | Out-Null
}
1..2 | ForEach-Object {
    Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=db&holdSeconds=3" | Out-Null
}

Write-Host "4. Wait $PrometheusWaitSeconds seconds for Prometheus scrape and SkyWalking flush..."
Start-Sleep -Seconds $PrometheusWaitSeconds

$endTime = (Get-Date).AddMinutes(1).ToString("yyyy-MM-dd HH:mm:ss")
$body = @{
    serviceName = $ServiceName
    startTime = $startTime
    endTime = $endTime
    problem = "$ServiceName verification: real Prometheus, ELK, SkyWalking, and PgVector reads must all succeed."
    traceId = $TraceId
} | ConvertTo-Json -Depth 8

Write-Host "5. Call real full-chain verification endpoint..."
$result = Invoke-RestMethod -Method Post -Uri "$AppUrl/api/v1/ops/verify/full-chain" -ContentType "application/json" -Body $body
$result | ConvertTo-Json -Depth 12

if ($result.data.overallReady -ne $true) {
    throw "Full-chain source readiness failed. Check prometheus/elk/skywalking/pgvector fields above."
}

Write-Host "Full-chain real source readiness passed."

