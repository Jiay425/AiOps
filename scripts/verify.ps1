param([string]$Python = 'python')
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location $workspace
try {
    & $Python -m compileall -q src tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python scripts/smoke_startup.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python scripts/audit_http_parity.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python scripts/audit_config_parity.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python scripts/audit_feature_parity.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python scripts/run_full_parity.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
