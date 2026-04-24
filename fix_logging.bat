@echo off
REM 日志配置修复脚本
REM 修复日志配置问题，确保生成 logs/app.log 文件

echo 🔧 Telegram AI 日志配置修复工具
echo ========================================

REM 检查Python环境
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python 未找到或不在 PATH 中
    echo 请确保 Python 已安装并添加到 PATH 环境变量
    pause
    exit /b 1
)

REM 运行Python修复脚本
echo 📋 运行日志配置修复脚本...
python fix_logging.py

if errorlevel 1 (
    echo.
    echo ❌ 修复脚本执行失败
    echo.
    echo 📋 手动修复步骤:
    echo 1. 创建 logs 目录:
    echo    mkdir logs
    echo.
    echo 2. 在 config/config.yaml 中添加以下内容:
    echo    logging:
    echo      level: "INFO"
    echo      file: "logs/app.log"
    echo      max_size: 10485760
    echo      backup_count: 5
    echo      console_output: true
    echo.
    echo 3. 修改 main.py 中的 setup_logger() 调用
    echo    将 "self.logger = setup_logger()" 替换为:
    echo    log_config = self.config.get("logging", {})
    echo    self.logger = setup_logger(
    echo        log_level=log_config.get("level", "INFO"),
    echo        log_file=log_config.get("file"),
    echo        console_output=log_config.get("console_output", True)
    echo    )
    pause
    exit /b 1
)

echo.
echo ✅ 日志配置修复完成！
echo.
echo 📋 下一步操作:
echo 1. 重启系统以应用新的日志配置:
echo    python main.py
echo.
echo 2. 检查日志文件是否生成:
echo    dir logs\app.log
echo.
echo 3. 使用监控脚本查看实时日志:
echo    .\monitor_system.ps1 -Action monitor
echo.
pause