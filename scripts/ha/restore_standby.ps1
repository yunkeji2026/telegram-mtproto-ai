# P7-1：standby 节点从 MinIO/S3 restore SQLite → 升级为 primary
#
# 用法：
#   .\scripts\ha\restore_standby.ps1 -MinioEndpoint http://minio-host:9000
#
# 流程：
#   1) 停本地 python 主进程（若在跑）
#   2) 用 litestream CLI 把 3 个 DB restore 到 data/
#   3) 重启主进程 → 它会尝试 acquire leader lock（若原 primary 已挂 TTL 会到期让出）
#
# 前置：
#   - 本机已安装 litestream（choco install litestream 或 go install）
#   - LITESTREAM_ACCESS_KEY_ID / LITESTREAM_SECRET_ACCESS_KEY 环境变量已配置

param(
    [string]$MinioEndpoint = "http://127.0.0.1:9000",
    [string]$Bucket = "rpa-backup",
    [string]$DataDir = "data"
)

$ErrorActionPreference = "Stop"

Write-Host "[ha] P7-1 standby restore starting..." -ForegroundColor Cyan
Write-Host "[ha] endpoint=$MinioEndpoint bucket=$Bucket dataDir=$DataDir"

# 停原进程（若存在）
$py = Get-Process -Name "python" -ErrorAction SilentlyContinue
if ($py) {
    Write-Host "[ha] stopping running python processes..." -ForegroundColor Yellow
    $py | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# 备份现有 DB（防误操作）
if (Test-Path "$DataDir\messenger_rpa.sqlite") {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupDir = "$DataDir\pre-restore-$stamp"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Get-ChildItem "$DataDir\*.sqlite" | Copy-Item -Destination $backupDir
    Write-Host "[ha] local DB backed up to $backupDir"
}

# Restore 每个 DB
$dbs = @(
    @{ path = "messenger_rpa.sqlite";          s3key = "messenger_rpa" },
    @{ path = "messenger_rpa_approvals.sqlite"; s3key = "messenger_rpa_approvals" },
    @{ path = "bot.db";                         s3key = "bot" }
)

foreach ($db in $dbs) {
    $localPath = Join-Path $DataDir $db.path
    $s3Url = "s3://$Bucket/$($db.s3key)"
    Write-Host "[ha] restoring $($db.path) from $s3Url"
    if (Test-Path $localPath) { Remove-Item $localPath -Force }
    litestream restore `
        -o $localPath `
        -config "docker\litestream.yml" `
        $localPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ha] ERROR: restore failed for $($db.path)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[ha] OK $($db.path) restored ($(Get-Item $localPath).Length bytes)"
}

Write-Host "[ha] restore complete. Start main process to contend leader lock." -ForegroundColor Green
Write-Host "[ha] next: python main.py"
