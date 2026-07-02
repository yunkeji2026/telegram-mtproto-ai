# start.ps1 — 启动 WhatsApp (Baileys) 协议扫码登录微服务
# 用法: pwsh -File start.ps1   (在 services/whatsapp-baileys 下)
# 说明: 为主进程提供网页二维码登录 + 多账号连接；入站消息回推统一收件箱。
#       主进程需开 config.platform_login.whatsapp.protocol_enabled=true 并指向 baileys_url。

$ErrorActionPreference = "Stop"
$root = "D:\workspace\telegram-mtproto-ai"

# 服务监听端口（须与主进程 platform_login.whatsapp.baileys_url 一致）
$env:PORT = "8790"
# 入站桥：Baileys 收到的消息 push 进统一收件箱（web 后台 18799，本机无 auth_token 网关）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 首连历史回填条数（0 关闭）
$env:WA_BACKFILL = "20"
# 媒体落地到 Python 静态目录（前端按 /static URL 加载）
$env:WA_MEDIA_DIR = "$root\src\web\static\protocol_media\whatsapp"
$env:WA_MEDIA_URL_BASE = "/static/protocol_media/whatsapp"
$env:LOG_LEVEL = "info"

Write-Host "[wa-baileys] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
node server.js
