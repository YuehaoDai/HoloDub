param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseDumpPath
)

$ErrorActionPreference = "Stop"

Get-Content $DatabaseDumpPath | docker compose exec -T postgres sh -lc "psql -U holodub -d holodub"
Write-Host "Database restore completed from $DatabaseDumpPath"
