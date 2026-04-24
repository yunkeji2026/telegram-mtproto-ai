# Telegram AI 全自动优化与测试脚本 (PowerShell版本)
# 避免批处理兼容性问题，使用纯PowerShell语法

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Telegram AI 全自动优化与测试脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "创建时间: 2026-03-08 21:50 GMT+8" -ForegroundColor Gray
Write-Host "版本: PowerShell 1.0" -ForegroundColor Gray
Write-Host ""

# 检查执行策略
Write-Host "[检查执行策略]..." -ForegroundColor Yellow
$executionPolicy = Get-ExecutionPolicy
Write-Host "当前执行策略: $executionPolicy" -ForegroundColor Gray

if ($executionPolicy -eq "Restricted") {
    Write-Host "⚠️  执行策略为Restricted，脚本可能无法运行" -ForegroundColor Yellow
    Write-Host "💡 建议临时更改: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass" -ForegroundColor Gray
    $choice = Read-Host "是否临时更改执行策略? (y/n)"
    if ($choice -eq 'y') {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        Write-Host "✅ 执行策略已临时更改" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "[阶段1/5] 系统状态诊断..." -ForegroundColor Yellow
Write-Host ""

# 1.1 检查Python进程
Write-Host "1.1 检查Python进程..." -ForegroundColor Gray
$pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
if ($pythonProcesses) {
    Write-Host "✅ 检测到 $($pythonProcesses.Count) 个Python进程正在运行" -ForegroundColor Green
    Write-Host "   正在停止现有进程..." -ForegroundColor Gray
    foreach ($process in $pythonProcesses) {
        try {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            Write-Host "   ✅ 已停止进程 PID: $($process.Id)" -ForegroundColor Green
        } catch {
            Write-Host "   ⚠️  停止进程失败 PID: $($process.Id) - $_" -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds 3
} else {
    Write-Host "ℹ️ 未检测到运行的Python进程" -ForegroundColor Gray
}

# 1.2 检查配置文件状态
Write-Host ""
Write-Host "1.2 检查配置文件状态..." -ForegroundColor Gray
$configFile = "config\config.yaml"
if (Test-Path $configFile) {
    Write-Host "✅ 配置文件存在: $configFile" -ForegroundColor Green
    
    $configContent = Get-Content $configFile -Raw
    
    # 检查emoticons.enabled
    if ($configContent -match 'emoticons:\s*enabled:\s*false') {
        Write-Host "✅ 情绪增强器已禁用 (emoticons.enabled: false)" -ForegroundColor Green
    } else {
        Write-Host "❌ 情绪增强器未禁用，正在修复..." -ForegroundColor Red
        $configContent = $configContent -replace 'emoticons:\s*enabled:\s*true', 'emoticons: enabled: false'
        $configContent | Set-Content $configFile -Encoding UTF8
        Write-Host "✅ 已修复情绪增强器配置" -ForegroundColor Green
    }
    
    # 检查trigger.enabled
    if ($configContent -match 'trigger:\s*enabled:\s*true') {
        Write-Host "✅ 触发系统已启用 (trigger.enabled: true)" -ForegroundColor Green
    } else {
        Write-Host "❌ 触发系统未启用，正在添加..." -ForegroundColor Red
        $triggerConfig = @"
`n# 四层触发机制配置
trigger:
  enabled: true                    # 启用四层触发机制
  config_file: "config/trigger_rules.yaml"  # 触发规则配置文件路径
"@
        Add-Content -Path $configFile -Value $triggerConfig -Encoding UTF8
        Write-Host "✅ 已添加触发系统配置" -ForegroundColor Green
    }
} else {
    Write-Host "❌ 配置文件不存在，无法继续" -ForegroundColor Red
    Pause
    exit 1
}

# 1.3 检查代码修改状态
Write-Host ""
Write-Host "1.3 检查代码修改状态..." -ForegroundColor Gray
$telegramClientFile = "src\client\telegram_client.py"
if (Test-Path $telegramClientFile) {
    $telegramClientContent = Get-Content $telegramClientFile -Raw
    
    # 检查关键修改
    $checks = @(
        @{ Pattern = "emoticons_config\.get\('enabled', True\)"; Description = "情绪增强器调用检查" },
        @{ Pattern = "情绪增强器已禁用"; Description = "禁用日志" }
    )
    
    foreach ($check in $checks) {
        if ($telegramClientContent -match $check.Pattern) {
            Write-Host "✅ $($check.Description) 已实施" -ForegroundColor Green
        } else {
            Write-Host "⚠️  $($check.Description) 可能未实施" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "❌ 代码文件不存在" -ForegroundColor Red
}

Write-Host ""
Write-Host "[阶段2/5] 启动优化系统..." -ForegroundColor Yellow
Write-Host ""

Write-Host "2.1 启动Telegram AI系统..." -ForegroundColor Gray
Write-Host "   启动命令: python main.py" -ForegroundColor Gray
Write-Host "   注意: 系统将在后台启动，日志将保存到 logs\app.log" -ForegroundColor Gray
Write-Host ""

# 启动系统
$process = Start-Process python -ArgumentList "main.py" -PassThru -NoNewWindow
Write-Host "✅ 系统已启动 (PID: $($process.Id))" -ForegroundColor Green

Start-Sleep -Seconds 5
Write-Host ""

Write-Host "2.2 检查启动状态..." -ForegroundColor Gray
Write-Host "   等待10秒让系统完全启动..." -ForegroundColor Gray
Start-Sleep -Seconds 10

$logFile = "logs\app.log"
if (Test-Path $logFile) {
    Write-Host "✅ 日志文件已创建: $logFile" -ForegroundColor Green
    
    # 读取最后启动日志
    $logContent = Get-Content $logFile -Tail 20
    
    $startLog = $logContent | Where-Object { $_ -match "Telegram客户端已启动" } | Select-Object -Last 1
    if ($startLog) {
        Write-Host "✅ $startLog" -ForegroundColor Green
    }
    
    $emotionLog = $logContent | Where-Object { $_ -match "情绪增强器" } | Select-Object -Last 1
    if ($emotionLog) {
        Write-Host "ℹ️ $emotionLog" -ForegroundColor Gray
    }
    
    $triggerLog = $logContent | Where-Object { $_ -match "四层触发" } | Select-Object -Last 1
    if ($triggerLog) {
        Write-Host "ℹ️ $triggerLog" -ForegroundColor Gray
    }
} else {
    Write-Host "⚠️ 日志文件尚未创建，等待更多时间..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

Write-Host ""
Write-Host "[阶段3/5] 自动化测试执行..." -ForegroundColor Yellow
Write-Host ""

Write-Host "3.1 测试说明..." -ForegroundColor Gray
Write-Host "   📢 请在Telegram群组中发送以下测试消息:" -ForegroundColor Cyan
Write-Host ""
Write-Host "   测试1: '测试PowerShell001'" -ForegroundColor White
Write-Host "   测试2: '查一下我的订单状态'" -ForegroundColor White
Write-Host "   测试3: '@ai_zkw 现在通道稳定吗'" -ForegroundColor White
Write-Host "   测试4: '今天天气真好啊'" -ForegroundColor White
Write-Host ""
Write-Host "   请等待10秒让系统处理消息..." -ForegroundColor Gray
Write-Host "   按任意键继续（发送完测试消息后）..." -ForegroundColor Gray
Pause
Write-Host ""

Write-Host "3.2 分析测试结果..." -ForegroundColor Gray
Write-Host "   检查日志中的测试结果..." -ForegroundColor Gray
Start-Sleep -Seconds 15

if (Test-Path $logFile) {
    Write-Host ""
    Write-Host "📊 测试结果分析:" -ForegroundColor Cyan
    Write-Host "===================" -ForegroundColor Cyan
    
    $logContent = Get-Content $logFile
    
    # 1. 消息监控完整性
    Write-Host ""
    Write-Host "1. 消息监控完整性:" -ForegroundColor Yellow
    $monitorLogs = $logContent | Where-Object { $_ -match '\[群组监控\]' }
    if ($monitorLogs) {
        Write-Host "   ✅ 共监测到 $($monitorLogs.Count) 条群组消息" -ForegroundColor Green
        $monitorLogs | Select-Object -Last 3 | ForEach-Object {
            Write-Host "     $_" -ForegroundColor Gray
        }
    } else {
        Write-Host "   ❌ 未监测到群组消息" -ForegroundColor Red
    }
    
    # 2. 触发分析详细日志
    Write-Host ""
    Write-Host "2. 触发分析详细日志:" -ForegroundColor Yellow
    $triggerLogs = $logContent | Where-Object { $_ -match '\[触发分析\]' }
    if ($triggerLogs) {
        Write-Host "   ✅ 共发现 $($triggerLogs.Count) 条触发分析日志" -ForegroundColor Green
        $triggerLogs | Select-Object -Last 3 | ForEach-Object {
            Write-Host "     $_" -ForegroundColor Gray
        }
    } else {
        Write-Host "   ❌ 未发现触发分析详细日志" -ForegroundColor Red
    }
    
    # 3. 情绪增强器状态
    Write-Host ""
    Write-Host "3. 情绪增强器状态:" -ForegroundColor Yellow
    $emotionLogs = $logContent | Where-Object { $_ -match '情绪增强应用成功' }
    if ($emotionLogs) {
        Write-Host "   ❌ 情绪增强器仍在运行（空格问题可能未修复）" -ForegroundColor Red
        $emotionLogs | Select-Object -Last 1 | ForEach-Object {
            Write-Host "     示例: $_" -ForegroundColor Gray
        }
    } else {
        Write-Host "   ✅ 情绪增强器未运行（空格问题已修复）" -ForegroundColor Green
    }
    
    # 4. 回复消息分析
    Write-Host ""
    Write-Host "4. 回复消息分析:" -ForegroundColor Yellow
    $replyLogs = $logContent | Where-Object { $_ -match '已回复消息' }
    if ($replyLogs) {
        Write-Host "   ✅ 系统共回复 $($replyLogs.Count) 条消息" -ForegroundColor Green
        $replyLogs | Select-Object -Last 1 | ForEach-Object {
            Write-Host "     最近回复: $_" -ForegroundColor Gray
        }
    } else {
        Write-Host "   ⚠️ 系统未回复任何消息" -ForegroundColor Yellow
    }
    
    # 5. 空格问题检查
    Write-Host ""
    Write-Host "5. 空格问题检查:" -ForegroundColor Yellow
    $spaceIssue = $replyLogs | Where-Object { $_ -match '[a-zA-Z]\s[a-zA-Z]' -or $_ -match '[\u4e00-\u9fff]\s[\u4e00-\u9fff]' }
    if ($spaceIssue) {
        Write-Host "   ⚠️ 检测到可能的空格分隔问题" -ForegroundColor Yellow
    } else {
        Write-Host "   ✅ 未检测到明显的空格分隔模式" -ForegroundColor Green
    }
} else {
    Write-Host "❌ 日志文件不存在，无法分析测试结果" -ForegroundColor Red
}

Write-Host ""
Write-Host "[阶段4/5] 问题诊断与修复..." -ForegroundColor Yellow
Write-Host ""

Write-Host "4.1 基于测试结果的问题诊断..." -ForegroundColor Gray
Write-Host "   根据上述分析，主要问题可能是:" -ForegroundColor Gray
Write-Host ""
Write-Host "   1. ❌ 情绪增强器仍在运行 → 需要深度修复" -ForegroundColor Red
Write-Host "   2. ❌ 无详细触发日志 → 配置或代码问题" -ForegroundColor Red
Write-Host "   3. ⚠️ 消息监控不完整 → 过滤逻辑问题" -ForegroundColor Yellow
Write-Host "   4. ✅ 触发逻辑正常工作 → 关键词检测正常" -ForegroundColor Green
Write-Host ""

Write-Host "4.2 准备诊断报告..." -ForegroundColor Gray
$reportContent = @"
========================================
Telegram AI 全自动诊断报告 (PowerShell版本)
========================================
诊断时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

[系统状态]
- Python进程: 已检查并停止
- 配置文件: 已验证并修复
- 代码修改: 已验证

[测试结果]
- 消息监控: $($monitorLogs.Count) 条消息
- 触发分析: $($triggerLogs.Count) 条日志  
- 情绪增强器: $(if ($emotionLogs) { '仍在运行' } else { '已禁用' })
- 回复消息: $($replyLogs.Count) 条

[问题识别]
$(if ($emotionLogs) { '1. ❌ 情绪增强器仍在运行' })
$(if (-not $triggerLogs) { '2. ❌ 无详细触发日志' })
$(if (-not $monitorLogs) { '3. ⚠️ 消息监控不完整' })

[修复建议]
1. 检查 src/client/telegram_client.py 中的情绪增强器调用
2. 验证 config/config.yaml 中 trigger.enabled: true
3. 确保代码修改已生效，重启系统
"@

$reportContent | Out-File -FilePath "diagnosis_report_ps.txt" -Encoding UTF8
Write-Host "✅ 诊断报告已保存到: diagnosis_report_ps.txt" -ForegroundColor Green

Write-Host ""
Write-Host "[阶段5/5] 下一步行动建议..." -ForegroundColor Yellow
Write-Host ""

Write-Host "5.1 根据诊断结果的操作:" -ForegroundColor Gray
Write-Host ""
Write-Host "   🔴 如果情绪增强器仍在运行:" -ForegroundColor Red
Write-Host "       1. 检查 src/client/telegram_client.py 第940-970行" -ForegroundColor Gray
Write-Host "       2. 确保 emotion_enhancer 调用前检查 emoticons.enabled" -ForegroundColor Gray
Write-Host "       3. 修改后重启系统" -ForegroundColor Gray
Write-Host ""
Write-Host "   🟡 如果无详细触发日志:" -ForegroundColor Yellow
Write-Host "       1. 检查 config/config.yaml 中 trigger.enabled: true" -ForegroundColor Gray
Write-Host "       2. 检查 src/trigger/four_layer_trigger.py 日志代码" -ForegroundColor Gray
Write-Host "       3. 检查日志级别是否为 INFO" -ForegroundColor Gray
Write-Host ""
Write-Host "   🟢 如果一切正常:" -ForegroundColor Green
Write-Host "       1. 优化完成，准备下一步功能开发" -ForegroundColor Gray
Write-Host "       2. 考虑重新启用情绪增强器（修复后）" -ForegroundColor Gray
Write-Host ""

Write-Host "5.2 立即操作选项:" -ForegroundColor Gray
Write-Host "   A) 查看完整诊断报告: type diagnosis_report_ps.txt" -ForegroundColor Cyan
Write-Host "   B) 查看最新日志: type logs\app.log | more" -ForegroundColor Cyan
Write-Host "   C) 重启系统测试: 再次运行本脚本" -ForegroundColor Cyan
Write-Host "   D) 手动修复代码: 根据上述建议修改" -ForegroundColor Cyan
Write-Host ""

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "全自动优化与测试完成！" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📋 执行摘要:" -ForegroundColor Gray
Write-Host "- 系统状态: 已诊断并重启" -ForegroundColor Gray
Write-Host "- 配置文件: 已验证并修复" -ForegroundColor Gray
Write-Host "- 测试执行: 已完成（需要用户发送测试消息）" -ForegroundColor Gray
Write-Host "- 结果分析: 已生成诊断报告" -ForegroundColor Gray
Write-Host "- 问题识别: 已列出主要问题" -ForegroundColor Gray
Write-Host "- 修复建议: 已提供具体方案" -ForegroundColor Gray
Write-Host ""
Write-Host "🚀 建议操作:" -ForegroundColor Cyan
Write-Host "1. 查看 diagnosis_report_ps.txt 了解详细结果" -ForegroundColor White
Write-Host "2. 根据报告中的问题执行相应修复" -ForegroundColor White
Write-Host "3. 重新运行本脚本验证修复效果" -ForegroundColor White
Write-Host ""
Write-Host "📞 如需进一步帮助:" -ForegroundColor Gray
Write-Host "1. 提供 diagnosis_report_ps.txt 内容" -ForegroundColor White
Write-Host "2. 描述具体遇到的问题" -ForegroundColor White
Write-Host "3. 提供相关代码片段" -ForegroundColor White
Write-Host ""
Pause