param(
    [switch]$IncludeDatabase
)

$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$targets = Get-ChildItem -LiteralPath $workspace -Directory -Recurse -Force |
    Where-Object { $_.Name -in @('__pycache__', '.pytest_cache', '.ruff_cache', 'build', 'dist') }

foreach ($target in $targets) {
    $resolved = $target.FullName
    if (-not $resolved.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to clean path outside workspace: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

Get-ChildItem -LiteralPath $workspace -Filter '*.egg-info' -Directory -Recurse | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

if ($IncludeDatabase) {
    $database = Join-Path $workspace '.data\ops-autoagent.db'
    if (Test-Path -LiteralPath $database) {
        Remove-Item -LiteralPath $database -Force
    }
}

Write-Output 'Python build and test artifacts cleaned.'

