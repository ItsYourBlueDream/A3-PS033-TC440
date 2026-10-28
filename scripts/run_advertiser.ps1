# NIRIKSHA — Run the DNS-SD advertiser (Process 2)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_advertiser.ps1 [-Port 8001] [-InjectPort 9999] [-InjectDiag http] [-MigrateTo 8002]
param(
    [int]$Port = 8001,
    [int]$InjectPort = 0,
    [string]$InjectDiag = "",
    [int]$MigrateTo = 0,
    [double]$MigrateDelay = 10.0,
    [string]$MigrateMode = "update"
)

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"

Write-Host "[ADVERTISER] Starting NIRIKSHA DNS-SD Advertiser ..." -ForegroundColor Magenta
Write-Host "[ADVERTISER] PYTHONPATH = $env:PYTHONPATH" -ForegroundColor DarkGray

$argsList = @("-m", "niriksha.advertiser", "--port", "$Port")
if ($InjectPort -gt 0) { $argsList += @("--inject-port", "$InjectPort") }
if ($InjectDiag -ne "") { $argsList += @("--inject-diag", "$InjectDiag") }
if ($MigrateTo -gt 0) { $argsList += @("--migrate-to", "$MigrateTo", "--migrate-delay", "$MigrateDelay", "--migrate-mode", "$MigrateMode") }

& "$root\.venv\Scripts\python.exe" $argsList
