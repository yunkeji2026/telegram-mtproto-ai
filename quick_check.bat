@echo off
REM 快速系统状态检查脚本
echo ========================================
echo Telegram MTProto AI 快速状态检查
echo ========================================
echo.

echo 1. 检查日志文件...
if exist logs\app.log (
    echo   ✅ 日志文件存在: logs\app.log
    for /f %%i in ('powershell -command "(Get-Item logs\\app.log).length"') do set size=%%i
    echo   📊 文件大小: %size% 字节
) else (
    echo   ❌ 日志文件不存在: logs\app.log
    echo   💡 可能原因: 系统未运行
)

echo.
echo 2. 检查session文件...
if exist sessions\639277356155.session (
    echo   ✅ Session文件存在: sessions\639277356155.session
    for /f %%i in ('powershell -command "(Get-Item sessions\\639277356155.session).length"') do set session_size=%%i
    echo   📊 文件大小: %session_size% 字节
) else (
    echo   ❌ Session文件不存在: sessions\639277356155.session
    echo   💡 可能原因: 首次运行或session被删除
)

echo.
echo 3. 检查配置文件...
if exist config\config.yaml (
    echo   ✅ 配置文件存在: config\config.yaml
    python -c "import yaml; yaml.safe_load(open('config/config.yaml')); print('   ✅ 配置文件语法正确')" 2>nul || echo   ❌ 配置文件语法错误
) else (
    echo   ❌ 配置文件不存在: config\config.yaml
)

echo.
echo 4. 检查关键依赖...
python -c "import pyrogram; print('   ✅ Pyrogram 已安装')" 2>nul || echo   ❌ Pyrogram 未安装
python -c "import openai; print('   ✅ OpenAI 已安装')" 2>nul || echo   ❌ OpenAI 未安装
python -c "import yaml; print('   ✅ PyYAML 已安装')" 2>nul || echo   ❌ PyYAML 未安装

echo.
echo ========================================
echo 建议操作:
echo.
if not exist logs\app.log (
    echo 1. 启动系统: python main.py
    echo   观察启动输出，确认无错误
)
echo 2. 测试功能:
echo   - 私聊发送"测试"到 @ai_zkw
echo   - 群组发送"@ai_zkw 你好"
echo   - 群组发送"客服在吗"
echo 3. 检查日志: type logs\app.log
echo.
echo ========================================
pause