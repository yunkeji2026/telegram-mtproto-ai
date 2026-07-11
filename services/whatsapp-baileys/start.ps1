# start.ps1 — 启动 WhatsApp (Baileys) 协议扫码登录微服务
# 用法: pwsh -File start.ps1   (在 services/whatsapp-baileys 下)
# 说明: 为主进程提供网页二维码登录 + 多账号连接；入站消息回推统一收件箱。
#       主进程需开 config.platform_login.whatsapp.protocol_enabled=true 并指向 baileys_url。

$ErrorActionPreference = "Stop"
$root = "D:\workspace\telegram-mtproto-ai"

# 服务监听端口（须与主进程 platform_login.whatsapp.baileys_url 一致）
$env:PORT = "8790"
# 入站桥：Baileys 收到的消息 push 进统一收件箱（web 后台 18799）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 会话健康桥：连上/被登出/重连放弃等状态转移主动 push（不配则由 PY_INGEST_URL 自动推导）
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"
# ingest endpoint requires Bearer auth (web_admin.auth_token). Must match config.yaml::web_admin.auth_token
# or inbound pushes are rejected (401) and messages are silently dropped.
$env:PY_API_TOKEN = "admin"
# 首连历史回填条数（0 关闭）
$env:WA_BACKFILL = "20"
# P0 同步开关：好友名单(通讯录) + 全量会话列表；会话占位上限防洪泛（默认开）
$env:WA_SYNC_CONTACTS = "1"
$env:WA_SYNC_CHATS = "1"
$env:WA_CHATS_MAX = "500"
# P2 群聊接入（入站落「群组动态」；置 0 回到只私聊）
$env:WA_SYNC_GROUPS = "1"
# P4-3/P4-4/P4-5A/P4-6A 消息级富交互：表情回应 + 已读回执 + 对端输入状态 + 编辑/撤回（置 0 关闭）
$env:WA_SYNC_REACTIONS = "1"
$env:WA_SYNC_RECEIPTS = "1"
$env:WA_SYNC_PRESENCE = "1"
$env:WA_SYNC_EDITS = "1"
# 媒体落地到 Python 静态目录（前端按 /static URL 加载）
$env:WA_MEDIA_DIR = "$root\src\web\static\protocol_media\whatsapp"
$env:WA_MEDIA_URL_BASE = "/static/protocol_media/whatsapp"
$env:LOG_LEVEL = "info"

Write-Host "[wa-baileys] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
# 日志落文件（隐藏窗口的计划任务下 stdout 会丢失，落盘便于事后排查连接/登出等问题）
$logDir = Join-Path $root "services\whatsapp-baileys\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("wa-baileys-" + (Get-Date -Format "yyyyMMdd") + ".log")
node server.js *>> $log
