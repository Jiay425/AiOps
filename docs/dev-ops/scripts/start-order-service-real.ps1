param(
    [string]$JavaHome = "D:\Java\jdk17",
    [string]$Port = "18081",
    [string]$SkyWalkingAgentPath = "",
    [string]$SkyWalkingBackend = "127.0.0.1:11800",
    [switch]$Background
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$OrderServiceDir = Join-Path $RepoRoot "samples\order-service"
$LogDir = Join-Path $RepoRoot "data\log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ($Background) {
    $out = Join-Path $LogDir "order-service-console.out.log"
    $err = Join-Path $LogDir "order-service-console.err.log"
    $args = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $PSCommandPath,
        "-JavaHome",
        $JavaHome,
        "-Port",
        $Port,
        "-SkyWalkingBackend",
        $SkyWalkingBackend
    )
    if (-not [string]::IsNullOrWhiteSpace($SkyWalkingAgentPath)) {
        $args += @("-SkyWalkingAgentPath", $SkyWalkingAgentPath)
    }
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList $args `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "order-service background process started: $($process.Id)"
    Write-Host "stdout: $out"
    Write-Host "stderr: $err"
    return
}

if (Test-Path $JavaHome) {
    $env:JAVA_HOME = $JavaHome
    $env:Path = "$env:JAVA_HOME\bin;$env:Path"
}

$env:ORDER_SERVICE_PORT = $Port
$env:ORDER_SERVICE_LOG_FILE = Join-Path $LogDir "log_error.log"

$JvmArguments = ""
if (-not [string]::IsNullOrWhiteSpace($SkyWalkingAgentPath)) {
    if (!(Test-Path $SkyWalkingAgentPath)) {
        throw "SkyWalking agent jar not found: $SkyWalkingAgentPath"
    }
    $JvmArguments = @(
        "-javaagent:$SkyWalkingAgentPath",
        "-Dskywalking.agent.service_name=order-service",
        "-Dskywalking.collector.backend_service=$SkyWalkingBackend",
        "-Dskywalking.agent.instance_name=local-order-service-$Port"
    ) -join " "
}

Set-Location $OrderServiceDir

Write-Host "Starting real order-service"
Write-Host "Port: $Port"
Write-Host "Log: $env:ORDER_SERVICE_LOG_FILE"
if (-not [string]::IsNullOrWhiteSpace($JvmArguments)) {
    Write-Host "SkyWalking: enabled"
}

if ([string]::IsNullOrWhiteSpace($JvmArguments)) {
    mvn spring-boot:run
} else {
    mvn spring-boot:run "-Dspring-boot.run.jvmArguments=$JvmArguments"
}
