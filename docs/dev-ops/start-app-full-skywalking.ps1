param(
    [string]$JavaHome = "D:\Java\jdk17",
    [string]$ServiceName = "ops-demo-service",
    [string]$BackendService = "127.0.0.1:11800",
    [string]$InstanceName = "local-full-8099",
    [string]$AgentPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if ([string]::IsNullOrWhiteSpace($AgentPath)) {
    $AgentPath = Join-Path $RepoRoot "docs\dev-ops\skywalking-agent\skywalking-agent.jar"
}

if (!(Test-Path $AgentPath)) {
    throw "SkyWalking agent jar not found: $AgentPath. Run docs\dev-ops\prepare-skywalking-agent.ps1 first."
}

if (Test-Path $JavaHome) {
    $env:JAVA_HOME = $JavaHome
    $env:Path = "$env:JAVA_HOME\bin;$env:Path"
}

$JvmArguments = @(
    "-javaagent:$AgentPath",
    "-Dskywalking.agent.service_name=$ServiceName",
    "-Dskywalking.collector.backend_service=$BackendService",
    "-Dskywalking.agent.instance_name=$InstanceName"
) -join " "

Set-Location $RepoRoot

Write-Host "Starting Spring Boot with SkyWalking Java Agent"
Write-Host "Agent: $AgentPath"
Write-Host "Service: $ServiceName"
Write-Host "Backend: $BackendService"

mvn -pl ops-autoagent-app -am spring-boot:run `
    "-Dspring-boot.run.profiles=full" `
    "-Dspring-boot.run.jvmArguments=$JvmArguments"

