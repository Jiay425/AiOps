param(
    [string]$Image = "apache/skywalking-java-agent:9.2.0-java17",
    [string]$TargetDir = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    $TargetDir = Join-Path $RepoRoot "docs\dev-ops\skywalking-agent"
}

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

Write-Host "Pulling SkyWalking Java Agent image: $Image"
docker pull $Image

$containerId = docker create $Image
try {
    Write-Host "Copying /skywalking/agent to $TargetDir"
    docker cp "$containerId`:/skywalking/agent/." $TargetDir
}
finally {
    docker rm $containerId | Out-Null
}

$AgentJar = Join-Path $TargetDir "skywalking-agent.jar"
if (!(Test-Path $AgentJar)) {
    throw "skywalking-agent.jar was not copied to $AgentJar"
}

Write-Host "SkyWalking Java Agent is ready: $AgentJar"
