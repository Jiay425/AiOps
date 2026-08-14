param(
    [string]$CaseId = "scope-expansion-cross-file-idempotency",
    [string]$BaseUrl = "http://127.0.0.1:8099",
    [switch]$StartApp,
    [string]$EnvFile = "docs/dev-ops/runbook-rag.local.env",
    [switch]$SkipInstall,
    [switch]$SkipLlmPreflight
)

$ErrorActionPreference = "Stop"

function Load-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) {
        Write-Host "Env file not found: $Path. Falling back to current process environment."
        return
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $idx = $line.IndexOf("=")
        if ($idx -le 0) {
            return
        }
        $name = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Test-RequiredEnv([string[]]$Names) {
    $missing = @()
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ([string]::IsNullOrWhiteSpace($value)) {
            $missing += $name
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing required environment variables: $($missing -join ', '). Set them in the shell before running this script. Do not write secrets into repo files."
    }
}

function Get-EnvValue([string]$Name, [string]$Fallback = "") {
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Fallback
    }
    return $value
}

function Join-Url([string]$Root, [string]$Path) {
    $rootValue = ""
    if ($null -ne $Root) {
        $rootValue = $Root.Trim()
    }
    while ($rootValue.EndsWith("/")) {
        $rootValue = $rootValue.Substring(0, $rootValue.Length - 1)
    }
    $pathValue = "/chat/completions"
    if ($null -ne $Path -and -not [string]::IsNullOrWhiteSpace($Path)) {
        $pathValue = $Path.Trim()
    }
    if (-not $pathValue.StartsWith("/")) {
        $pathValue = "/" + $pathValue
    }
    return $rootValue + $pathValue
}

function Test-LlmPreflight() {
    $baseUrl = Get-EnvValue "OPENAI_BASE_URL"
    $apiKey = Get-EnvValue "OPENAI_API_KEY"
    $model = Get-EnvValue "OPENAI_CHAT_MODEL" "gpt-4o-mini"
    $chatPath = Get-EnvValue "CODEOPS_LLM_CHAT_PATH" "/chat/completions"
    $uri = Join-Url $baseUrl $chatPath
    $body = @{
        model = $model
        messages = @(@{
            role = "user"
            content = "Return OK."
        })
        temperature = 0
        max_tokens = 8
        stream = $false
    } | ConvertTo-Json -Depth 8

    try {
        Invoke-RestMethod `
            -Method Post `
            -Uri $uri `
            -Headers @{ Authorization = "Bearer $apiKey" } `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 60 | Out-Null
        Write-Host "LLM preflight OK: model=$model"
    } catch {
        $status = ""
        if ($_.Exception.Response) {
            $status = " HTTP " + [int]$_.Exception.Response.StatusCode
        }
        throw "LLM preflight failed$status. Check OPENAI_BASE_URL, OPENAI_CHAT_MODEL and account balance. Underlying error: $($_.Exception.Message)"
    }
}

function Wait-HttpReady([string]$Uri, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Method Get -Uri $Uri -TimeoutSec 5 | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "Application did not become ready: $Uri"
}

function Wait-CodeOpsReady([string]$BaseUrl, [int]$Seconds = 120) {
    $readyUri = "$BaseUrl/api/v1/codeops/task/list/recent"
    try {
        Wait-HttpReady $readyUri $Seconds
        return
    } catch {
        Write-Host "CodeOps readiness endpoint failed: $($_.Exception.Message)"
        Write-Host "Falling back to actuator health for diagnostic context."
        try {
            Invoke-WebRequest -Method Get -Uri "$BaseUrl/actuator/health" -TimeoutSec 5 | Out-Null
        } catch {
            Write-Host "Actuator health is not UP. This can be caused by optional health checks such as mail."
        }
        throw
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

Load-EnvFile (Join-Path $repoRoot $EnvFile)
Test-RequiredEnv @("OPENAI_API_KEY", "OPENAI_BASE_URL")

if (-not $SkipLlmPreflight) {
    Test-LlmPreflight
}

if ($StartApp) {
    $startArgs = @("-EnvFile", $EnvFile)
    if ($SkipInstall) {
        $startArgs += "-SkipInstall"
    }
    & "$PSScriptRoot\start-eval-app.ps1" @startArgs
}

Wait-CodeOpsReady $BaseUrl

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "$BaseUrl/api/v1/codeops/evaluation/run/$CaseId" `
    -TimeoutSec 1800

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outDir = Join-Path $repoRoot "data\codeops-real-llm-runs\$stamp-$CaseId"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$response | ConvertTo-Json -Depth 30 | Set-Content -Path (Join-Path $outDir "summary.json") -Encoding UTF8

try {
    $report = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/codeops/evaluation/report" -TimeoutSec 30
    $report | ConvertTo-Json -Depth 40 | Set-Content -Path (Join-Path $outDir "report.json") -Encoding UTF8
} catch {
    Write-Host "Report fetch skipped: $($_.Exception.Message)"
}

Write-Host "Case finished: $CaseId"
Write-Host "Artifacts: $outDir"
Write-Host ($response | ConvertTo-Json -Depth 8)
