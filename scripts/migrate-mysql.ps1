param(
    [Parameter(Mandatory=$true)][string]$DatabaseHost,
    [int]$Port = 3306,
    [string]$User = 'root',
    [Parameter(Mandatory=$true)][SecureString]$Password
)
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sqlRoot = Join-Path $workspace 'docs\dev-ops\mysql\sql'
$plain = [System.Net.NetworkCredential]::new('', $Password).Password
try {
    Get-ChildItem -LiteralPath $sqlRoot -Filter '*.sql' | Sort-Object Name | ForEach-Object {
        Get-Content -Raw -LiteralPath $_.FullName | mysql --host=$DatabaseHost --port=$Port --user=$User "--password=$plain"
        if ($LASTEXITCODE -ne 0) { throw "MySQL migration failed: $($_.Name)" }
    }
} finally {
    $plain = $null
}
