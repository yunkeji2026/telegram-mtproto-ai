<#
.SYNOPSIS
Telegram AI 日志配置修复脚本

.DESCRIPTION
修复日志配置问题，确保生成 logs/app.log 文件

.EXAMPLE
.\fix_logging.ps1
#>

Write-Host "🔧 Telegram AI 日志配置修复工具" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Gray

# 检查Python环境
try {
    $pythonVersion = python --version 2>&1
    Write-Host "✅ Python 环境: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "❌ Python 未找到或不在 PATH 中" -ForegroundColor Red
    Write-Host "请确保 Python 已安装并添加到 PATH 环境变量" -ForegroundColor Yellow
    exit 1
}

# 运行Python修复脚本
Write-Host "📋 运行日志配置修复脚本..." -ForegroundColor Cyan
python fix_logging.py

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "❌ 修复脚本执行失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "📋 手动修复步骤:" -ForegroundColor Yellow
    Write-Host "1. 创建 logs 目录:" -ForegroundColor White
    Write-Host "   mkdir logs" -ForegroundColor Gray
    Write-Host ""
    Write-Host "2. 在 config/config.yaml 中添加以下内容:" -ForegroundColor White
    Write-Host "   logging:" -ForegroundColor Gray
    Write-Host "     level: 'INFO'" -ForegroundColor Gray
    Write-Host "     file: 'logs/app.log'" -ForegroundColor Gray
    Write-Host "     max_size: 10485760" -ForegroundColor Gray
    Write-Host "     backup_count: 5" -ForegroundColor Gray
    Write-Host "     console_output: true" -ForegroundColor Gray
    Write-Host ""
    Write-Host "3. 修改 main.py 中的 setup_logger() 调用" -ForegroundColor White
    Write-Host "   将 'self.logger = setup_logger()' 替换为:" -ForegroundColor Gray
    Write-Host "   log_config = self.config.get('logging', {})" -ForegroundColor Gray
    Write-Host "   self.logger = setup_logger(" -ForegroundColor Gray
    Write-Host "       log_level=log_config.get('level', 'INFO')," -ForegroundColor Gray
    Write-Host "       log_file=log_config.get('file')," -ForegroundColor Gray
    Write-Host "       console_output=log_config.get('console_output', True)" -ForegroundColor Gray
    Write-Host "   )" -ForegroundColor Gray
    exit 1
}

Write-Host ""
Write-Host "✅ 日志配置修复完成！" -ForegroundColor Green
Write-Host ""
Write-Host "📋 下一步操作:" -ForegroundColor Cyan
Write-Host "1. 重启系统以应用新的日志配置:" -ForegroundColor White
Write-Host "   python main.py" -ForegroundColor Gray
Write-Host ""
Write-Host "2. 检查日志文件是否生成:" -ForegroundColor White
Write-Host "   Get-ChildItem logs\app.log" -ForegroundColor Gray
Write-Host ""
Write-Host "3. 使用监控脚本查看实时日志:" -ForegroundColor White
Write-Host "   .\monitor_system.ps1 -Action monitor" -ForegroundColor Gray
Write-Host ""
Write-Host "4. 发送测试消息验证功能:" -ForegroundColor White
Write-Host "   发送 '测试001' 和 '查订单' 到群组" -ForegroundColor Gray