param(
    [ValidateSet("code-fix-5xx", "no-code-latency", "all")]
    [string]$Case = "all",
    [string]$EnvFile = "docs/dev-ops/runbook-rag.local.env",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Write-Host "This compatibility entry now verifies the local Prometheus -> Alertmanager -> CodeOps chain."
Write-Host "Start the compose stack and Java sample service first; EnvFile and SkipInstall are retained only for CLI compatibility."
$cases = if ($Case -eq "all") { @("no-code-latency", "code-fix-5xx") } else { @($Case) }
foreach ($selectedCase in $cases) {
    & "$PSScriptRoot\verify-local-alert-chain.ps1" -Case $selectedCase
    if (-not $?) { throw "Local alert chain verification failed: $selectedCase" }
}
