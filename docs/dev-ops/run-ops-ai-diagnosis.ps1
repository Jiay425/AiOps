param(
    [string]$AppUrl = "http://127.0.0.1:8099",
    [string]$ElasticsearchUrl = "http://127.0.0.1:9200",
    [string]$ServiceName = "ops-demo-service",
    [string]$TraceId = "trace-ops-ai-diagnosis-001",
    [int]$PrometheusWaitSeconds = 20
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startTime = (Get-Date).AddMinutes(-10).ToString("yyyy-MM-dd HH:mm:ss")
$seedTime = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")

Write-Host "1. Check application environment..."
$environment = Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/environment"
$environment | ConvertTo-Json -Depth 10

Write-Host "2. Seed real Elasticsearch fault evidence..."
& "$scriptDir/seed-elk-ops-evidence.ps1" `
    -ElasticsearchUrl $ElasticsearchUrl `
    -ServiceName $ServiceName `
    -TraceId $TraceId `
    -Timestamp $seedTime

Write-Host "3. Generate real application traffic for Prometheus and SkyWalking..."
1..12 | ForEach-Object {
    try {
        Invoke-WebRequest -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=error" -UseBasicParsing | Out-Null
    } catch {
        # 500 responses are expected for error-mode traffic.
    }
}

1..6 | ForEach-Object {
    Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=slow&sleepMillis=1600" | Out-Null
}

1..3 | ForEach-Object {
    Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/mock/order/create?mode=db&holdSeconds=3" | Out-Null
}

Write-Host "4. Wait $PrometheusWaitSeconds seconds for Prometheus scrape and SkyWalking flush..."
Start-Sleep -Seconds $PrometheusWaitSeconds

$endTime = (Get-Date).AddMinutes(1).ToString("yyyy-MM-dd HH:mm:ss")
$body = @{
    serviceName = $ServiceName
    startTime = $startTime
    endTime = $endTime
    problem = "$ServiceName 在最近 10 分钟出现大量 500 错误，请通过 MCP/Prometheus、MCP/ELK、SkyWalking 和 PgVector Runbook 分析原因并给出修复建议。"
    traceId = $TraceId
    maxStep = 7
} | ConvertTo-Json -Depth 8

Write-Host "5. Start AI incident diagnosis SSE..."
$sseResponse = Invoke-WebRequest `
    -Method Post `
    -Uri "$AppUrl/api/v1/ops/incident/analyze" `
    -ContentType "application/json" `
    -Body $body `
    -UseBasicParsing `
    -TimeoutSec 600

$sseText = $sseResponse.Content
Write-Host "6. Diagnosis SSE output:"
$sseText

$diagnosisId = $null
if ($sseText -match "diagnosisId=(diag-[0-9a-fA-F-]+)") {
    $diagnosisId = $Matches[1]
}

if ([string]::IsNullOrWhiteSpace($diagnosisId)) {
    throw "Diagnosis completed but diagnosisId was not found in SSE output."
}

Write-Host "7. Query persisted diagnosis record: $diagnosisId"
$record = Invoke-RestMethod -Method Get -Uri "$AppUrl/api/v1/ops/incident/record/$diagnosisId"
$record | ConvertTo-Json -Depth 12

if ($record.code -ne "0000") {
    throw "Diagnosis record query failed: $($record.info)"
}

$data = $record.data
$checks = [ordered]@{
    diagnosisId = $diagnosisId
    status = $data.status
    hasMetricEvidence = -not [string]::IsNullOrWhiteSpace($data.metricEvidenceJson)
    hasLogEvidence = -not [string]::IsNullOrWhiteSpace($data.logEvidenceJson)
    hasTraceEvidence = -not [string]::IsNullOrWhiteSpace($data.traceEvidenceJson)
    hasEvidenceChain = -not [string]::IsNullOrWhiteSpace($data.evidenceChainJson)
    hasRunbook = -not [string]::IsNullOrWhiteSpace($data.runbookJson)
    hasReport = -not [string]::IsNullOrWhiteSpace($data.report)
}

Write-Host "8. Diagnosis quality checks:"
$checks | ConvertTo-Json -Depth 4

if ($data.status -ne "SUCCESS") {
    throw "Diagnosis status is not SUCCESS: $($data.status)"
}

if (-not $checks.hasEvidenceChain -or -not $checks.hasReport) {
    throw "Diagnosis did not persist evidence chain or report."
}

Write-Host "AI diagnosis flow passed. diagnosisId=$diagnosisId"

