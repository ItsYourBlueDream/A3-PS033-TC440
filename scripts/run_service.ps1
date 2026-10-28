# NIRIKSHA — Run the diagnostic HTTP service (Process 1)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_service.ps1

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"

Write-Host "[SERVICE] Starting NIRIKSHA Diagnostic HTTP Service on port 8001 ..." -ForegroundColor Cyan
Write-Host "[SERVICE] PYTHONPATH = $env:PYTHONPATH" -ForegroundColor DarkGray

& "$root\.venv\Scripts\uvicorn.exe" niriksha.service:app `
    --host 0.0.0.0 `
    --port 8001 `
    --log-level info
