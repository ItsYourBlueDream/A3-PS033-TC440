# NIRIKSHA Phase 2B - Automated Live Migration Demonstration
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_migration_demo.ps1 [-PortA 8001] [-PortB 8002] [-PollInterval 3.0]

param(
    [int]$PortA = 8001,
    [int]$PortB = 8002,
    [double]$PollInterval = 3.0
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$env:PYTHONPATH = "$root\src"
$py = "$root\.venv\Scripts\python.exe"
$logFile = "$root\niriksha_events.jsonl"
$triggerFile = "$root\.migration_trigger"

# Temporary log files for FastAPI service instances
$svcA_out = Join-Path $env:TEMP "niriksha-svcA-$PortA-out.log"
$svcA_err = Join-Path $env:TEMP "niriksha-svcA-$PortA-err.log"
$svcB_out = Join-Path $env:TEMP "niriksha-svcB-$PortB-out.log"
$svcB_err = Join-Path $env:TEMP "niriksha-svcB-$PortB-err.log"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " NIRIKSHA Phase 2B: Live DNS-SD Endpoint Migration Demo     " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Service A Port : $PortA" -ForegroundColor DarkGray
Write-Host "Service B Port : $PortB" -ForegroundColor DarkGray
Write-Host "Event Log File : $logFile" -ForegroundColor DarkGray
Write-Host "Trigger File   : $triggerFile" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

function Test-PortInUse([int]$Port) {
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        $listener.Stop()
        return $false
    } catch {
        return $true
    }
}

function Wait-HttpEndpoint {
    param(
        [string]$Url,
        [System.Diagnostics.Process]$Process = $null,
        [string]$OutLog = "",
        [string]$ErrLog = "",
        [int]$TimeoutSeconds = 20
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        # Detect early process exit
        if ($Process -and $Process.HasExited) {
            $errContent = if ($ErrLog -and (Test-Path -LiteralPath $ErrLog)) { Get-Content -LiteralPath $ErrLog -Raw } else { "No stderr captured" }
            $outContent = if ($OutLog -and (Test-Path -LiteralPath $OutLog)) { Get-Content -LiteralPath $OutLog -Raw } else { "No stdout captured" }
            Write-Host "`n[ERROR] Service process $($Process.Id) exited prematurely with exit code $($Process.ExitCode)!" -ForegroundColor Red
            if ($errContent) { Write-Host "[STDERR]:`n$errContent" -ForegroundColor Yellow }
            if ($outContent) { Write-Host "[STDOUT]:`n$outContent" -ForegroundColor DarkGray }
            throw "Process exited prematurely before endpoint $Url became healthy."
        }

        try {
            $resp = Invoke-RestMethod -Uri $Url -TimeoutSec 1 -ErrorAction Stop
            if ($resp.status -eq "healthy") {
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }

    # Bounded timeout reached
    $errContent = if ($ErrLog -and (Test-Path -LiteralPath $ErrLog)) { Get-Content -LiteralPath $ErrLog -Raw } else { "No stderr captured" }
    $outContent = if ($OutLog -and (Test-Path -LiteralPath $OutLog)) { Get-Content -LiteralPath $OutLog -Raw } else { "No stdout captured" }
    Write-Host "`n[ERROR] Endpoint $Url did not become healthy within $TimeoutSeconds seconds." -ForegroundColor Red
    if ($errContent) { Write-Host "[STDERR]:`n$errContent" -ForegroundColor Yellow }
    if ($outContent) { Write-Host "[STDOUT]:`n$outContent" -ForegroundColor DarkGray }
    throw "Endpoint $Url did not become healthy within $TimeoutSeconds seconds."
}

function Get-NewEvents {
    param(
        [string]$Path,
        [int]$SkipLines
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }
    try {
        $stream = [System.IO.FileStream]::new(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)
        $lines = @()
        while (-not $reader.EndOfStream) {
            $lines += $reader.ReadLine()
        }
        $reader.Close()
        $stream.Close()
    } catch {
        return @()
    }

    if ($lines.Count -le $SkipLines) {
        return @()
    }

    $newLines = $lines | Select-Object -Skip $SkipLines
    $events = @()
    foreach ($l in $newLines) {
        if (-not [string]::IsNullOrWhiteSpace($l)) {
            try {
                $events += ($l | ConvertFrom-Json)
            } catch {}
        }
    }
    return $events
}

function Wait-ForCondition {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds,
        [string]$Description
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $result = & $Condition
        if ($result) {
            return $result
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timeout ($TimeoutSeconds s) exceeded waiting for: $Description"
}

# ---------------------------------------------------------------------------
# Pre-flight Checks
# ---------------------------------------------------------------------------

Write-Host "`n[PRE-FLIGHT] Verifying required ports are free..." -ForegroundColor Yellow
if (Test-PortInUse $PortA) {
    throw "Port $PortA is already in use by another process. Please free port $PortA before running the demo."
}
if (Test-PortInUse $PortB) {
    throw "Port $PortB is already in use by another process. Please free port $PortB before running the demo."
}
Write-Host "[PRE-FLIGHT] Ports $PortA and $PortB are available." -ForegroundColor Green

# Clean up stale temporary files from previous aborted runs
if (Test-Path -LiteralPath $triggerFile) {
    Remove-Item -LiteralPath $triggerFile -Force -ErrorAction SilentlyContinue
}
foreach ($f in @($svcA_out, $svcA_err, $svcB_out, $svcB_err)) {
    if (Test-Path -LiteralPath $f) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
}

# Capture baseline line count of log file so we only verify events from THIS run
$initialLineCount = 0
if (Test-Path -LiteralPath $logFile) {
    try {
        $stream = [System.IO.FileStream]::new(
            $logFile,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)
        while (-not $reader.EndOfStream) {
            $null = $reader.ReadLine()
            $initialLineCount++
        }
        $reader.Close()
        $stream.Close()
    } catch {}
}
Write-Host "[PRE-FLIGHT] Existing log lines: $initialLineCount (checking only newly appended events)" -ForegroundColor DarkGray

# Track spawned processes for clean, safe termination in finally
$spawnedProcesses = @()

try {
    # -----------------------------------------------------------------------
    # Step 1: Start FastAPI Service A (port 8001) using python.exe -m uvicorn
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 1] Starting FastAPI Service A on port $PortA via python.exe -m uvicorn ..." -ForegroundColor Yellow
    $svcA_args = "-m uvicorn niriksha.service:app --host 0.0.0.0 --port $PortA --log-level info"
    $svc1 = Start-Process -FilePath $py `
        -ArgumentList $svcA_args `
        -WorkingDirectory "$root" `
        -RedirectStandardOutput $svcA_out `
        -RedirectStandardError $svcA_err `
        -PassThru -NoNewWindow
    $spawnedProcesses += $svc1

    Wait-HttpEndpoint -Url "http://127.0.0.1:$PortA/health" -Process $svc1 -OutLog $svcA_out -ErrLog $svcA_err -TimeoutSeconds 20
    Write-Host "[DEMO Step 1] Service A is healthy and listening on port $PortA." -ForegroundColor Green

    # -----------------------------------------------------------------------
    # Step 2: Start FastAPI Service B (port 8002) - Standby replacement target
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 2] Starting FastAPI Service B on port $PortB (standby replacement) ..." -ForegroundColor Yellow
    $svcB_args = "-m uvicorn niriksha.service:app --host 0.0.0.0 --port $PortB --log-level info"
    $svc2 = Start-Process -FilePath $py `
        -ArgumentList $svcB_args `
        -WorkingDirectory "$root" `
        -RedirectStandardOutput $svcB_out `
        -RedirectStandardError $svcB_err `
        -PassThru -NoNewWindow
    $spawnedProcesses += $svc2

    Wait-HttpEndpoint -Url "http://127.0.0.1:$PortB/health" -Process $svc2 -OutLog $svcB_out -ErrLog $svcB_err -TimeoutSeconds 20
    Write-Host "[DEMO Step 2] Service B is healthy and listening on port $PortB." -ForegroundColor Green

    # -----------------------------------------------------------------------
    # Step 3: Start DNS-SD Advertiser on port 8001 with migration trigger file
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 3] Starting DNS-SD Advertiser on port $PortA (coordination trigger file configured) ..." -ForegroundColor Yellow
    $advArgs = @(
        "-m", "niriksha.advertiser",
        "--port", "$PortA",
        "--migrate-to", "$PortB",
        "--migrate-trigger-file", "$triggerFile"
    )
    $adv = Start-Process -FilePath $py `
        -ArgumentList $advArgs `
        -WorkingDirectory "$root" `
        -PassThru -NoNewWindow
    $spawnedProcesses += $adv
    Start-Sleep -Seconds 2

    # -----------------------------------------------------------------------
    # Step 4: Start Continuous Monitor
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 4] Starting Continuous Monitor (poll interval: ${PollInterval}s) ..." -ForegroundColor Yellow
    $monArgs = @(
        "-m", "niriksha.monitor_runner",
        "--interval", "$PollInterval",
        "--timeout", "3",
        "--discovery-wait", "5",
        "--log", "$logFile"
    )
    $mon = Start-Process -FilePath $py `
        -ArgumentList $monArgs `
        -WorkingDirectory "$root" `
        -PassThru -NoNewWindow
    $spawnedProcesses += $mon

    # -----------------------------------------------------------------------
    # Stage 1 Gating: Wait for Initial VERIFIED state on port 8001
    # -----------------------------------------------------------------------
    Write-Host "`n[STAGE 1 GATING] Waiting for monitor to discover and log initial VERIFIED state on port $PortA ..." -ForegroundColor Cyan
    $initEvent = Wait-ForCondition -TimeoutSeconds 30 -Description "Initial VERIFIED state on port $PortA" -Condition {
        $evts = Get-NewEvents -Path $logFile -SkipLines $initialLineCount
        foreach ($e in $evts) {
            if ($e.event -eq "state_change" -and $e.new_state -eq "VERIFIED" -and $e.service.port -eq $PortA) {
                return $e
            }
        }
        return $null
    }
    Write-Host "[STAGE 1 PASS] Service A verified on port $PortA!" -ForegroundColor Green
    Write-Host "  Transition : $($initEvent.old_state) -> $($initEvent.new_state)" -ForegroundColor Green
    Write-Host "  Latency    : $($initEvent.measurements.verification_latency_ms) ms" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # Stage 2: Inject Service A failure
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 5] Stopping Service A on port $PortA (simulating endpoint outage) ..." -ForegroundColor Red
    if (-not $svc1.HasExited) {
        Stop-Process -Id $svc1.Id -Force
    }
    Write-Host "[DEMO Step 5] Service A terminated. DNS-SD advertisement still active on port $PortA." -ForegroundColor Yellow

    # -----------------------------------------------------------------------
    # Stage 3 Gating: Wait for Failure Detection (VERIFIED -> UNREACHABLE)
    # -----------------------------------------------------------------------
    Write-Host "`n[STAGE 3 GATING] Waiting for monitor to detect fault and log UNREACHABLE on port $PortA ..." -ForegroundColor Cyan
    $failEvent = Wait-ForCondition -TimeoutSeconds 25 -Description "Failure detection on port $PortA" -Condition {
        $evts = Get-NewEvents -Path $logFile -SkipLines $initialLineCount
        foreach ($e in $evts) {
            if ($e.event -eq "state_change" -and $e.old_state -eq "VERIFIED" -and $e.new_state -eq "UNREACHABLE" -and $e.service.port -eq $PortA) {
                return $e
            }
        }
        return $null
    }
    Write-Host "[STAGE 3 PASS] Fault detected and logged by active verifier!" -ForegroundColor Green
    Write-Host "  Transition : $($failEvent.old_state) -> $($failEvent.new_state)" -ForegroundColor Green
    Write-Host "  Est. Det.  : $($failEvent.estimated_detection_latency_ms) ms" -ForegroundColor Green
    Write-Host "  Reason     : $($failEvent.reason)" -ForegroundColor Green

    # -----------------------------------------------------------------------
    # Stage 4: Trigger DNS-SD migration to replacement endpoint (port 8002)
    # -----------------------------------------------------------------------
    Write-Host "`n[DEMO Step 6] Triggering DNS-SD SRV migration from port $PortA to port $PortB via trigger file..." -ForegroundColor Yellow
    New-Item -ItemType File -Path $triggerFile -Force | Out-Null
    Write-Host "[DEMO Step 6] Trigger created. Waiting for advertiser to update mDNS SRV record to port $PortB ..." -ForegroundColor DarkGray

    # -----------------------------------------------------------------------
    # Stage 5 Gating: Wait for Migration & Autonomous Recovery on port 8002
    # -----------------------------------------------------------------------
    Write-Host "`n[STAGE 5 GATING] Waiting for monitor to observe mDNS SRV update and recover on port $PortB ..." -ForegroundColor Cyan
    $recovEvent = Wait-ForCondition -TimeoutSeconds 30 -Description "Recovery verification on port $PortB" -Condition {
        $evts = Get-NewEvents -Path $logFile -SkipLines $initialLineCount
        foreach ($e in $evts) {
            if ($e.event -eq "state_change" -and $e.old_state -eq "UNREACHABLE" -and $e.new_state -eq "VERIFIED" -and $e.service.port -eq $PortB -and $e.is_recovery -eq $true) {
                return $e
            }
        }
        return $null
    }
    Write-Host "[STAGE 5 PASS] Migration and autonomous recovery fully verified!" -ForegroundColor Green
    Write-Host "  Transition       : $($recovEvent.old_state) -> $($recovEvent.new_state) (Port $PortB)" -ForegroundColor Green
    Write-Host "  Recovery Latency : $($recovEvent.recovery_latency_ms) ms [Measured]" -ForegroundColor Green
    Write-Host "  Rediscovery Lat. : $($recovEvent.rediscovery_latency_ms) ms [Measured]" -ForegroundColor Green
    Write-Host "  Est. Total Recov : $($recovEvent.estimated_total_recovery_latency_ms) ms [Estimated]" -ForegroundColor Green

    # Brief delay so subsequent probe is captured
    Start-Sleep -Seconds 3

    # -----------------------------------------------------------------------
    # Demonstration Summary
    # -----------------------------------------------------------------------
    Write-Host "`n============================================================" -ForegroundColor Cyan
    Write-Host " MIGRATION DEMONSTRATION COMPLETE: GENUINE SEQUENCE LOGGED   " -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan

    $runEvents = Get-NewEvents -Path $logFile -SkipLines $initialLineCount
    Write-Host "Chronological events logged during this demonstration run:" -ForegroundColor Yellow
    $stepIdx = 1
    foreach ($evt in $runEvents) {
        $evType = $evt.event
        $ts = $evt.wall_clock_utc
        if ($evType -eq "state_change") {
            $p = $evt.service.port
            $recovFlag = if ($evt.is_recovery) { " [RECOVERY]" } else { "" }
            $migFlag = if ($evt.is_migration) { " [MIGRATION]" } else { "" }
            Write-Host "  $stepIdx. [$ts] state_change: $($evt.old_state) -> $($evt.new_state) on port $p$recovFlag$migFlag" -ForegroundColor Cyan
            $stepIdx++
        } elseif ($evType -eq "endpoint_migration") {
            Write-Host "  $stepIdx. [$ts] endpoint_migration: Port $($evt.old_endpoint.port) -> Port $($evt.new_endpoint.port) (rediscovery: $($evt.rediscovery_latency_ms) ms)" -ForegroundColor Magenta
            $stepIdx++
        } elseif ($evType -eq "discovery") {
            Write-Host "  $stepIdx. [$ts] discovery ($($evt.action)): port $($evt.service.port)" -ForegroundColor DarkGray
            $stepIdx++
        }
    }

} finally {
    Write-Host "`n[CLEANUP] Safely stopping demonstration processes..." -ForegroundColor DarkGray
    foreach ($proc in $spawnedProcesses) {
        if ($proc -and -not $proc.HasExited) {
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            } catch {}
        }
    }
    if (Test-Path -LiteralPath $triggerFile) {
        Remove-Item -LiteralPath $triggerFile -Force -ErrorAction SilentlyContinue
    }
    foreach ($f in @($svcA_out, $svcA_err, $svcB_out, $svcB_err)) {
        if (Test-Path -LiteralPath $f) {
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "[CLEANUP] Cleanup completed." -ForegroundColor DarkGray
}
