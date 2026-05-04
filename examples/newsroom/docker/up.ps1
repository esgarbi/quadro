# up.ps1 — build and start the Quadro newsroom.
#
# Usage:
#   .\up.ps1                              # uses defaults from .env or docker-compose.yml
#   .\up.ps1 -Target 10                  # publish 10 articles
#   .\up.ps1 -Target 3 -Cycles 200
#   .\up.ps1 -Choreography sleep_study
#
# The Board UI is available at http://localhost:8080 once the container starts.
# Ctrl+C stops the logs but leaves the container running.
# Use .\down.ps1 to stop everything.

param(
    [int]    $Target        = 0,
    [int]    $Cycles        = 0,
    [string] $Choreography  = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Change to the directory containing this script
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ── Build env overrides ────────────────────────────────────────────────────────
$EnvOverrides = @{}
if ($Target -gt 0)          { $EnvOverrides["NEWSROOM_TARGET"]        = "$Target" }
if ($Cycles -gt 0)          { $EnvOverrides["NEWSROOM_CYCLES"]        = "$Cycles" }
if ($Choreography -ne "")   { $EnvOverrides["NEWSROOM_CHOREOGRAPHY"]  = $Choreography }

# Apply overrides to the current process environment
foreach ($kv in $EnvOverrides.GetEnumerator()) {
    [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value, "Process")
}

$UiPort = if ($env:UI_PORT) { $env:UI_PORT } else { "8080" }

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Quadro Newsroom" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Board UI   : http://localhost:$UiPort"
if ($Target -gt 0)        { Write-Host "  Target     : $Target articles" }
if ($Cycles -gt 0)        { Write-Host "  Cycles     : $Cycles" }
if ($Choreography -ne "") { Write-Host "  Choreography: $Choreography" }
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

docker compose up --build
