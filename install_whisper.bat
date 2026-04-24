@echo off
echo ========================================
echo 语音识别依赖安装脚本
echo ========================================
echo.

echo 正在安装openai-whisper...
pip install openai-whisper

echo.
echo 可选：安装更快的版本（需要CUDA）
echo pip install faster-whisper
echo.

echo 检查安装结果...
python -c "import whisper; print('✅ Whisper安装成功')" || echo "❌ Whisper安装失败"

echo.
echo ========================================
echo 安装完成！
echo ========================================
echo.
echo 下一步：
echo 1. 停止当前系统（Ctrl+C）
echo 2. 重启系统：python main.py
echo 3. 发送语音消息测试
echo.
pause