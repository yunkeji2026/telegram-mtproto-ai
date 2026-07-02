# restart_main_keep_tts.ps1 — 重启 main.py，但【保留】本机 IndexTTS2 情感克隆适配服务
# 背景：restart.ps1 用 `Get-Process python* | Stop-Process` 会连带杀掉 IndexTTS2
#       适配服务（也是 python.exe），导致声纹模型被卸载、下次启动要重载 ~90s。
#       本脚本只精确重启 main.py，IndexTTS2（aitr_indextts2_server.py）不受影响。
# 用法: powershell -ExecutionPolicy Bypass -File .\restart_main_keep_tts.ps1

$ErrorActionPreference = "SilentlyContinue"

# 1) 只停 main.py 对应的 python 进程；显式排除 IndexTTS2 适配服务
Write-Host "[restart-main] stopping main.py python (IndexTTS2 adapter preserved)..."
$targets = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*aitr_indextts2*' }
foreach ($p in $targets) {
    Write-Host ("[restart-main] kill PID=" + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force
}
Start-Sleep 4

# 2) 启动新 main.py，日志沿用 restart.ps1 的命名（logs\restart_<ts>.out/err.log）
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$out = "logs\restart_${ts}.out.log"
$err = "logs\restart_${ts}.err.log"

Write-Host "[restart-main] starting main.py at $ts ..."
Start-Process -FilePath "python" `
    -ArgumentList "main.py" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $out `
    -RedirectStandardError  $err `
    -NoNewWindow

Start-Sleep 6
$now = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' } |
    Select-Object -ExpandProperty ProcessId
Write-Host "[restart-main] main.py PID(s)=$($now -join ',')  log=$out"
