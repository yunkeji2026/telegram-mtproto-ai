# WhatsApp (Baileys) 协议多开登录微服务

为 `telegram-mtproto-ai` 主进程提供 WhatsApp **网页二维码登录 + 多账号协议连接**（M3）。
Python 侧桥接：`src/integrations/whatsapp_baileys_login.py`。

## 运行

```bash
cd services/whatsapp-baileys
npm install
PORT=8790 node server.js
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/login/start` | 发起登录，返回 `{login_id, qr_image}`（data URI 二维码） |
| GET  | `/login/:id/status` | 轮询：`{status, account_id, qr_image}`，status ∈ pending/scanned/authorized/expired/failed |
| POST | `/login/:id/cancel` | 取消 / 登出 |
| GET  | `/accounts` | 已连接账号列表 |
| POST | `/accounts/restore` | 恢复磁盘上所有已持久化 session（幂等，开机自动调用） |
| POST | `/accounts/:id/send` | 主动发送文本：body `{jid, text}`（jid 可裸号码），返回 `{ok, message_id}` |
| POST | `/accounts/:id/send-media` | 主动发送媒体：body `{jid, path, media_type, caption}`（path 为本机文件），返回 `{ok, message_id}` |
| GET  | `/health` | 健康检查 |

入站消息（M6①）：收到对方消息时，本服务会把消息 **push** 到 Python 统一收件箱
（`POST $PY_INGEST_URL`，带 `Authorization: Bearer $PY_API_TOKEN`）。未配置 `PY_INGEST_URL`
则不上报（仅本地连接保活）。群聊 `@g.us` 暂不接入。

环境变量：

| 变量 | 说明 |
|---|---|
| `PORT` | 监听端口（默认 8790） |
| `PY_INGEST_URL` | 统一收件箱入站桥，如 `http://127.0.0.1:8000/api/internal/protocol/ingest` |
| `PY_API_TOKEN` | 主项目 admin token（与 `Authorization: Bearer` 一致；主项目未设 token 可留空） |
| `WA_BACKFILL` | 首连历史回填条数（`messaging-history.set`，默认 20，设 0 关闭） |
| `WA_MEDIA_DIR` | 入站媒体落地目录（**单机共享**，指向主项目 `src/web/static/protocol_media/whatsapp` 的绝对路径）；未设则不下载媒体 |
| `WA_MEDIA_URL_BASE` | 媒体的浏览器 URL 前缀（默认 `/static/protocol_media/whatsapp`，与 WA_MEDIA_DIR 对应） |
| `WA_SESSIONS_DIR` | session 持久化目录（默认 `./sessions`） |

入站媒体（M6④）：收到图片/语音/视频/文件时，`downloadMediaMessage` 下载并写入 `WA_MEDIA_DIR`，
把 `media_type` + `media_ref`（`WA_MEDIA_URL_BASE/<file>`）一并 push 到收件箱，前端按 /static URL 直接显示。
群聊 `@g.us` 仍跳过。媒体目录与主项目同机时零额外配置即可被 `/static` 挂载服务。

首连历史回填（M6②）：连接成功后 Baileys 会触发 `messaging-history.set` 同步初始会话，
本服务取最近 `WA_BACKFILL` 条**有文本**的入站消息 push 到收件箱，使新接入账号即有上下文。

## 在主项目启用

`config/config.yaml`：

```yaml
platform_login:
  enabled: true
  whatsapp:
    protocol_enabled: true            # 默认 false，联调通过后再开
    baileys_url: "http://127.0.0.1:8790"
```

开启后，统一收件箱「账号管理 → ＋ 新增 WhatsApp → 协议多开」即走本服务的网页二维码。

## 风险提示

Baileys 为社区逆向库，多开存在 **WhatsApp 封号 / ToS 风险**。生产务必配套：
一号一代理 IP、一号一指纹环境、养号节奏与行为模拟。每个账号的鉴权态持久化于
`sessions/<login_id>/`（已 gitignore）。
