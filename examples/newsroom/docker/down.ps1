# down.ps1 — stop the Quadro newsroom containers.
#
# Usage:
#   .\down.ps1           # stop containers, keep volumes (articles + model weights)
#   .\down.ps1 -Clean    # stop containers AND remove all volumes (fresh slate)

param(
    [switch] $Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

if ($Clean) {
    Write-Host "Stopping containers and removing volumes..." -ForegroundColor Yellow
    docker compose down --volumes
    Write-Host "Done. Model weights and articles have been removed." -ForegroundColor Green
} else {
    Write-Host "Stopping containers (volumes retained)..." -ForegroundColor Yellow
    docker compose down
    Write-Host "Done. Articles and model weights are preserved in Docker volumes." -ForegroundColor Green
    Write-Host "Run '.\down.ps1 -Clean' to remove them."
}
