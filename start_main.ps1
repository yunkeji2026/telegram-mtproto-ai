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

# 端口防护（2026-07-12 补）：进程表没有 main.py 但 18799 仍被占 = 幽灵持有者
# （半死实例/别的程序）。此时起新进程必然绑失败成「跑着却服务不了」的假活实例
# ——当天曾出现 3 个 main.py 并发互相踩踏。这里先清掉持有者再起，起不动就报错退出。
$port = 18799
$own = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($own.Count) {
    $pids = ($own | Select-Object -ExpandProperty OwningProcess -Unique)
    Write-Host "[start-main] port $port held by PID(s)=$($pids -join ',') — stopping ghost holder"
    foreach ($holder in $pids) { Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $own = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($own.Count) {
        Write-Host "[start-main] ABORT: port $port cannot be freed" -ForegroundColor Red
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path logs | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$out = "logs\boot_${ts}.out.log"
$err = "logs\boot_${ts}.err.log"

Write-Host "[start-main] launching main.py at $ts ..."
# Win32_Process.Create（不继承句柄）：Start-Process 会让 python 继承本 shell 的
# stdout 管道句柄，凡捕获本脚本输出的调用方（自动化工具）会挂死等 EOF。
$cmdline = "cmd.exe /c python main.py > `"$PSScriptRoot\$out`" 2> `"$PSScriptRoot\$err`""
$null = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmdline; CurrentDirectory = $PSScriptRoot }

Start-Sleep 4
$now = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' } |
    Select-Object -ExpandProperty ProcessId
Write-Host "[start-main] done PID(s)=$($now -join ',') log=$out"
Write-Host "[start-main] IndexTTS2 will start via minicpm_clone.local_autostart (coupled)"
