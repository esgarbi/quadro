# down.ps1 — stop the Quadro ordering system containers.
#
# Usage:
#   .\down.ps1           # stop containers
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
    Write-Host "Done." -ForegroundColor Green
} else {
    Write-Host "Stopping containers..." -ForegroundColor Yellow
    docker compose down
    Write-Host "Done." -ForegroundColor Green
    Write-Host "Run '.\down.ps1 -Clean' to also remove volumes."
}
