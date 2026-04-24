@echo off
REM Telegram MTProto AI 简单重启测试脚本
REM 创建时间: 2026-03-08 06:00 GMT+8

echo ========================================
echo Telegram MTProto AI 系统重启测试
echo ========================================
echo.

echo [1/5] 检查Python环境...
python --version
if errorlevel 1 (
    echo ❌ Python未安装或不在PATH中
    pause
    exit /b 1
)
echo ✅ Python环境正常
echo.

echo [2/5] 检查配置文件...
if not exist "config\config.yaml" (
    echo ❌ 配置文件不存在: config\config.yaml
    pause
    exit /b 1
)
echo ✅ 配置文件存在
echo.

echo [3/5] 检查Session文件...
python -c "
import yaml
with open('config/config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)
session_name = config['telegram']['session_name']
session_file = f'sessions/{session_name}.session'
import os
if os.path.exists(session_file):
    size = os.path.getsize(session_file)
    print(f'✅ Session文件存在: {session_file} ({size} bytes)')
else:
    print(f'❌ Session文件不存在: {session_file}')
    print('💡 需要重新登录获取验证码')
"
echo.

echo [4/5] 停止现有进程...
echo 按 Ctrl+C 停止正在运行的Python进程 (如果有)
echo 等待5秒...
timeout /t 5 /nobreak >nul
echo.

echo [5/5] 启动系统...
echo 正在启动AI聊天助手...
echo 注意: 如果出现验证码，请在5分钟内创建code.txt文件
echo.
echo 按 Ctrl+C 可随时停止系统
echo ========================================
echo.

python main.py

echo.
echo ========================================
echo 系统已停止
echo ========================================
pause