param(
    [string]$OutputDir = ".\backups"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dbDumpPath = Join-Path $OutputDir "postgres-$timestamp.sql"
$dataArchivePath = Join-Path $OutputDir "data-$timestamp.zip"

docker compose exec -T postgres sh -lc "pg_dump -U holodub -d holodub" | Out-File -Encoding utf8 $dbDumpPath
if (Test-Path ".\data") {
    Compress-Archive -Path ".\data\*" -DestinationPath $dataArchivePath -Force
}

Write-Host "Database dump: $dbDumpPath"
Write-Host "Data archive:   $dataArchivePath"
