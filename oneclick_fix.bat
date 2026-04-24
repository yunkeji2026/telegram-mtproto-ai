@echo off
echo Telegram AI 一键修复脚本
echo 只做三件事: 修复配置、重启系统、测试
echo.

REM 步骤1: 修复配置
echo 1. 修复情绪增强器配置...
if exist config\config.yaml (
    REM 备份原文件
    copy config\config.yaml config\config.yaml.backup 2>nul
    
    REM 确保emoticons.enabled为false
    powershell -Command "(Get-Content 'config\config.yaml') -replace 'emoticons:\s*enabled:\s*(true|True|TRUE)', 'emoticons: enabled: false' | Set-Content 'config\config.yaml'"
    
    REM 确保trigger.enabled为true
    findstr /C:"trigger:" config\config.yaml >nul
    if errorlevel 1 (
        echo. >> config\config.yaml
        echo # 四层触发机制配置 >> config\config.yaml
        echo trigger: >> config\config.yaml
        echo   enabled: true >> config\config.yaml
        echo   config_file: "config/trigger_rules.yaml" >> config\config.yaml
    )
    echo ✅ 配置修复完成
) else (
    echo ❌ 配置文件不存在
    pause
    exit
)

echo.
echo 2. 停止现有进程...
taskkill /F /IM python.exe /T 2>nul
timeout /t 3 >nul

echo.
echo 3. 启动优化系统...
echo 正在启动，请等待新窗口显示"Telegram客户端已启动"...
start python main.py

echo.
echo ✅ 一键修复完成！
echo.
echo 📢 测试步骤:
echo 1. 等待新窗口显示"Telegram客户端已启动"
echo 2. 发送消息: "测试修复001"
echo 3. 发送消息: "查订单状态"
echo 4. 等待10秒，检查回复
echo.
echo 🔍 验证要点:
echo - 回复是否正常? (无空格问题)
echo - 消息是否记录? (查看logs\app.log)
echo - 系统是否稳定?
echo.
echo 🚨 如果仍有问题:
echo 1. 提供错误截图
echo 2. 描述具体现象
echo 3. 执行: python verify_config.py
echo.
pause