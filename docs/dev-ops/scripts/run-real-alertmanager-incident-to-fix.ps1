param(
    [ValidateSet("code-fix-5xx", "no-code-latency", "all")]
    [string]$Case = "all",
    [string]$EnvFile = "docs/dev-ops/runbook-rag.local.env",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

function Invoke-JsonGet([string]$Uri, [int]$TimeoutSec = 10) {
    Invoke-RestMethod -Method Get -Uri $Uri -TimeoutSec $TimeoutSec
}

function Wait-HttpReady([string]$Name, [string]$Uri, [int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-JsonGet $Uri 5 | Out-Null
            Write-Host "$Name ready: $Uri"
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "$Name did not become ready: $Uri"
}

function Wait-OrderAlert([string]$AlertName, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $prom = Invoke-JsonGet "http://127.0.0.1:9090/api/v1/alerts"
        $alert = @($prom.data.alerts | Where-Object {
            $_.labels.alertname -eq $AlertName -and $_.state -eq "firing"
        }) | Select-Object -First 1
        if ($alert) {
            Write-Host "Prometheus alert firing: $AlertName"
            Start-Sleep -Seconds 15
            $am = Invoke-JsonGet "http://127.0.0.1:9093/api/v2/alerts"
            $amAlert = @($am | Where-Object {
                $_.labels.alertname -eq $AlertName -and $_.status.state -eq "active"
            }) | Select-Object -First 1
            if ($amAlert) {
                Write-Host "Alertmanager alert active: $AlertName"
                return
            }
        }
        Start-Sleep -Seconds 5
    }
    throw "Alert did not become active: $AlertName"
}

function Wait-LatestIncidentTask([string]$AlertName, [int]$Seconds = 240) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $recent = Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/list/recent" 10
            $task = @($recent.data | Where-Object {
                $_.taskType -eq "INCIDENT_TO_FIX" -and $_.goal -like "*$AlertName*"
            }) | Select-Object -First 1
            if (-not $task) {
                $task = @($recent.data | Where-Object {
                    $_.taskType -eq "INCIDENT_TO_FIX" -and $_.goal -like "*order-service*"
                }) | Select-Object -First 1
            }
            if ($task) {
                Write-Host "Incident-to-Fix task observed: $($task.taskId), status=$($task.status)"
                return $task
            }
        } catch {
            Write-Host "Waiting for CodeOps task: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 8
    }
    throw "No Incident-to-Fix task found for alert: $AlertName"
}

function Wait-TaskTerminal([string]$TaskId, [int]$Seconds = 600) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $taskResponse = Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/$TaskId" 20
        $status = $taskResponse.data.status
        Write-Host "Task status: $TaskId -> $status"
        if ($status -in @("COMPLETED", "FAILED", "WAITING_APPROVAL")) {
            return $taskResponse.data
        }
        Start-Sleep -Seconds 10
    }
    throw "Task did not reach terminal status: $TaskId"
}

function Save-RunArtifacts([string]$CaseName, [object]$Task) {
    $repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $outDir = Join-Path $repoRoot "data\real-chain-runs\$stamp-$CaseName"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $taskId = $Task.taskId
    if ([string]::IsNullOrWhiteSpace($taskId)) {
        throw "Cannot save artifacts because taskId is blank."
    }
    Write-JsonArtifact (Join-Path $outDir "task.json") (Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/$taskId" 30) 20
    Write-JsonArtifact (Join-Path $outDir "trace.json") (Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/$taskId/trace" 30) 30
    Write-JsonArtifact (Join-Path $outDir "incident-view.json") (Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/incident/$taskId" 30) 30
    Write-JsonArtifact (Join-Path $outDir "security.json") (Invoke-JsonGet "http://127.0.0.1:8099/api/v1/codeops/task/$taskId/security" 30) 30

    Write-Host "Artifacts saved: $outDir"
}

function Write-JsonArtifact([string]$Path, [object]$Value, [int]$Depth) {
    if ($Value -is [string] -and $Value.TrimStart().StartsWith("{")) {
        Set-Content -Path $Path -Value $Value -Encoding UTF8
        return
    }
    $Value |
        ConvertTo-Json -Depth $Depth |
        Set-Content -Path $Path -Encoding UTF8
}

function Run-OneCase([string]$CaseName) {
    if ($CaseName -eq "code-fix-5xx") {
        $alertName = "OrderServiceHttp5xxHigh"
        & "$PSScriptRoot\inject-order-service-incidents.ps1" -Case code-fix-5xx -Requests 180 -BaseUrl "http://127.0.0.1:18081" | Select-Object -First 20
    } else {
        $alertName = "OrderServiceLatencyHigh"
        & "$PSScriptRoot\inject-order-service-incidents.ps1" -Case no-code-latency -Requests 50 -BaseUrl "http://127.0.0.1:18081" | Select-Object -First 20
    }

    Wait-OrderAlert $alertName
    $task = Wait-LatestIncidentTask $alertName
    $task = Wait-TaskTerminal $task.taskId
    Save-RunArtifacts $CaseName $task
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

Write-Host "Starting AutoAgent app with full profile..."
if ($SkipInstall) {
    & "$PSScriptRoot\start-runbook-rag-eval.ps1" -EnvFile $EnvFile -SkipInstall
} else {
    & "$PSScriptRoot\start-runbook-rag-eval.ps1" -EnvFile $EnvFile
}

Write-Host "Starting order-service if needed..."
try {
    Invoke-JsonGet "http://127.0.0.1:18081/actuator/health" 5 | Out-Null
    Write-Host "order-service already ready"
} catch {
    & "$PSScriptRoot\start-order-service-real.ps1" -Background
}

Wait-HttpReady "order-service" "http://127.0.0.1:18081/actuator/health"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:9090/-/reload" -TimeoutSec 10 | Out-Null
Wait-HttpReady "Prometheus" "http://127.0.0.1:9090/-/ready"
Wait-HttpReady "Alertmanager" "http://127.0.0.1:9093/-/ready"

if ($Case -eq "all") {
    Run-OneCase "no-code-latency"
    Run-OneCase "code-fix-5xx"
} else {
    Run-OneCase $Case
}
