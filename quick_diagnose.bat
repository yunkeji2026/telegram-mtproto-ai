@echo off
echo ========================================
echo Telegram MTProto AI 快速诊断
echo ========================================
echo.

echo [1] 检查Python版本...
python --version
if %errorlevel% neq 0 (
    echo ❌ Python未找到
    goto :end
)

echo.
echo [2] 检查依赖安装...
echo --- 核心依赖 ---
python -c "import pyrogram; print('✅ pyrogram 已安装')" || echo "❌ pyrogram 未安装"
python -c "import openai; print('✅ openai 已安装')" || echo "❌ openai 未安装"
python -c "import aiohttp; print('✅ aiohttp 已安装')" || echo "❌ aiohttp 未安装"

echo.
echo --- 语音识别依赖 ---
python -c "import whisper; print('✅ whisper 已安装')" || echo "❌ whisper 未安装"

echo.
echo [3] 检查配置文件...
if exist config\config.yaml (
    echo ✅ config.yaml 存在
) else (
    echo ❌ config.yaml 不存在
)

if exist requirements.txt (
    echo ✅ requirements.txt 存在
) else (
    echo ❌ requirements.txt 不存在
)

echo.
echo [4] 检查会话文件...
if exist sessions\639277356155.session (
    echo ✅ 会话文件存在 (不需要重新登录)
) else (
    echo ⚠️ 会话文件不存在 (需要重新登录)
)

echo.
echo [5] 检查日志目录...
if exist logs\ (
    echo ✅ 日志目录存在
) else (
    echo ⚠️ 日志目录不存在
)

echo.
echo [6] 快速启动测试...
echo 按Ctrl+C可以停止测试
echo.
echo 如果看到以下信息说明成功：
echo 1. "Telegram客户端已启动，等待消息..."
echo 2. 没有红色错误信息
echo.
pause

echo.
echo 正在启动测试 (5秒超时)...
start /b python -c "
import sys
import subprocess
import threading
import time

def run_test():
    try:
        proc = subprocess.Popen([sys.executable, 'main.py'], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE,
                              text=True,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        
        # 运行5秒
        time.sleep(5)
        
        # 获取输出
        stdout, stderr = proc.communicate(timeout=1)
        
        if proc.returncode == 0:
            print('✅ 启动成功')
        else:
            print(f'❌ 启动失败，返回码: {proc.returncode}')
            if stderr:
                print(f'错误信息: {stderr[:200]}...')
        
        # 终止进程
        proc.terminate()
        
    except Exception as e:
        print(f'❌ 测试异常: {e}')

run_test()
"

echo.
echo ========================================
echo 诊断完成
echo ========================================
echo.
echo 📝 请提供以下信息：
echo 1. 上面的检查结果
echo 2. 手动运行 python main.py 的输出
echo 3. 发送"测试"到 @ai_zkw 的结果
echo.
pause

:end
pause