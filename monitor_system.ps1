<#
.SYNOPSIS
Telegram MTProto AI 系统监控脚本

.DESCRIPTION
自动启动系统并实时监控日志，检测关键问题：
1. 系统启动状态
2. [群组监控] 消息记录
3. [触发分析] 决策过程
4. 情绪增强器状态
5. 空格问题检测
6. AI回复质量

.PARAMETER Action
运行模式：run（启动+监控），monitor（仅监控），diagnose（诊断）

.EXAMPLE
.\monitor_system.ps1 -Action run     # 启动系统并监控
.\monitor_system.ps1 -Action monitor # 仅监控现有系统
.\monitor_system.ps1 -Action diagnose # 系统诊断
#>

param(
    [ValidateSet("run", "monitor", "diagnose")]
    [string]$Action = "monitor"
)

# 配置
$LogFile = "logs/app.log"
$ConfigFile = "config/config.yaml"
$SessionDir = "sessions/"
$StartCommand = "python main.py"
$Encoding = "UTF8"

# 颜色定义
$ColorInfo = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorSuccess = "Cyan"
$ColorDebug = "Gray"

# 初始化
function Initialize-Monitor {
    Write-Host "🔧 Telegram AI 系统监控脚本" -ForegroundColor $ColorSuccess
    Write-Host "==========================================" -ForegroundColor $ColorDebug
    
    # 自动检测并切换到正确目录
    $originalDir = Get-Location
    $projectDir = $originalDir
    
    # 检查当前目录是否有 config/config.yaml
    if (-not (Test-Path "config/config.yaml")) {
        # 尝试切换到 telegram-mtproto-ai 子目录
        $candidateDir = Join-Path $originalDir "telegram-mtproto-ai"
        if (Test-Path $candidateDir) {
            Set-Location $candidateDir
            $projectDir = $candidateDir
            Write-Host "📁 自动切换到项目目录: $projectDir" -ForegroundColor $ColorSuccess
        }
    }
    
    # 再次检查配置文件是否存在
    if (-not (Test-Path "config/config.yaml")) {
        Write-Host "❌ 错误：找不到配置文件 config/config.yaml" -ForegroundColor $ColorError
        Write-Host "   当前目录: $projectDir" -ForegroundColor $ColorError
        Write-Host "   请在 telegram-mtproto-ai 目录中执行此脚本" -ForegroundColor $ColorError
        Set-Location $originalDir
        exit 1
    }
    
    # 检查Python
    try {
        $pythonVersion = python --version 2>&1
        Write-Host "✅ Python 环境: $pythonVersion" -ForegroundColor $ColorSuccess
    } catch {
        Write-Host "❌ Python 未找到或不在 PATH 中" -ForegroundColor $ColorError
        exit 1
    }
    
    # 检查配置文件
    if (Test-Path $ConfigFile) {
        Write-Host "✅ 配置文件存在: $ConfigFile" -ForegroundColor $ColorSuccess
    } else {
        Write-Host "⚠️ 配置文件不存在: $ConfigFile" -ForegroundColor $ColorWarning
    }
    
    # 检查会话文件
    if (Test-Path $SessionDir) {
        $sessionFiles = Get-ChildItem $SessionDir -Filter "*.session" -ErrorAction SilentlyContinue
        if ($sessionFiles.Count -gt 0) {
            Write-Host "✅ 会话文件存在: $($sessionFiles.Count) 个文件" -ForegroundColor $ColorSuccess
        } else {
            Write-Host "⚠️ 会话目录存在但无 .session 文件" -ForegroundColor $ColorWarning
        }
    } else {
        Write-Host "⚠️ 会话目录不存在: $SessionDir" -ForegroundColor $ColorWarning
    }
}

# 系统诊断
function Diagnose-System {
    Write-Host "🔍 系统诊断开始" -ForegroundColor $ColorSuccess
    
    # 1. 检查进程
    $pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
    if ($pythonProcesses.Count -gt 0) {
        Write-Host "✅ Python 进程运行中: $($pythonProcesses.Count) 个进程" -ForegroundColor $ColorSuccess
        foreach ($proc in $pythonProcesses) {
            Write-Host "   - PID: $($proc.Id), 启动时间: $($proc.StartTime)" -ForegroundColor $ColorDebug
        }
    } else {
        Write-Host "⚠️ 无 Python 进程运行" -ForegroundColor $ColorWarning
    }
    
    # 2. 检查日志文件
    if (Test-Path $LogFile) {
        $logSize = (Get-Item $LogFile).Length
        $logLines = (Get-Content $LogFile | Measure-Object -Line).Lines
        Write-Host "✅ 日志文件存在: 大小 $([math]::Round($logSize/1KB, 2)) KB, $logLines 行" -ForegroundColor $ColorSuccess
        
        # 分析最后20行
        $lastLines = Get-Content $LogFile -Tail 20 -ErrorAction SilentlyContinue
        if ($lastLines) {
            Write-Host "📋 日志最后20行摘要:" -ForegroundColor $ColorInfo
            foreach ($line in $lastLines) {
                if ($line -match '\[ERROR\]') {
                    Write-Host "   ❌ $line" -ForegroundColor $ColorError
                } elseif ($line -match '\[WARNING\]') {
                    Write-Host "   ⚠️ $line" -ForegroundColor $ColorWarning
                } elseif ($line -match '情绪增强应用成功') {
                    Write-Host "   ⚠️ $line" -ForegroundColor $ColorWarning -NoNewline
                    if ($line -match '[\u4e00-\u9fff]\s[\u4e00-\u9fff]') {
                        Write-Host " (检测到空格问题)" -ForegroundColor $ColorError
                    }
                } elseif ($line -match '\[群组监控\]') {
                    Write-Host "   👁️ $line" -ForegroundColor $ColorInfo
                } elseif ($line -match '\[触发分析\]') {
                    Write-Host "   🔍 $line" -ForegroundColor $ColorSuccess
                } elseif ($line -match '已回复消息') {
                    Write-Host "   💬 $line" -ForegroundColor $ColorSuccess
                }
            }
        }
    } else {
        Write-Host "⚠️ 日志文件不存在: $LogFile" -ForegroundColor $ColorWarning
    }
    
    # 3. 检查关键配置
    if (Test-Path $ConfigFile) {
        $configContent = Get-Content $ConfigFile -Raw
        
        # 检查情绪增强器
        if ($configContent -match 'emoticons:\s*enabled:\s*(true|false)') {
            $emoticonsEnabled = $matches[1] -eq 'true'
            if ($emoticonsEnabled) {
                Write-Host "❌ 情绪增强器已启用 (可能导致空格问题)" -ForegroundColor $ColorError
            } else {
                Write-Host "✅ 情绪增强器已禁用" -ForegroundColor $ColorSuccess
            }
        }
        
        # 检查触发系统
        if ($configContent -match 'trigger:\s*enabled:\s*(true|false)') {
            $triggerEnabled = $matches[1] -eq 'true'
            if ($triggerEnabled) {
                Write-Host "✅ 触发系统已启用" -ForegroundColor $ColorSuccess
            } else {
                Write-Host "⚠️ 触发系统未启用" -ForegroundColor $ColorWarning
            }
        }
        
        # 检查AI模型
        if ($configContent -match 'model:\s*"([^"]+)"') {
            $model = $matches[1]
            Write-Host "✅ AI模型: $model" -ForegroundColor $ColorSuccess
        }
    }
    
    Write-Host "🔍 系统诊断完成" -ForegroundColor $ColorSuccess
}

# 启动系统
function Start-System {
    Write-Host "🚀 启动 Telegram AI 系统..." -ForegroundColor $ColorSuccess
    
    # 检查是否已运行
    $pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
    if ($pythonProcesses.Count -gt 0) {
        Write-Host "⚠️ 发现正在运行的 Python 进程，正在停止..." -ForegroundColor $ColorWarning
        Stop-Process -Name python -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    
    # 设置环境变量
    $env:PYTHONIOENCODING = 'utf-8'
    
    # 启动命令
    Write-Host "▶️ 执行: $StartCommand" -ForegroundColor $ColorInfo
    
    try {
        # 在后台启动进程
        $processInfo = New-Object System.Diagnostics.ProcessStartInfo
        $processInfo.FileName = "python"
        $processInfo.Arguments = "main.py"
        $processInfo.WorkingDirectory = (Get-Location).Path
        $processInfo.UseShellExecute = $false
        $processInfo.RedirectStandardOutput = $true
        $processInfo.RedirectStandardError = $true
        $processInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $processInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $processInfo
        $process.Start() | Out-Null
        
        # 读取前几行输出
        Start-Sleep -Seconds 3
        
        if (-not $process.HasExited) {
            Write-Host "✅ 系统启动成功 (PID: $($process.Id))" -ForegroundColor $ColorSuccess
            
            # 读取初始输出
            $output = $process.StandardOutput.ReadToEndAsync()
            $errorOutput = $process.StandardError.ReadToEndAsync()
            
            Start-Sleep -Seconds 1
            
            if ($output.Result) {
                $outputLines = $output.Result -split "`n"
                foreach ($line in $outputLines | Select-Object -First 10) {
                    Write-Host "   $line" -ForegroundColor $ColorDebug
                }
            }
            
            return $process
        } else {
            Write-Host "❌ 系统启动后立即退出" -ForegroundColor $ColorError
            if ($errorOutput.Result) {
                Write-Host "错误输出:" -ForegroundColor $ColorError
                Write-Host $errorOutput.Result -ForegroundColor $ColorError
            }
            return $null
        }
    } catch {
        Write-Host "❌ 启动失败: $_" -ForegroundColor $ColorError
        return $null
    }
}

# 监控日志
function Monitor-Logs {
    param(
        [int]$TailLines = 20,
        [int]$UpdateInterval = 2
    )
    
    Write-Host "👁️ 开始监控日志: $LogFile" -ForegroundColor $ColorSuccess
    Write-Host "   按 Ctrl+C 停止监控" -ForegroundColor $ColorDebug
    
    $lastPosition = 0
    $alertCounts = @{
        "群组监控" = 0
        "触发分析" = 0
        "情绪增强" = 0
        "空格问题" = 0
        "AI回复" = 0
        "错误" = 0
    }
    
    # 如果日志文件不存在，等待它创建
    while (-not (Test-Path $LogFile)) {
        Write-Host "⏳ 等待日志文件创建..." -ForegroundColor $ColorWarning
        Start-Sleep -Seconds $UpdateInterval
    }
    
    # 读取现有内容
    if (Test-Path $LogFile) {
        $initialContent = Get-Content $LogFile -Tail $TailLines -ErrorAction SilentlyContinue
        if ($initialContent) {
            Write-Host "📋 最后 $TailLines 行日志:" -ForegroundColor $ColorInfo
            foreach ($line in $initialContent) {
                Process-LogLine $line $alertCounts
            }
        }
        $lastPosition = (Get-Item $LogFile).Length
    }
    
    # 持续监控
    while ($true) {
        try {
            if (Test-Path $LogFile) {
                $currentSize = (Get-Item $LogFile).Length
                
                if ($currentSize -gt $lastPosition) {
                    # 读取新内容
                    $stream = [System.IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
                    $stream.Position = $lastPosition
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                    
                    while (-not $reader.EndOfStream) {
                        $line = $reader.ReadLine()
                        if ($line) {
                            Process-LogLine $line $alertCounts
                        }
                    }
                    
                    $reader.Close()
                    $stream.Close()
                    $lastPosition = $currentSize
                }
            }
            
            # 更新统计显示
            Write-Host "📊 统计: " -NoNewline -ForegroundColor $ColorDebug
            Write-Host "监控[$($alertCounts['群组监控'])] " -NoNewline -ForegroundColor $ColorInfo
            Write-Host "触发[$($alertCounts['触发分析'])] " -NoNewline -ForegroundColor $ColorSuccess
            Write-Host "回复[$($alertCounts['AI回复'])] " -NoNewline -ForegroundColor $ColorSuccess
            Write-Host "错误[$($alertCounts['错误'])]" -ForegroundColor $(if ($alertCounts['错误'] -gt 0) { $ColorError } else { $ColorDebug })
            
            Start-Sleep -Seconds $UpdateInterval
            
        } catch {
            Write-Host "❌ 监控错误: $_" -ForegroundColor $ColorError
            Start-Sleep -Seconds $UpdateInterval
        }
    }
}

# 处理日志行
function Process-LogLine {
    param(
        [string]$line,
        [hashtable]$alertCounts
    )
    
    # 检测关键模式
    if ($line -match '\[ERROR\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ❌ $line" -ForegroundColor $ColorError
        $alertCounts["错误"]++
    }
    elseif ($line -match '\[WARNING\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ⚠️ $line" -ForegroundColor $ColorWarning
    }
    elseif ($line -match '情绪增强应用成功') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ⚠️ 情绪增强: $line" -ForegroundColor $ColorWarning -NoNewline
        $alertCounts["情绪增强"]++
        if ($line -match '[\u4e00-\u9fff]\s[\u4e00-\u9fff]') {
            Write-Host " (检测到空格问题)" -ForegroundColor $ColorError
            $alertCounts["空格问题"]++
        } else {
            Write-Host ""
        }
    }
    elseif ($line -match '\[群组监控\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 👁️ $line" -ForegroundColor $ColorInfo
        $alertCounts["群组监控"]++
    }
    elseif ($line -match '\[触发分析\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 🔍 $line" -ForegroundColor $ColorSuccess
        $alertCounts["触发分析"]++
    }
    elseif ($line -match '\[L1规则\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 📋 $line" -ForegroundColor $ColorSuccess
    }
    elseif ($line -match '\[L2语义\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 🧠 $line" -ForegroundColor $ColorSuccess
    }
    elseif ($line -match '\[L3上下文\]') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 🔄 $line" -ForegroundColor $ColorSuccess
    }
    elseif ($line -match '已回复消息' -or $line -match '回复消息') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 💬 $line" -ForegroundColor $ColorSuccess
        $alertCounts["AI回复"]++
    }
    elseif ($line -match 'DeepSeek.*初始化成功' -or $line -match 'AI.*连接成功') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ✅ $line" -ForegroundColor $ColorSuccess
    }
    elseif ($line -match '登录用户' -or $line -match '客户端已启动') {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 🚀 $line" -ForegroundColor $ColorSuccess
    }
    # 默认显示调试信息
    elseif ($line -match '\[DEBUG\]' -or $line -match '\[INFO\]') {
        # 可选：显示所有INFO/DEBUG日志
        # Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ℹ️ $line" -ForegroundColor $ColorDebug
    }
}

# 主执行流程
Initialize-Monitor

switch ($Action) {
    "run" {
        Write-Host "🎯 模式: 启动系统并监控" -ForegroundColor $ColorSuccess
        $process = Start-System
        if ($process) {
            Monitor-Logs
        }
    }
    "monitor" {
        Write-Host "🎯 模式: 仅监控现有系统" -ForegroundColor $ColorSuccess
        Monitor-Logs
    }
    "diagnose" {
        Write-Host "🎯 模式: 系统诊断" -ForegroundColor $ColorSuccess
        Diagnose-System
    }
}

Write-Host "👋 监控脚本结束" -ForegroundColor $ColorSuccess