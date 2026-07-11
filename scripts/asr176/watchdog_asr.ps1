# Self-heal watchdog for the GPU audio service (run ON 176 via schtasks, every 5 min).
# If /health does not answer within 8s, bounce the service through its scheduled task.
# Rationale: AITR_ASR_176 is ONSTART-only — a mid-day crash would silently degrade
# clients to CPU fallback (slow ASR / plus_base SER) until someone notices.
# Register (as admin, on 176):
#   schtasks /Create /F /TN 'AITR_ASR_WATCHDOG' /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_asr\watchdog_asr.ps1"
$ErrorActionPreference = 'Continue'
$log = 'C:\aitr_asr\logs\watchdog.log'

try {
    $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 8 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { exit 0 }   # healthy: stay quiet, no log spam
    $reason = "status=$($resp.StatusCode)"
} catch {
    $reason = "unreachable: $($_.Exception.Message)"
}

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] health failed ($reason) -> restarting"
schtasks /End /TN 'AITR_ASR_176' 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
schtasks /Run /TN 'AITR_ASR_176' | Out-Null
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] restart issued"
