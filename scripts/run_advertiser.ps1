# NIRIKSHA — Run the DNS-SD advertiser (Process 2)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_advertiser.ps1

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"

Write-Host "[ADVERTISER] Starting NIRIKSHA DNS-SD Advertiser ..." -ForegroundColor Magenta
Write-Host "[ADVERTISER] PYTHONPATH = $env:PYTHONPATH" -ForegroundColor DarkGray

& "$root\.venv\Scripts\python.exe" -m niriksha.advertiser --port 8001
