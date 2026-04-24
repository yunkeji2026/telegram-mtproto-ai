@echo off
echo ========================================
echo Telegram MTProto AI 安装和测试脚本
echo ========================================
echo 创建时间: 2026-03-08 06:30 GMT+8
echo.

echo [步骤1/6] 停止现有进程...
echo 请按 Ctrl+C 停止当前运行的Python程序
echo 如果PowerShell窗口已关闭，请跳过此步
echo.
pause

echo.
echo [步骤2/6] 检查Python环境...
python --version
if %errorlevel% neq 0 (
    echo ❌ Python未安装或不在PATH中
    echo 请先安装Python 3.8+
    pause
    exit /b 1
)

echo.
echo [步骤3/6] 安装核心依赖...
echo 正在安装pyrogram等核心依赖...
pip install pyrogram tgcrypto openai aiohttp PyYAML colorama loguru
if %errorlevel% neq 0 (
    echo ❌ 依赖安装失败
    pause
    exit /b 1
)

echo.
echo [步骤4/6] 安装语音识别依赖...
echo 正在安装openai-whisper...
pip install openai-whisper
if %errorlevel% neq 0 (
    echo ⚠️ Whisper安装失败，但系统仍可运行（仅文本模式）
    echo 您可以先测试文本功能，稍后重试
    pause
)

echo.
echo [步骤5/6] 启动系统...
echo 正在启动Telegram AI系统...
echo 如果首次运行或session过期，需要输入验证码
echo.
echo 重要：验证码处理流程：
echo 1. 系统会发送验证码到您的Telegram应用
echo 2. 创建code.txt文件并写入验证码数字
echo 3. 系统会自动读取并继续登录
echo 4. 验证码有效时间：5分钟
echo.
echo 按任意键启动系统...
pause
start python main.py

echo.
echo [步骤6/6] 测试指南...
echo.
echo ✅ 系统启动成功！
echo.
echo 📝 测试步骤：
echo 1. 等待系统显示"Telegram客户端已启动，等待消息..."
echo 2. 发送文字"测试"到 @ai_zkw
echo 3. 查看系统是否回复"欢迎咨询！"或类似消息
echo 4. 发送语音消息测试语音识别
echo.
echo 📊 验证方法：
echo - 检查logs/app.log日志文件
echo - 查看PowerShell控制台输出
echo - 确认收到AI回复
echo.
echo 🔧 故障排除：
echo 1. 无回复：等待30秒后重试，检查网络连接
echo 2. 验证码问题：删除sessions/文件夹重新登录
echo 3. API错误：检查config/config.yaml中的API密钥
echo.
echo ========================================
echo 安装测试完成！
echo ========================================
echo.
echo 📞 如需帮助：
echo 1. 提供logs/app.log内容
echo 2. 截图PowerShell错误信息
echo 3. 描述具体问题现象
echo.
pause