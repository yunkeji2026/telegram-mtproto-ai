# restart.ps1 — 一键重启 main.py 服务
# 用法: pwsh -File restart.ps1
# 或直接在 PowerShell 里: .\restart.ps1

$ErrorActionPreference = "SilentlyContinue"

# 1) 杀掉所有 python 进程（包括子进程）
Write-Host "[restart] stopping all python processes..."
Get-Process python* | Stop-Process -Force
Start-Sleep 4

# 确认全部退出
$still = Get-Process python* 2>$null
if ($still) {
    Write-Host "[restart] force-killing remaining: $($still.Id -join ',')"
    $still | Stop-Process -Force
    Start-Sleep 2
}

# 2) 启动新进程，日志落到 logs\restart_<ts>.out/err.log
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$out = "logs\restart_${ts}.out.log"
$err = "logs\restart_${ts}.err.log"

Write-Host "[restart] starting main.py at $ts ..."
Start-Process -FilePath "python" `
    -ArgumentList "main.py" `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $out `
    -RedirectStandardError  $err `
    -NoNewWindow

Start-Sleep 5
$pid2 = (Get-Process python* | Select-Object -First 1).Id
Write-Host "[restart] done. PID=$pid2  logs=$out"
