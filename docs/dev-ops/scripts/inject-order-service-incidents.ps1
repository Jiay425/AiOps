param(
    [string]$BaseUrl = "http://127.0.0.1:18081",
    [ValidateSet("no-code-latency", "code-fix-5xx")]
    [string]$Case = "code-fix-5xx",
    [int]$Requests = 80
)

$ErrorActionPreference = "Stop"

if ($Case -eq "no-code-latency") {
    for ($i = 0; $i -lt $Requests; $i++) {
        try {
            Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/orders/dependency-latency?durationMs=1500" -TimeoutSec 10 | Out-Null
        } catch {
            Write-Host "latency request failed: $($_.Exception.Message)"
        }
    }
    exit 0
}

$body = @{
    skuId = "sku-2001"
    quantity = 1
    unitPrice = "19.90"
} | ConvertTo-Json

for ($i = 0; $i -lt $Requests; $i++) {
    try {
        Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/orders/submit" -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
    } catch {
        Write-Host "5xx request observed: $($_.Exception.Message)"
    }
}
