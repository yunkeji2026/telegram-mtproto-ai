# Stop the ASR service (run ON 176). Used for failover drills / maintenance.
schtasks /End /TN 'AITR_ASR_176' 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Output 'stopped'
