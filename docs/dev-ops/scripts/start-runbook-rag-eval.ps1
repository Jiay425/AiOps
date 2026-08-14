param(
    [string]$EnvFile = "docs/dev-ops/runbook-rag.local.env",
    [switch]$RunEval,
    [switch]$RebuildVector,
    [switch]$SkipInstall
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

function Require-Env([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing required environment variable: $Name"
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

if (Test-Path "D:\Java\jdk17") {
    $env:JAVA_HOME = "D:\Java\jdk17"
} elseif (-not $env:JAVA_HOME) {
    throw "JAVA_HOME is not set and D:\Java\jdk17 does not exist"
}
$env:Path = "$env:JAVA_HOME\bin;$env:Path"

Load-EnvFile (Join-Path $repoRoot $EnvFile)
Require-Env "OPENAI_API_KEY"
Require-Env "OPS_RUNBOOK_EMBEDDING_API_KEY"
Require-Env "OPS_RUNBOOK_RERANK_API_KEY"

if ($RebuildVector) {
    $env:OPS_RUNBOOK_VECTOR_REBUILD_ON_STARTUP = "true"
}
$env:OPS_RUNBOOK_VECTOR_SCHEMA_CHECK_ON_STARTUP = "true"

$logDir = Join-Path $repoRoot "data\log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$appOut = Join-Path $logDir "runbook-rag-app.out.log"
$appErr = Join-Path $logDir "runbook-rag-app.err.log"

$listener = Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Write-Host "Stopping existing app process on 8099: $($listener.OwningProcess)"
    Stop-Process -Id $listener.OwningProcess -Force
    Start-Sleep -Seconds 2
}

if (-not $SkipInstall) {
    Write-Host "Installing multi-module project to avoid stale local dependencies..."
    mvn -q -DskipTests install
}

Write-Host "Packaging app jar..."
mvn -q -pl ops-autoagent-app -am -DskipTests package

Write-Host "Starting app jar with full profile..."
$appJar = Join-Path $repoRoot "ops-autoagent-app\target\ops-autoagent-app.jar"
if (-not (Test-Path $appJar)) {
    throw "App jar not found: $appJar"
}
Start-Process -FilePath "java" `
    -ArgumentList @("-jar", $appJar, "--spring.profiles.active=full") `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $appOut `
    -RedirectStandardError $appErr `
    -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try {
        Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8099/actuator/health" -TimeoutSec 5 | Out-Null
        $ready = $true
        break
    } catch {
        $started = Select-String -Path $appOut -Pattern "Started Application" -Quiet -ErrorAction SilentlyContinue
        if ($started) {
            $ready = $true
            break
        }
    }
}

if (-not $ready) {
    Write-Host "App did not become ready. Last logs:"
    Get-Content $appOut -Tail 80 -ErrorAction SilentlyContinue
    Get-Content $appErr -Tail 80 -ErrorAction SilentlyContinue
    throw "App startup failed"
}

Write-Host "App is ready on http://127.0.0.1:8099"

if ($RunEval) {
    Write-Host "Running Runbook RAG eval..."
    $result = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8099/api/v1/ops/evaluation/runbook-rag/run" -TimeoutSec 600
    $result | ConvertTo-Json -Depth 16 | Set-Content -Path (Join-Path $logDir "runbook-rag-eval-result.json") -Encoding UTF8
    $data = $result.data
    Write-Host "Runbook RAG eval done: cases=$($data.totalCases), success=$($data.successCases), top3=$($data.top3Recall), mrr=$($data.meanReciprocalRank)"
    Write-Host "Report: $($data.reportMarkdownPath)"
}
