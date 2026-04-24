@echo off
echo ========================================
echo 简单语法检查和测试
echo ========================================
echo.

echo [1] 检查Python语法...
python -m py_compile src/client/telegram_client.py
if %errorlevel% equ 0 (
    echo ✅ telegram_client.py 语法正确
) else (
    echo ❌ telegram_client.py 有语法错误
    pause
    exit /b 1
)

echo.
echo [2] 检查Whisper安装...
python -c "import whisper; print('✅ Whisper已安装')"
if %errorlevel% neq 0 (
    echo ⚠️  Whisper未安装，语音功能将受限
    echo 请运行: pip install openai-whisper
)

echo.
echo [3] 检查配置文件...
if exist config\config.yaml (
    echo ✅ 配置文件存在
) else (
    echo ❌ 配置文件不存在
    pause
    exit /b 1
)

echo.
echo [4] 检查会话文件...
if exist sessions\639277356155.session (
    echo ✅ 会话文件存在 (不需要重新登录)
) else (
    echo ⚠️ 会话文件不存在 (需要重新登录)
)

echo.
echo ========================================
echo 检查完成
echo ========================================
echo.
echo 🚀 启动系统测试:
echo 1. Ctrl+C停止现有进程 (如果正在运行)
echo 2. python main.py
echo 3. 发送"测试"消息验证基础功能
echo 4. 发送语音消息测试语音识别
echo.
echo 📝 需要记录的信息:
echo - python main.py 的启动输出
echo - logs/app.log 中的语音处理日志
echo - 收到的AI回复内容
echo.
pause