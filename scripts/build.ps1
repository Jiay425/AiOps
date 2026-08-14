param([string]$Image = 'system/ops-autoagent-app:2.0.0')
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
docker build --tag $Image --file (Join-Path $workspace 'Dockerfile') $workspace
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
