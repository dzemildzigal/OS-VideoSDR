param(
    [string]$OutputRoot = "artifacts/metrics"
)

$ErrorActionPreference = "Stop"

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outDir = Join-Path $OutputRoot $timestamp
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

Write-Host "Metrics collection scaffold"
Write-Host "Created metrics output directory: $outDir"
Write-Host "TODO: copy runtime CSV/JSON reports into this directory."
