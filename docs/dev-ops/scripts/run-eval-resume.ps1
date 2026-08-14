param(
    [string]$BaseUrl = "http://127.0.0.1:8099",
    [string]$StartedAtFile = "",
    [string]$MysqlExe = "D:\MySQL\MySQL Server 8.0\bin\mysql.exe",
    [string]$DbHost = "127.0.0.1",
    [string]$DbPort = "13306",
    [string]$DbName = "ops-autoagent-diagnosis",
    [string]$DbUser = "root",
    [string]$DbPassword = $env:OPS_EVAL_DB_PASSWORD
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "data\log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if ([string]::IsNullOrWhiteSpace($DbPassword)) {
    $DbPassword = "123456"
}

if ([string]::IsNullOrWhiteSpace($StartedAtFile)) {
    $StartedAtFile = Join-Path $logDir "eval-all-started-at.txt"
}

if (Test-Path $StartedAtFile) {
    $startedAt = (Get-Content $StartedAtFile -Raw).Trim()
} else {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $startedAt | Set-Content -Path $StartedAtFile -Encoding UTF8
}

$resumeStartedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$resumeStartedAt | Set-Content -Path (Join-Path $logDir "eval-resume-started-at.txt") -Encoding UTF8

function Invoke-MysqlScalarLines {
    param([string]$Sql)

    @(& $MysqlExe `
        --host=$DbHost `
        --port=$DbPort `
        --user=$DbUser `
        --password=$DbPassword `
        --default-character-set=utf8mb4 `
        --batch `
        --raw `
        --skip-column-names `
        --execute=$Sql `
        $DbName 2>$null)
}

function Get-FirstMysqlInt {
    param([string]$Sql)

    $lines = @(Invoke-MysqlScalarLines $Sql)
    foreach ($line in $lines) {
        $text = "$line".Trim()
        if ($text -match "^-?\d+$") {
            return [int]$text
        }
    }
    return 0
}

$caseIds = Invoke-MysqlScalarLines "SELECT case_id FROM ops_eval_case WHERE enabled = 1 ORDER BY id;"
$results = @()

foreach ($caseId in $caseIds) {
    if ([string]::IsNullOrWhiteSpace($caseId)) {
        continue
    }

    $escapedCaseId = $caseId.Replace("'", "''")
    $escapedStartedAt = $startedAt.Replace("'", "''")
    $successCount = Get-FirstMysqlInt "SELECT COUNT(*) FROM ops_eval_run WHERE case_id = '$escapedCaseId' AND status = 'SUCCESS' AND create_time >= '$escapedStartedAt';"

    if ($successCount -gt 0) {
        $results += [ordered]@{
            caseId = $caseId
            action = "SKIPPED_SUCCESS"
        }
        continue
    }

    $caseStart = Get-Date
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/ops/evaluation/run/$caseId" -TimeoutSec 900
        $caseEnd = Get-Date
        $results += [ordered]@{
            caseId = $caseId
            action = "RERUN"
            status = $response.status
            costSeconds = [math]::Round(($caseEnd - $caseStart).TotalSeconds, 1)
            response = $response
        }
    } catch {
        $caseEnd = Get-Date
        $results += [ordered]@{
            caseId = $caseId
            action = "RERUN_FAILED_REQUEST"
            costSeconds = [math]::Round(($caseEnd - $caseStart).TotalSeconds, 1)
            error = $_.Exception.Message
        }
    }

    $results | ConvertTo-Json -Depth 16 | Set-Content -Path (Join-Path $logDir "eval-resume-result.json") -Encoding UTF8
}

$results | ConvertTo-Json -Depth 16 | Set-Content -Path (Join-Path $logDir "eval-resume-result.json") -Encoding UTF8

