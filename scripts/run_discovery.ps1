# NIRIKSHA — Run the discovery client + HTTP verifier (Process 3)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_discovery.ps1

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"

Write-Host "[DISCOVERY] Starting NIRIKSHA Discovery Client ..." -ForegroundColor Green
Write-Host "[DISCOVERY] PYTHONPATH = $env:PYTHONPATH" -ForegroundColor DarkGray

& "$root\.venv\Scripts\python.exe" -m niriksha.discovery --timeout 30
