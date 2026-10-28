# NIRIKSHA Phase 2A - Run the continuous monitor (Process 4)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_monitor.ps1

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"

Write-Host "[MONITOR] Starting NIRIKSHA Continuous Monitor ..." -ForegroundColor Yellow
Write-Host "[MONITOR] PYTHONPATH = $env:PYTHONPATH" -ForegroundColor DarkGray
Write-Host "[MONITOR] Log file   : $root\niriksha_events.jsonl" -ForegroundColor DarkGray

& "$root\.venv\Scripts\python.exe" -m niriksha.monitor_runner `
    --interval 10 `
    --timeout 8 `
    --log "$root\niriksha_events.jsonl" `
    --discovery-wait 15
