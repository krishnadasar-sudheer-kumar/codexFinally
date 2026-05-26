$ErrorActionPreference = "Stop"

$ContainerName = if ($env:FINALLY_CONTAINER) { $env:FINALLY_CONTAINER } else { "finally" }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required but was not found on PATH."
}

$RunningId = docker ps -q -f "name=^/$ContainerName$"
$ExistingId = docker ps -aq -f "name=^/$ContainerName$"

if ($RunningId) {
    docker stop $ContainerName *> $null
}

if ($ExistingId) {
    docker rm $ContainerName *> $null
    Write-Host "Stopped FinAlly container. Data volume was preserved."
}
else {
    Write-Host "FinAlly container is not running."
}
