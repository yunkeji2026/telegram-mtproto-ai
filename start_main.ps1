# start_main.ps1 — 登录/开机自启 main.py（IndexTTS2 由主程序 coupled 托管，无需单独计划任务）
# 用法: powershell -ExecutionPolicy Bypass -File .\start_main.ps1
# 建议：任务计划程序「登录时」触发本脚本（见下方 schtasks 示例）。

$ErrorActionPreference = "SilentlyContinue"
Set-Location $PSScriptRoot

# 已在跑则跳过（避免重复实例争端口）
$existing = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*aitr_indextts2*' }
if ($existing) {
    Write-Host "[start-main] main.py already running PID=$($existing.ProcessId)"
    exit 0
}

New-Item -ItemType Directory -Force -Path logs | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$out = "logs\boot_${ts}.out.log"
$err = "logs\boot_${ts}.err.log"

Write-Host "[start-main] launching main.py at $ts ..."
Start-Process -FilePath "python" `
    -ArgumentList "main.py" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $out `
    -RedirectStandardError  $err `
    -WindowStyle Hidden

Start-Sleep 4
$now = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' } |
    Select-Object -ExpandProperty ProcessId
Write-Host "[start-main] done PID(s)=$($now -join ',') log=$out"
Write-Host "[start-main] IndexTTS2 will start via minicpm_clone.local_autostart (coupled)"
