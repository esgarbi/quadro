# up.ps1 — build and start the Quadro ordering system.
#
# Usage:
#   .\up.ps1                              # uses defaults from .env or docker-compose.yml
#   .\up.ps1 -Target 5                    # ship 5 orders
#   .\up.ps1 -Target 3 -Cycles 200
#   .\up.ps1 -Profile burst
#   .\up.ps1 -Choreography wave_study
#
# The Board UI is available at http://localhost:8081 once the container starts.
# Ctrl+C stops the logs but leaves the container running.
# Use .\down.ps1 to stop everything.

param(
    [int]    $Target        = 0,
    [int]    $Cycles        = 0,
    [string] $Profile       = "",
    [string] $Choreography  = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ── Build env overrides ────────────────────────────────────────────────────────
$EnvOverrides = @{}
if ($Target -gt 0)          { $EnvOverrides["ORDERING_TARGET"]         = "$Target" }
if ($Cycles -gt 0)          { $EnvOverrides["ORDERING_CYCLES"]         = "$Cycles" }
if ($Profile -ne "")        { $EnvOverrides["ORDERING_PROFILE"]        = $Profile }
if ($Choreography -ne "")   { $EnvOverrides["ORDERING_CHOREOGRAPHY"]   = $Choreography }

foreach ($kv in $EnvOverrides.GetEnumerator()) {
    [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value, "Process")
}

$UiPort = if ($env:UI_PORT) { $env:UI_PORT } else { "8081" }

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Quadro Ordering System" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Board UI   : http://localhost:$UiPort"
if ($Target -gt 0)        { Write-Host "  Target     : $Target orders" }
if ($Cycles -gt 0)        { Write-Host "  Cycles     : $Cycles" }
if ($Profile -ne "")      { Write-Host "  Profile    : $Profile" }
if ($Choreography -ne "") { Write-Host "  Choreography: $Choreography" }
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

docker compose up --build
