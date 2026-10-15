param(
    [ValidateSet("code-fix-5xx", "no-code-latency")]
    [string]$Case = "no-code-latency",
    [string]$AppBaseUrl = "http://127.0.0.1:8099",
    [string]$OrderBaseUrl = "http://127.0.0.1:18081",
    [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$alertName = if ($Case -eq "code-fix-5xx") { "OrderServiceHttp5xxHigh" } else { "OrderServiceLatencyHigh" }
$runStarted = (Get-Date).ToUniversalTime().ToString("o")

function Get-Json([string]$Uri) {
    Invoke-RestMethod -Method Get -Uri $Uri -TimeoutSec 20
}

function Find-Alert([object]$Items, [string]$Name) {
    @($Items | Where-Object { $_.labels.alertname -eq $Name }) | Select-Object -First 1
}

# The script never posts an alert.  It exercises the sample service; only the
# Prometheus rule and Alertmanager are permitted to trigger the webhook.
Get-Json "$AppBaseUrl/actuator/health" | Out-Null
Get-Json "$OrderBaseUrl/actuator/health" | Out-Null
Get-Json "http://127.0.0.1:9090/-/ready" | Out-Null
Get-Json "http://127.0.0.1:9093/-/ready" | Out-Null
$beforeResponse = Get-Json "$AppBaseUrl/api/v1/codeops/task/list/recent"
$before = @($beforeResponse.data | ForEach-Object { $_.taskId })
$promBefore = Get-Json "http://127.0.0.1:9090/api/v1/alerts"
$alreadyFiring = Find-Alert @($promBefore.data.alerts | Where-Object { $_.state -eq "firing" }) $alertName
if ($alreadyFiring) {
    throw "$alertName is already firing; wait for resolution before starting a fresh verification"
}

$requests = if ($Case -eq "code-fix-5xx") { 180 } else { 50 }
& "$PSScriptRoot\inject-order-service-incidents.ps1" -Case $Case -Requests $requests -BaseUrl $OrderBaseUrl

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$promAlert = $null
$amAlert = $null
$task = $null
while ((Get-Date) -lt $deadline) {
    $prom = Get-Json "http://127.0.0.1:9090/api/v1/alerts"
    $candidate = Find-Alert @($prom.data.alerts | Where-Object { $_.state -eq "firing" }) $alertName
    if ($candidate) { $promAlert = $candidate }
    $am = Get-Json "http://127.0.0.1:9093/api/v2/alerts"
    $candidate = Find-Alert @($am | Where-Object { $_.status.state -eq "active" }) $alertName
    if ($candidate) { $amAlert = $candidate }
    if ($promAlert -and $amAlert) {
        $recent = Get-Json "$AppBaseUrl/api/v1/codeops/task/list/recent"
        $task = @($recent.data | Where-Object {
            $_.taskId -notin $before -and $_.taskType -eq "INCIDENT_TO_FIX" -and $_.goal -like "*$alertName*"
        }) | Select-Object -First 1
        if ($task) { break }
    }
    Start-Sleep -Seconds 5
}
if (-not $promAlert) { throw "Prometheus never fired $alertName" }
if (-not $amAlert) { throw "Alertmanager did not receive $alertName" }
if (-not $task) { throw "No new CodeOps task was created from $alertName" }

$taskId = $task.taskId
while ((Get-Date) -lt $deadline) {
    $response = Get-Json "$AppBaseUrl/api/v1/codeops/task/$taskId"
    $task = $response.data
    if ($task.status -notin @("RUNNING", "QUEUED")) { break }
    Start-Sleep -Seconds 10
}
if ($task.status -in @("RUNNING", "QUEUED")) { throw "Task $taskId did not stop within the timeout" }

$view = Get-Json "$AppBaseUrl/api/v1/codeops/task/incident/$taskId"
$trace = Get-Json "$AppBaseUrl/api/v1/codeops/task/$taskId/trace"
$observability = Get-Json "$AppBaseUrl/api/v1/codeops/task/$taskId/observability"
$incident = $view.data.incident
$outDir = Join-Path $repoRoot ("data\local-alert-chain\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-$Case")
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
@{ prometheus = $promAlert; alertmanager = $amAlert } |
    ConvertTo-Json -Depth 16 | Set-Content (Join-Path $outDir "alerts.json") -Encoding UTF8
$view | ConvertTo-Json -Depth 30 | Set-Content (Join-Path $outDir "incident-view.json") -Encoding UTF8
$trace | ConvertTo-Json -Depth 30 | Set-Content (Join-Path $outDir "trace.json") -Encoding UTF8
$observability | ConvertTo-Json -Depth 30 | Set-Content (Join-Path $outDir "observability.json") -Encoding UTF8
if ($incident.source -ne "alertmanager" -or [string]::IsNullOrWhiteSpace($incident.incidentId) -or
    $incident.evidenceMode -ne "LIVE" -or $incident.fixtureFallbackAllowed -eq $true) {
    throw "Task $taskId is not linked to a live Alertmanager Incident"
}
$coverage = $view.data.artifacts.evidence.evidenceCoverage
if (-not $coverage -or $coverage.fixtureFallbackUsed -or [double]$coverage.realEvidenceCoverage -lt 0.67) {
    throw "Task $taskId has no verified live evidence; refusing to call this a real-chain pass"
}
$patch = $view.data.artifacts.patch
if ($Case -eq "no-code-latency") {
    $repairSteps = @($task.steps | Where-Object { $_.selectedSkill -eq "bug_fix" })
    if ($task.status -ne "NO_CODE_FIX" -or $patch.patchGenerated -eq $true -or
        $patch.llmGenerated -eq $true -or $repairSteps.Count -gt 0) {
        throw "No-code incident $taskId did not stop without a patch"
    }
} else {
    if ($task.status -notin @("WAITING_APPROVAL", "COMPLETED") -or
        ($patch.patchGenerated -ne $true -and $patch.llmGenerated -ne $true) -or
        $patch.patchScopeGuard.passed -ne $true -or
        $view.data.guardrails.testsPassed -ne $true) {
        throw "Code-fix incident $taskId did not produce a scoped, verified patch"
    }
}

@{ case = $Case; runStarted = $runStarted; alertName = $alertName; taskId = $taskId;
   taskStatus = $task.status; evidenceCoverage = $coverage; source = "LOCAL_LIVE_MONITORING" } |
    ConvertTo-Json -Depth 12 | Set-Content (Join-Path $outDir "result.json") -Encoding UTF8
Write-Host "Local monitoring chain verified: alert=$alertName task=$taskId status=$($task.status)"
Write-Host "Evidence: $outDir"
