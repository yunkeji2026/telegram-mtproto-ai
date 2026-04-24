Write-Host "启动Telegram AI系统..."
$process = Start-Process python -ArgumentList "main.py" -PassThru -NoNewWindow -RedirectStandardOutput "logs/app.log" -RedirectStandardError "logs/error.log"
Write-Host "系统已启动 (PID: $($process.Id))"
Start-Sleep -Seconds 10
Write-Host "检查日志..."
if (Test-Path "logs/app.log") {
    $log = Get-Content "logs/app.log" -Tail 5
    Write-Host "日志内容:"
    $log
} else {
    Write-Host "日志文件未创建"
}
Write-Host "完成。"