param(
    [switch]$Build,
    [switch]$Open
)

$ErrorActionPreference = "Stop"

$RootDir = [string](Resolve-Path (Join-Path $PSScriptRoot ".."))
$ImageName = if ($env:FINALLY_IMAGE) { $env:FINALLY_IMAGE } else { "finally:local" }
$ContainerName = if ($env:FINALLY_CONTAINER) { $env:FINALLY_CONTAINER } else { "finally" }
$VolumeName = if ($env:FINALLY_VOLUME) { $env:FINALLY_VOLUME } else { "finally-data" }
$Port = if ($env:FINALLY_PORT) { $env:FINALLY_PORT } elseif ($env:APP_PORT) { $env:APP_PORT } else { "8000" }
$EnvFile = Join-Path $RootDir ".env"
$EnvExample = Join-Path $RootDir ".env.example"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required but was not found on PATH."
}

if (-not (Test-Path $EnvFile) -and (Test-Path $EnvExample)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "Created .env from .env.example"
}

if (-not (Test-Path $EnvFile)) {
    throw "Missing .env. Create one from .env.example before starting FinAlly."
}

docker image inspect $ImageName *> $null
$ImageExists = $LASTEXITCODE -eq 0

if ($Build -or -not $ImageExists) {
    docker build -t $ImageName $RootDir
}

$ExistingId = docker ps -aq -f "name=^/$ContainerName$"
$RunningId = docker ps -q -f "name=^/$ContainerName$"

if ($RunningId) {
    if ($Build) {
        docker stop $ContainerName *> $null
        docker rm $ContainerName *> $null
    }
    else {
        Write-Host "FinAlly is already running at http://localhost:$Port"
        exit 0
    }
}
elseif ($ExistingId) {
    if ($Build) {
        docker rm $ContainerName *> $null
    }
    else {
        docker start $ContainerName *> $null
        Write-Host "FinAlly started at http://localhost:$Port"
        if ($Open) {
            Start-Process "http://localhost:$Port"
        }
        exit 0
    }
}

docker volume create $VolumeName *> $null
docker run -d `
    --name $ContainerName `
    --env-file $EnvFile `
    -e DB_PATH=/app/db/finally.db `
    -p "${Port}:8000" `
    -v "${VolumeName}:/app/db" `
    $ImageName *> $null

Write-Host "FinAlly started at http://localhost:$Port"

if ($Open) {
    Start-Process "http://localhost:$Port"
}
