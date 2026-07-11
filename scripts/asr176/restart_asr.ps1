# Restart the ASR service via its scheduled task (run ON 176).
schtasks /End /TN 'AITR_ASR_176' 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
schtasks /Run /TN 'AITR_ASR_176' | Out-Null
Write-Output 'restarted'
