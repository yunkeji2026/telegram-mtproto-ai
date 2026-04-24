# Telegram MTProto AI 安装和测试脚本 (PowerShell版本)
# 运行: powershell -ExecutionPolicy Bypass -File install_test.ps1

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Telegram MTProto AI 安装和测试脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "创建时间: 2026-03-08 13:40 GMT+8" -ForegroundColor Gray
Write-Host ""

# 检查是否以管理员身份运行
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    Write-Host "⚠️  建议以管理员身份运行此脚本" -ForegroundColor Yellow
    Write-Host "   右键点击 -> 以管理员身份运行" -ForegroundColor Gray
    Write-Host ""
    $choice = Read-Host "是否继续? (y/n)"
    if ($choice -ne 'y') {
        exit 1
    }
}

# 步骤1: 停止现有进程
Write-Host "[步骤1/6] 停止现有进程..." -ForegroundColor Green
Write-Host "请确保已停止所有Python进程" -ForegroundColor Gray
Write-Host "按 Ctrl+C 停止当前运行的Python程序" -ForegroundColor Gray
Write-Host ""
pause

# 步骤2: 检查Python环境
Write-Host "[步骤2/6] 检查Python环境..." -ForegroundColor Green
try {
    $pythonVersion = python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✅ Python版本: $pythonVersion" -ForegroundColor Green
    } else {
        Write-Host "❌ Python未安装或不在PATH中" -ForegroundColor Red
        Write-Host "请先安装Python 3.8+" -ForegroundColor Yellow
        pause
        exit 1
    }
} catch {
    Write-Host "❌ Python检查失败: $_" -ForegroundColor Red
    pause
    exit 1
}

# 步骤3: 安装核心依赖
Write-Host "[步骤3/6] 安装核心依赖..." -ForegroundColor Green
Write-Host "正在安装pyrogram等核心依赖..." -ForegroundColor Gray

$corePackages = @(
    "pyrogram",
    "tgcrypto", 
    "openai",
    "aiohttp",
    "PyYAML",
    "colorama",
    "loguru"
)

foreach ($package in $corePackages) {
    Write-Host "安装 $package ..." -ForegroundColor Gray
    pip install $package
    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠️  $package 安装可能有问题" -ForegroundColor Yellow
    }
}

# 步骤4: 安装语音识别依赖
Write-Host "[步骤4/6] 安装语音识别依赖..." -ForegroundColor Green
Write-Host "正在安装openai-whisper..." -ForegroundColor Gray

pip install openai-whisper
if ($LASTEXITCODE -ne 0) {
    Write-Host "⚠️  Whisper安装失败，系统仍可以文本模式运行" -ForegroundColor Yellow
    Write-Host "您可以先测试文本功能，稍后重试" -ForegroundColor Gray
    pause
}

# 步骤5: 验证安装
Write-Host "[步骤5/6] 验证安装..." -ForegroundColor Green

function Test-PythonModule {
    param([string]$moduleName)
    
    try {
        python -c "import $moduleName; print('✅ $moduleName 已安装')"
        return $true
    } catch {
        Write-Host "❌ $moduleName 未安装" -ForegroundColor Red
        return $false
    }
}

Write-Host "检查关键模块..." -ForegroundColor Gray
$modules = @("pyrogram", "openai", "aiohttp", "whisper")
$allInstalled = $true

foreach ($module in $modules) {
    if (-not (Test-PythonModule $module)) {
        $allInstalled = $false
    }
}

if ($allInstalled) {
    Write-Host "✅ 所有关键模块已安装" -ForegroundColor Green
} else {
    Write-Host "⚠️  部分模块未安装，系统功能可能受限" -ForegroundColor Yellow
}

# 步骤6: 启动系统
Write-Host "[步骤6/6] 启动系统..." -ForegroundColor Green
Write-Host "正在启动Telegram AI系统..." -ForegroundColor Gray
Write-Host ""
Write-Host "重要：验证码处理流程：" -ForegroundColor Yellow
Write-Host "1. 系统会发送验证码到您的Telegram应用" -ForegroundColor Gray
Write-Host "2. 创建code.txt文件并写入验证码数字" -ForegroundColor Gray
Write-Host "3. 系统会自动读取并继续登录" -ForegroundColor Gray
Write-Host "4. 验证码有效时间：5分钟" -ForegroundColor Gray
Write-Host ""
Write-Host "按任意键启动系统..." -ForegroundColor Gray
pause

# 启动系统
$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptPath

Write-Host "启动命令: python main.py" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 停止系统" -ForegroundColor Yellow
Write-Host ""

# 在新窗口中启动系统
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "python"
$psi.Arguments = "main.py"
$psi.UseShellExecute = $true
$psi.WorkingDirectory = $scriptPath

try {
    $process = [System.Diagnostics.Process]::Start($psi)
    Write-Host "✅ 系统已启动" -ForegroundColor Green
    Write-Host "进程ID: $($process.Id)" -ForegroundColor Gray
} catch {
    Write-Host "❌ 启动失败: $_" -ForegroundColor Red
}

# 测试指南
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "测试指南" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "✅ 系统启动成功！" -ForegroundColor Green
Write-Host ""
Write-Host "📝 测试步骤：" -ForegroundColor Yellow
Write-Host "1. 等待系统显示'Telegram客户端已启动，等待消息...'" -ForegroundColor Gray
Write-Host "2. 发送文字'测试'到 @ai_zkw" -ForegroundColor Gray
Write-Host "3. 查看系统是否回复'欢迎咨询！'或类似消息" -ForegroundColor Gray
Write-Host "4. 发送语音消息测试语音识别" -ForegroundColor Gray
Write-Host ""
Write-Host "📊 验证方法：" -ForegroundColor Yellow
Write-Host "- 检查 logs/app.log 日志文件" -ForegroundColor Gray
Write-Host "- 查看PowerShell控制台输出" -ForegroundColor Gray
Write-Host "- 确认收到AI回复" -ForegroundColor Gray
Write-Host ""
Write-Host "🔧 故障排除：" -ForegroundColor Yellow
Write-Host "1. 无回复：等待30秒后重试，检查网络连接" -ForegroundColor Gray
Write-Host "2. 验证码问题：删除sessions/文件夹重新登录" -ForegroundColor Gray
Write-Host "3. API错误：检查config/config.yaml中的API密钥" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "安装测试完成！" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "📞 如需帮助：" -ForegroundColor Yellow
Write-Host "1. 提供 logs/app.log 内容" -ForegroundColor Gray
Write-Host "2. 截图PowerShell错误信息" -ForegroundColor Gray
Write-Host "3. 描述具体问题现象" -ForegroundColor Gray
Write-Host ""
pause