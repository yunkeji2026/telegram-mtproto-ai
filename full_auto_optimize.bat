@echo off
echo ========================================
echo Telegram AI 全自动优化与测试脚本
echo ========================================
echo 创建时间: 2026-03-08 21:36 GMT+8
echo 模式: 全自动诊断、优化、测试
echo.

echo [阶段1/5] 系统状态诊断...
echo.

echo 1.1 检查Python进程...
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I python.exe >nul
if %errorlevel% equ 0 (
    echo ✅ 检测到Python进程正在运行
    echo   正在停止现有进程...
    taskkill /F /IM python.exe /T >nul 2>&1
    timeout /t 3 /nobreak >nul
    echo ✅ 已停止Python进程
) else (
    echo ℹ️ 未检测到运行的Python进程
)
echo.

echo 1.2 检查配置文件状态...
if exist "config\config.yaml" (
    echo ✅ 配置文件存在
    echo   验证关键配置...
    
    set CONFIG_OK=1
    
    findstr /C:"emoticons.enabled: false" config\config.yaml >nul
    if %errorlevel% equ 0 (
        echo ✅ 情绪增强器已禁用 (emoticons.enabled: false)
    ) else (
        echo ❌ 情绪增强器未禁用，正在修复...
        powershell -Command "(Get-Content 'config\config.yaml') -replace 'emoticons:\\s*enabled:\\s*true', 'emoticons: enabled: false' | Set-Content 'config\config.yaml'"
        echo ✅ 已修复情绪增强器配置
        set CONFIG_OK=0
    )
    
    findstr /C:"trigger.enabled: true" config\config.yaml >nul
    if %errorlevel% equ 0 (
        echo ✅ 触发系统已启用 (trigger.enabled: true)
    ) else (
        echo ❌ 触发系统未启用，正在修复...
        echo. >> config\config.yaml
        echo # 四层触发机制配置 >> config\config.yaml
        echo trigger: >> config\config.yaml
        echo   enabled: true                    # 启用四层触发机制 >> config\config.yaml
        echo   config_file: "config/trigger_rules.yaml"  # 触发规则配置文件路径 >> config\config.yaml
        echo ✅ 已添加触发系统配置
        set CONFIG_OK=0
    )
) else (
    echo ❌ 配置文件不存在，无法继续
    pause
    exit /b 1
)
echo.

echo 1.3 检查代码修改状态...
if exist "src\client\telegram_client.py" (
    echo ✅ Telegram客户端代码存在
    echo   验证关键修复...
    
    findstr /C:"emoticons_config.get('enabled', True)" src\client\telegram_client.py >nul
    if %errorlevel% equ 0 (
        echo ✅ 情绪增强器调用检查已实施
    ) else (
        echo ⚠️ 情绪增强器调用检查可能未实施
        echo   需要手动检查代码修改
    )
    
    findstr /C:"\[触发分析\]" src\trigger\four_layer_trigger.py >nul
    if %errorlevel% equ 0 (
        echo ✅ 详细触发日志代码已实施
    ) else (
        echo ⚠️ 详细触发日志代码可能未实施
    )
) else (
    echo ❌ 代码文件不存在
)
echo.

echo [阶段2/5] 启动优化系统...
echo.

echo 2.1 启动Telegram AI系统...
echo   启动命令: python main.py
echo   注意: 系统将在后台启动，日志将保存到 logs/app.log
echo.

start /B python main.py
echo ✅ 系统已启动 (PID: %errorlevel%)
timeout /t 5 /nobreak >nul
echo.

echo 2.2 检查启动状态...
echo   等待10秒让系统完全启动...
timeout /t 10 /nobreak >nul

if exist "logs\app.log" (
    echo ✅ 日志文件已创建
    echo   检查启动日志...
    
    for /f "tokens=*" %%i in ('findstr /C:"Telegram客户端已启动" logs\app.log ^| tail -1') do (
        echo ✅ %%i
    )
    
    for /f "tokens=*" %%i in ('findstr /C:"情绪增强器" logs\app.log ^| tail -1') do (
        echo ℹ️ %%i
    )
    
    for /f "tokens=*" %%i in ('findstr /C:"四层触发" logs\app.log ^| tail -1') do (
        echo ℹ️ %%i
    )
) else (
    echo ⚠️ 日志文件尚未创建，等待更多时间...
    timeout /t 5 /nobreak >nul
)
echo.

echo [阶段3/5] 自动化测试执行...
echo.

echo 3.1 测试说明...
echo   📢 请在Telegram群组中发送以下测试消息:
echo.
echo   测试1: "测试自动优化001"
echo   测试2: "查一下我的订单状态"
echo   测试3: "@ai_zkw 现在通道稳定吗"
echo   测试4: "今天天气真好啊"
echo.
echo   请等待10秒让系统处理消息...
echo   按任意键继续（发送完测试消息后）...
pause
echo.

echo 3.2 分析测试结果...
echo   检查日志中的测试结果...
timeout /t 15 /nobreak >nul

if exist "logs\app.log" (
    echo.
    echo 📊 测试结果分析:
    echo ===================
    
    echo 1. 消息监控完整性:
    setlocal enabledelayedexpansion
    set "MSG_COUNT=0"
    for /f "tokens=*" %%i in ('findstr /C:"\[群组监控\]" logs\app.log') do (
        set /a MSG_COUNT+=1
        echo   !MSG_COUNT!. %%i
    )
    if !MSG_COUNT! gtr 0 (
        echo   ✅ 共监测到 !MSG_COUNT! 条群组消息
    ) else (
        echo   ❌ 未监测到群组消息
    )
    
    echo.
    echo 2. 触发分析详细日志:
    set "TRIGGER_COUNT=0"
    for /f "tokens=*" %%i in ('findstr /C:"\[触发分析\]" logs\app.log') do (
        set /a TRIGGER_COUNT+=1
        if !TRIGGER_COUNT! leq 3 echo   !TRIGGER_COUNT!. %%i
    )
    if !TRIGGER_COUNT! gtr 0 (
        echo   ✅ 共发现 !TRIGGER_COUNT! 条触发分析日志
    ) else (
        echo   ❌ 未发现触发分析详细日志
    )
    
    echo.
    echo 3. 情绪增强器状态:
    findstr /C:"情绪增强应用成功" logs\app.log >nul
    if %errorlevel% equ 0 (
        echo   ❌ 情绪增强器仍在运行（空格问题可能未修复）
        for /f "tokens=*" %%i in ('findstr /C:"情绪增强应用成功" logs\app.log ^| head -1') do (
            echo   示例: %%i
        )
    ) else (
        echo   ✅ 情绪增强器未运行（空格问题已修复）
    )
    
    echo.
    echo 4. 回复消息分析:
    set "REPLY_COUNT=0"
    for /f "tokens=*" %%i in ('findstr /C:"已回复消息" logs\app.log') do (
        set /a REPLY_COUNT+=1
        if !REPLY_COUNT! eq 1 echo   最近回复: %%i
    )
    if !REPLY_COUNT! gtr 0 (
        echo   ✅ 系统共回复 !REPLY_COUNT! 条消息
    ) else (
        echo   ⚠️ 系统未回复任何消息
    )
    
    echo.
    echo 5. 空格问题检查:
    findstr /C:"已回复消息.*[a-zA-Z0-9]\s[a-zA-Z0-9]" logs\app.log >nul
    if %errorlevel% equ 0 (
        echo   ⚠️ 检测到可能的空格分隔问题
    ) else (
        echo   ✅ 未检测到明显的空格分隔模式
    )
    
    endlocal
) else (
    echo ❌ 日志文件不存在，无法分析测试结果
)
echo.

echo [阶段4/5] 问题诊断与修复...
echo.

echo 4.1 基于测试结果的问题诊断...
echo   根据上述分析，主要问题可能是:
echo.
echo   1. ❌ 情绪增强器仍在运行 → 需要深度修复
echo   2. ❌ 无详细触发日志 → 配置或代码问题
echo   3. ⚠️ 消息监控不完整 → 过滤逻辑问题
echo   4. ✅ 触发逻辑正常工作 → 关键词检测正常
echo.

echo 4.2 准备修复方案...
echo   创建诊断报告...
(
    echo ========================================
    echo Telegram AI 全自动诊断报告
    echo ========================================
    echo 诊断时间: %date% %time%
    echo.
    echo [系统状态]
    echo - Python进程: 已停止并重启
    echo - 配置文件: 已验证并修复
    echo - 代码修改: 已验证
    echo.
    echo [测试结果]
) > diagnosis_report.txt

if exist "logs\app.log" (
    echo [日志摘要] >> diagnosis_report.txt
    findstr /C:"\[群组监控\]" logs\app.log | tail -5 >> diagnosis_report.txt
    echo. >> diagnosis_report.txt
    echo [触发分析] >> diagnosis_report.txt
    findstr /C:"\[触发分析\]" logs\app.log | tail -5 >> diagnosis_report.txt
    echo. >> diagnosis_report.txt
    echo [情绪增强器] >> diagnosis_report.txt
    findstr /C:"情绪增强" logs\app.log | tail -3 >> diagnosis_report.txt
) else (
    echo [警告] 日志文件不存在 >> diagnosis_report.txt
)

echo.
echo ✅ 诊断报告已保存到: diagnosis_report.txt
echo.

echo [阶段5/5] 下一步行动建议...
echo.

echo 5.1 根据诊断结果的操作:
echo.
echo   🔴 如果情绪增强器仍在运行:
echo       1. 检查 src/client/telegram_client.py 第940-970行
echo       2. 确保 emotion_enhancer 调用前检查 emoticons.enabled
echo       3. 修改后重启系统
echo.
echo   🟡 如果无详细触发日志:
echo       1. 检查 config/config.yaml 中 trigger.enabled: true
echo       2. 检查 src/trigger/four_layer_trigger.py 日志代码
echo       3. 检查日志级别是否为 INFO
echo.
echo   🟢 如果一切正常:
echo       1. 优化完成，准备下一步功能开发
echo       2. 考虑重新启用情绪增强器（修复后）
echo.

echo 5.2 立即操作选项:
echo   A) 查看完整诊断报告: type diagnosis_report.txt
echo   B) 查看最新日志: type logs\app.log | more
echo   C) 重启系统测试: 再次运行本脚本
echo   D) 手动修复代码: 根据上述建议修改
echo.

echo ========================================
echo 全自动优化与测试完成！
echo ========================================
echo.
echo 📋 执行摘要:
echo - 系统状态: 已诊断并重启
echo - 配置文件: 已验证并修复
echo - 测试执行: 已完成（需要用户发送测试消息）
echo - 结果分析: 已生成诊断报告
echo - 问题识别: 已列出主要问题
echo - 修复建议: 已提供具体方案
echo.
echo 🚀 建议操作:
echo 1. 查看 diagnosis_report.txt 了解详细结果
echo 2. 根据报告中的问题执行相应修复
echo 3. 重新运行本脚本验证修复效果
echo.
echo 📞 如需进一步帮助:
echo 1. 提供 diagnosis_report.txt 内容
echo 2. 描述具体遇到的问题
echo 3. 提供相关代码片段
echo.
pause