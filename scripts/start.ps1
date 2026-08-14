param(
    [string]$Python = 'python'
)

$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location $workspace
try {
    & $Python -m ops_autoagent.main
} finally {
    Pop-Location
}
