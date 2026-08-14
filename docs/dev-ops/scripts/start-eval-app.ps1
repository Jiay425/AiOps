$ErrorActionPreference = "Stop"

$script = Join-Path $PSScriptRoot "start-runbook-rag-eval.ps1"
& $script @args
