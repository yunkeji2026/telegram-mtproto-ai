# start.ps1 — 启动 Messenger 网页模式（隔离浏览器 + 官方 messenger.com）登录微服务
# 用法: pwsh -File start.ps1   (在 services/messenger-web 下)
# 说明: 用 Playwright 驱动持久化 Chromium 加载 messenger.com，功能对齐官方网页版；
#       登录默认 headed（弹窗内完成官方扫码/账密/2FA），成功后收发经 DOM 自动化。
#       主进程需开 config.platform_login.messenger.web_enabled=true 并指向 web_url。

$ErrorActionPreference = "Stop"
$root = "D:\workspace\telegram-mtproto-ai"

# 服务监听端口（须与主进程 platform_login.messenger.web_url 一致）
$env:PORT = "8791"
# 入站桥：Messenger 收到的消息 push 进统一收件箱（web 后台 18799）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 会话健康桥（P0-2）：登录/掉线/放弃自愈等状态转移主动 push（不配则由 PY_INGEST_URL 自动推导）
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"
# ingest endpoint 需 Bearer 鉴权（须与 config.yaml::web_admin.auth_token 一致），否则入站被 401 丢弃
$env:PY_API_TOKEN = "admin"
# 登录交互：0=headed（弹窗，运营在窗口内完成官方登录）；登录成功持久化后可切 1 后台常驻
$env:MSG_HEADLESS = "0"
# 开机自动恢复持久化 profile（headed 默认关 → 曾出现「主进程先起时 restore 落空、
# 账号一直不上线」的启动顺序依赖）。显式开：服务一起来就恢复会话，与主进程启动顺序解耦。
$env:MSG_RESTORE_ON_BOOT = "1"
# 入站轮询间隔（毫秒）；0 关闭入站同步
$env:MSG_POLL_MS = "4000"
# 首连回填最近会话末条数（0 关闭）
$env:MSG_BACKFILL = "20"
$env:MSG_SYNC = "1"
# 入站媒体落地目录（对齐 whatsapp-baileys）：进线程读到图片/视频等媒体气泡时，用浏览器会话
# 下载写入 Python 静态目录（同机共享），前端按 /static URL 加载。未配置则回落占位文本。
$env:MSG_MEDIA_DIR = "D:\workspace\telegram-mtproto-ai\src\web\static\protocol_media\messenger"
$env:MSG_MEDIA_URL_BASE = "/static/protocol_media/messenger"
$env:LOG_LEVEL = "info"

Write-Host "[messenger-web] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
$logDir = Join-Path $root "services\messenger-web\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("messenger-web-" + (Get-Date -Format "yyyyMMdd") + ".log")
node server.js *>> $log
