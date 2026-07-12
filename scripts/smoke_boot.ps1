# smoke_boot.ps1 -- Stage 2 refactor verifier: boot main.py in isolation until
# initialize() completes (PASS). Neither --check nor pytest covers the main.py
# initialize()/web-factory wiring, so a real boot is the only way to verify it.
#
# Desktop mode skips Telegram; example config uses independent ports
# (web 18787 / metrics 19190), zero conflict with the live service (18799/19199).
#
# PASS(exit 0): web port 18787 starts listening within timeout (initialize done).
# FAIL(exit 1): timeout; prints error tail. Always kills the process it started.
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\smoke_boot.ps1 [-TimeoutSec 120]
param(
    [int]$TimeoutSec = 120,
    [int]$Port = 18787
)
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1) Ensure a config.yaml exists (fall back to example; enough for desktop smoke)
if (-not (Test-Path "config\config.yaml")) {
    Copy-Item "config\config.example.yaml" "config\config.yaml" -Force
    Write-Host "[smoke] config.yaml missing, copied from example"
}

# 2) Clear any stale listener on the port, then WAIT until it is actually free.
# A mid-teardown process can still hold the port for a moment; starting before it
# frees causes a bind failure that looks like a spurious "boot failed".
$stale = Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue
if ($stale) { $stale.OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -EA SilentlyContinue } }
$freeDeadline = (Get-Date).AddSeconds(8)
while ((Get-Date) -lt $freeDeadline -and (Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue)) {
    Start-Sleep -Milliseconds 500
}

# 3) Boot in desktop mode (skips Telegram protocol client). '-u' = unbuffered so
# logs flush immediately: on a crash the real traceback/last line is visible.
$env:AITR_DESKTOP_MODE = "1"
$out = "_smoke.out"; $err = "_smoke.err"
Remove-Item $out, $err -EA SilentlyContinue
Write-Host "[smoke] starting main.py (desktop mode, port=$Port, timeout=${TimeoutSec}s) ..."
$proc = Start-Process -FilePath "python" -ArgumentList "-u main.py" -WorkingDirectory $root -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru

# 4) Poll for port readiness
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $proc.Id -EA SilentlyContinue)) {
        Write-Host "[smoke] process exited early (boot failed)"
        break
    }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue) { $ready = $true; break }
    Start-Sleep -Seconds 2
}

# 5) Teardown: kill the process we started + anything left on the port
Stop-Process -Id $proc.Id -Force -EA SilentlyContinue
Start-Sleep 2
Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue | Select-Object -ExpandProperty OwningProcess | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -EA SilentlyContinue }

if ($ready) {
    Write-Host "[smoke] PASS - initialize() completed, web port $Port listened"
    exit 0
} else {
    Write-Host "[smoke] FAIL - not ready before timeout. stdout tail (real progress/crash):"
    Get-Content $out -Tail 15 -EA SilentlyContinue | ForEach-Object { "    $_" }
    Write-Host "[smoke] stderr tail:"
    Get-Content $err -Tail 20 -EA SilentlyContinue | ForEach-Object { "    $_" }
    exit 1
}
