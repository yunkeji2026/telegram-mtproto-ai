@echo off
echo ========================================
echo Telegram AI 简化诊断脚本
echo 避免复杂语法，提供基本功能
echo ========================================
echo.

echo [1/6] 显示当前环境...
echo 当前目录: %CD%
echo.
python --version
if errorlevel 1 (
    echo ❌ Python未找到，请检查PATH
    pause
    exit /b 1
)
echo.

echo [2/6] 检查配置文件...
if not exist "config\config.yaml" (
    echo ❌ config.yaml不存在
    pause
    exit /b 1
)

echo 检查关键配置项:
echo --- emoticons.enabled ---
findstr /C:"emoticons.enabled" config\config.yaml
echo.
echo --- trigger.enabled ---
findstr /C:"trigger.enabled" config\config.yaml
echo.

echo [3/6] 停止现有Python进程...
echo 正在停止Python进程...
taskkill /F /IM python.exe /T >nul 2>&1
if errorlevel 1 (
    echo ℹ️ 无Python进程运行或权限不足
)
timeout /t 2 /nobreak >nul
echo.

echo [4/6] 启动系统...
echo 正在启动Telegram AI系统...
echo 注意: 系统将在新窗口启动
echo.
start "Telegram AI" cmd /c "python main.py && pause"
echo ✅ 系统已启动，请等待启动完成...
echo 等待10秒...
timeout /t 10 /nobreak >nul
echo.

echo [5/6] 检查启动状态...
if exist "logs\app.log" (
    echo ✅ 日志文件存在
    echo 最后10行日志:
    echo -------------------------
    for /f "skip=-10" %%i in ('type "logs\app.log"') do echo %%i
    echo -------------------------
) else (
    echo ⚠️ 日志文件尚未创建，系统可能仍在启动
)
echo.

echo [6/6] 测试指南...
echo.
echo 📢 请在Telegram群组发送测试消息:
echo.
echo 1. "测试简单诊断001"
echo 2. "我的订单状态"
echo 3. "@ai_zkw 通道信息"
echo 4. "今天好天气"
echo.
echo 等待10秒后检查日志...
echo.
pause

echo.
echo 检查测试结果...
timeout /t 10 /nobreak >nul

if exist "logs\app.log" (
    echo.
    echo 📊 测试消息记录:
    echo -------------------------
    findstr /C:"[群组监控]" "logs\app.log" | findstr /C:"测试简单诊断 我的订单 @ai_zkw 今天好天气"
    echo.
    echo 📊 情绪增强器状态:
    echo -------------------------
    findstr /C:"情绪增强" "logs\app.log"
    echo.
    echo 📊 触发分析日志:
    echo -------------------------
    findstr /C:"[触发分析]" "logs\app.log"
) else (
    echo ❌ 日志文件不存在，系统可能未启动
)

echo.
echo ========================================
echo 简化诊断完成
echo ========================================
echo.
echo 📋 验证要点:
echo 1. 所有4条测试消息是否记录? ([群组监控])
echo 2. 是否有"情绪增强"日志? (应为无)
echo 3. 是否有"[触发分析]"日志? (应有)
echo 4. 系统是否稳定运行?
echo.
echo 📞 如需帮助，请提供:
echo 1. 此脚本的输出
echo 2. logs\app.log 内容
echo 3. 具体问题描述
echo.
pause