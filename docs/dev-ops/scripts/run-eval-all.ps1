$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

$logDir = Join-Path $repoRoot "data\log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$startedAt | Set-Content -Path (Join-Path $logDir "eval-all-started-at.txt") -Encoding UTF8

$result = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8099/api/v1/ops/evaluation/run" -TimeoutSec 7200
$result | ConvertTo-Json -Depth 16 | Set-Content -Path (Join-Path $logDir "eval-all-result.json") -Encoding UTF8
