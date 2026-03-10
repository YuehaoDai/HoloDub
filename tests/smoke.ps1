param(
    [switch]$BuildImages
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "==> Go tests"
go test ./...

Write-Host "==> Python venv"
if (-not (Test-Path ".\.venv-smoke\Scripts\python.exe")) {
    python -m venv .venv-smoke
}

Write-Host "==> Python deps"
.\.venv-smoke\Scripts\python -m pip install -e .\ml_service[dev]

Write-Host "==> Python tests"
.\.venv-smoke\Scripts\python -m pytest ml_service/tests

Write-Host "==> Compose config"
$env:COMPOSE_ENV_FILE = ".env.example"
docker compose --env-file .env.example config | Out-Null

if ($BuildImages) {
    Write-Host "==> Compose build"
    docker compose --env-file .env.example build api worker ml-service
}

Write-Host "Smoke checks completed."
