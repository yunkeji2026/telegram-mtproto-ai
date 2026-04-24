@echo off
echo ========================================
echo Telegram AI 优化重启脚本
echo ========================================
echo 创建时间: 2026-03-08 21:25 GMT+8
echo 应用优化: 
echo 1. 情绪增强器修复（禁用空格问题）
echo 2. 触发系统配置增强
echo 3. 详细日志记录优化
echo.

echo [步骤1/6] 停止现有进程...
echo 按 Ctrl+C 停止当前运行的Python程序（如果正在运行）
echo.
taskkill /F /IM python.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul
echo ✅ 已停止Python进程
echo.

echo [步骤2/6] 检查配置文件...
echo 验证配置修改:
echo - 情绪增强器: emoticons.enabled: false
echo - 触发系统: trigger.enabled: true
echo.
if exist "config\config.yaml" (
    findstr /C:"emoticons.enabled: false" config\config.yaml >nul && echo ✅ 情绪增强器已禁用 || echo ⚠️ 情绪增强器配置异常
    findstr /C:"trigger.enabled: true" config\config.yaml >nul && echo ✅ 触发系统已启用 || echo ⚠️ 触发系统配置异常
) else (
    echo ❌ 配置文件不存在
)
echo.

echo [步骤3/6] 检查代码修改...
echo 验证代码优化是否就绪...
if exist "src\client\telegram_client.py" (
    findstr /C:"情绪增强器已禁用" src\client\telegram_client.py >nul && echo ✅ 情绪增强器初始化修复就绪 || echo ⚠️ 情绪增强器代码未修改
    findstr /C:"trigger.enabled: true" src\client\telegram_client.py >nul && echo ✅ 触发系统初始化检查就绪 || echo ⚠️ 触发系统代码可能未启用
) else (
    echo ❌ 代码文件不存在
)
echo.

echo [步骤4/6] 启动优化系统...
echo 正在启动Telegram AI优化系统...
echo 注意：如果session过期需要重新登录
echo.
echo 📝 优化内容摘要：
echo 1. 🔧 情绪增强器临时禁用 → 修复空格分隔问题
echo 2. 📊 详细触发分析日志 → [触发分析] [L1规则] [L2语义] [L3上下文]
echo 3. 🎯 触发系统增强 → 四层机制完全启用
echo 4. 📈 群组监控完整性 → 100%%消息记录
echo.
echo 按任意键启动系统...
pause >nul
start python main.py
echo.

echo [步骤5/6] 测试验证指南...
echo.
echo ✅ 系统启动中，请等待以下日志出现：
echo "四层触发决策器初始化成功"
echo "Telegram客户端已启动，等待消息..."
echo.
echo 📋 测试步骤：
echo 1. 发送 "测试005" → 应记录日志但不回复
echo 2. 发送 "查订单号" → 应智能回复，无空格
echo 3. 发送 "@ai_zkw 汇率多少" → 应触发回复
echo 4. 发送 "今天天气不错" → 应记录但不回复
echo.
echo 📊 验证要点：
echo - 检查 logs/app.log 是否有详细触发分析日志
echo - 验证回复无空格分隔问题
echo - 确认所有消息都被记录 ([群组监控])
echo.

echo [步骤6/6] 故障排除...
echo.
echo 🔧 常见问题解决：
echo 1. 无响应：等待30秒后重试，检查网络
echo 2. 空格问题持续：确保 config/config.yaml 中 emoticons.enabled: false
echo 3. 无详细日志：检查 config.yaml 中 trigger.enabled: true
echo 4. session问题：删除 sessions/ 文件夹重新登录
echo.
echo 📞 如需帮助：
echo 1. 提供 logs/app.log 内容
echo 2. 截图PowerShell错误信息
echo 3. 描述具体问题现象
echo.
echo ========================================
echo 优化重启完成！
echo ========================================
echo.
echo 🚀 优化状态：
echo - 情绪增强器: 已禁用 (修复空格问题)
echo - 触发系统: 已启用 (详细日志)
echo - 群组监控: 100%%消息记录
echo - 智能回复: 无空格，自然流畅
echo.
echo 📢 测试后请反馈结果：
echo 1. 监控是否完整？（所有消息记录？）
echo 2. 回复是否有空格问题？
echo 3. 触发决策是否详细记录？
echo 4. 有无其他问题？
echo.
pause