# NIRIKSHA Phase 3A - Run the Streamlit Dashboard
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_dashboard.ps1 [-Port 8501]

param(
    [int]$Port = 8501
)

$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"
$py = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    $py = "python"
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  NIRIKSHA - Discover. Examine. Verify." -ForegroundColor Cyan
Write-Host "  Technical Demonstration Streamlit Dashboard" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "[DASHBOARD] URL        : http://localhost:$Port" -ForegroundColor Green
Write-Host "[DASHBOARD] Event Log  : $root\niriksha_events.jsonl" -ForegroundColor DarkGray
Write-Host "[DASHBOARD] PYTHONPATH : $env:PYTHONPATH" -ForegroundColor DarkGray

& $py -m streamlit run "$root\src\niriksha\dashboard.py" `
    --server.port $Port `
    --server.address localhost `
    --server.headless true
