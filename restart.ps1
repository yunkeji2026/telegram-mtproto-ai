# restart.ps1 — 一键重启 main.py（coupled 模式：IndexTTS2 随主程序一起停/起）
# 用法: pwsh -File restart.ps1
# 说明: minicpm_clone.local_autostart.enabled=true 时，IndexTTS2 由 main.py 托管；
#       杀 python 会连带关掉 IndexTTS2，新 main 启动后会自动再拉起（~60-90s eager 载入）。
#       若 local_autostart.enabled=false（独立常驻模式），请改用 restart_main_keep_tts.ps1。

$ErrorActionPreference = "SilentlyContinue"

Write-Host "[restart] stopping all python processes (main + coupled IndexTTS2)..."
Get-Process python* | Stop-Process -Force
Start-Sleep 4

$still = Get-Process python* 2>$null
if ($still) {
    Write-Host "[restart] force-killing remaining: $($still.Id -join ',')"
    $still | Stop-Process -Force
    Start-Sleep 2
}

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
