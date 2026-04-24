@echo off
echo Telegram AI 最简测试
echo 仅验证核心功能，无复杂操作
echo.

echo 步骤1: 检查基本环境
python --version
if errorlevel 1 (
    echo 错误: Python未找到
    pause
    exit
)

echo.
echo 步骤2: 检查配置文件
if not exist config\config.yaml (
    echo 错误: 配置文件不存在
    pause
    exit
)

echo 正在检查情绪增强器配置...
findstr "emoticons.enabled" config\config.yaml

echo.
echo 步骤3: 重启系统（按Ctrl+C停止当前进程）
echo 按任意键继续...或按Ctrl+C取消
pause >nul

echo.
echo 正在停止现有Python进程...
taskkill /F /IM python.exe 2>nul
timeout /t 2 >nul

echo.
echo 步骤4: 启动系统
echo 系统将在新窗口启动，请等待"Telegram客户端已启动"
start python main.py

echo.
echo 步骤5: 测试指南
echo.
echo 等待10秒让系统启动，然后在Telegram群组发送:
echo.
echo 1. "测试简单001"
echo 2. "帮我查订单"
echo 3. "@ai_zkw 你好"
echo.
echo 步骤6: 检查结果（1分钟后）
echo 等待60秒让系统处理消息...
timeout /t 60 >nul

echo.
if exist logs\app.log (
    echo 日志摘要:
    echo ---------------
    echo 最后收到消息:
    findstr "收到消息" logs\app.log | tail -3
    echo.
    echo 情绪增强日志:
    findstr "情绪增强" logs\app.log | tail -2
    echo.
    echo 回复消息:
    findstr "已回复消息" logs\app.log | tail -2
    echo ---------------
) else (
    echo 警告: 未找到日志文件
)

echo.
echo 测试完成！
echo.
echo 请报告:
echo 1. 系统是否启动成功？
echo 2. 回复是否有空格问题？
echo 3. 消息是否被记录？
echo.
pause