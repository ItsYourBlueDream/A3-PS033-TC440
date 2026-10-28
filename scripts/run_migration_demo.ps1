# NIRIKSHA Phase 2B - Automated Live Migration Demonstration
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_migration_demo.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"
$py = "$root\.venv\Scripts\python.exe"
$uvicorn = "$root\.venv\Scripts\uvicorn.exe"
$logFile = "$root\niriksha_events.jsonl"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " NIRIKSHA Phase 2B: Live DNS-SD Endpoint Migration Demo     " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Log file: $logFile" -ForegroundColor DarkGray

# 1. Start Service A (port 8001)
Write-Host "`n[DEMO Step 1] Starting FastAPI Service A on port 8001 ..." -ForegroundColor Yellow
$svc1 = Start-Process -FilePath $uvicorn -ArgumentList "niriksha.service:app --host 0.0.0.0 --port 8001 --log-level warning" -PassThru -NoNewWindow
Start-Sleep -Seconds 2

# 2. Start Service B (port 8002) - Replacement endpoint
Write-Host "[DEMO Step 2] Starting FastAPI Service B on port 8002 (replacement target) ..." -ForegroundColor Yellow
$svc2 = Start-Process -FilePath $uvicorn -ArgumentList "niriksha.service:app --host 0.0.0.0 --port 8002 --log-level warning" -PassThru -NoNewWindow
Start-Sleep -Seconds 2

# 3. Start DNS-SD Advertiser on port 8001 with migration scheduled to port 8002 after 15 seconds
Write-Host "[DEMO Step 3] Starting DNS-SD Advertiser on port 8001 (will migrate to 8002 in 15s) ..." -ForegroundColor Yellow
$adv = Start-Process -FilePath $py -ArgumentList "-m niriksha.advertiser --port 8001 --migrate-to 8002 --migrate-delay 15" -PassThru -NoNewWindow
Start-Sleep -Seconds 3

# 4. Run Monitor for 30 seconds to capture initial verification, fault/migration, and recovery
Write-Host "[DEMO Step 4] Running Continuous Monitor for 35 seconds to observe migration ..." -ForegroundColor Yellow
$mon = Start-Process -FilePath $py -ArgumentList "-m niriksha.monitor_runner --interval 4 --timeout 3 --discovery-wait 5 --log `"$logFile`"" -PassThru -NoNewWindow

# Wait 8s for initial verification at port 8001
Start-Sleep -Seconds 8

# 5. Stop Service A on port 8001 to simulate endpoint fault prior to/during migration
Write-Host "`n[DEMO Step 5] Stopping Service A on port 8001 (simulating service failure) ..." -ForegroundColor Red
if (-not $svc1.HasExited) {
    Stop-Process -Id $svc1.Id -Force
}

# Wait for migration and recovery
Write-Host "[DEMO Step 6] Waiting for DNS-SD SRV migration to port 8002 and monitor recovery ..." -ForegroundColor Yellow
Start-Sleep -Seconds 18

# Clean up remaining processes
Write-Host "`n[DEMO Step 7] Cleaning up demo processes ..." -ForegroundColor DarkGray
if (-not $svc2.HasExited) { Stop-Process -Id $svc2.Id -Force }
if (-not $adv.HasExited)  { Stop-Process -Id $adv.Id -Force }
if (-not $mon.HasExited)  { Stop-Process -Id $mon.Id -Force }

Write-Host "`n[DEMO COMPLETE] Reading latest migration and recovery events from log:" -ForegroundColor Green
Get-Content $logFile -Tail 6 | ForEach-Object { Write-Host "  $_" -ForegroundColor Cyan }
