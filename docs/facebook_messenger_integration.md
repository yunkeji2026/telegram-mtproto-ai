# Facebook / Messenger 接入策略与运维手册

> 编制日期：2026-04-20  
> 适用版本：Graph API v25.0（2026-Q1 稳定版）  
> 项目模块：`src/integrations/facebook_webhook.py` + `src/integrations/messenger_rpa/`

---

## 1. 双通道全景对比

项目里有 **两套** Facebook/Messenger 接入路径，互补而非互斥：

| 维度 | RPA 路径（messenger_rpa） | 官方 API 路径（facebook_webhook） |
|---|---|---|
| **目标账号** | 个人 FB 账号的 Messenger 1v1 | Facebook **Page**（公众主页）的 Inbox |
| **底层技术** | ADB + 截图 + Vision (glm-4v-flash) | Graph API v25.0 + Webhooks |
| **延迟** | 30-90s/轮（依赖 polling + vision 推理） | < 1s（事件推送） |
| **成本** | vision token 费 + 每台手机硬件 | 完全免费（Meta 官方） |
| **合规** | 灰色（个人号自动化违反 FB ToS） | **完全合规**（官方 API） |
| **稳定** | 受 UI 改版影响（Bloks 一年改 N 次） | 接口稳定，有 deprecation 期 |
| **限速** | 无明确限速但要做人类节奏 | API rate limit 200/h/user，Page 充足 |
| **多账号** | 一台手机=一个号；横向加机即可 | 一个 App 可绑多个 Page |
| **无人值守** | 必须手机在线 + 屏幕亮 | 服务端 webhook，零运维 |

**推荐部署**：
- 官方 Page 的客服 → **facebook_webhook**（Page Token + Webhook）
- 个人号 1v1 → **messenger_rpa**（ADB + Vision）

---

## 2. Facebook Page Webhook（推荐主线）

### 2.1 申请流程（一次性）

#### 步骤 A：在 Meta for Developers 创建 App
1. 打开 https://developers.facebook.com/apps/
2. **创建 App** → Use case 选 **"Other"** → App type 选 **"Business"**
3. 在 App 控制台添加 **Messenger** 产品

#### 步骤 B：连接 Page 并拿 Token
1. Messenger → Settings → **Access Tokens** → 选你管理的 Page
2. 点击 **"Generate Token"** 拿到 `Page Access Token`（首次开发用临时 token，长期需走 Token Debugger 升级到永久）
3. 记下 **Page ID**（在 Page 设置 / About / Page ID）

#### 步骤 C：配置 Webhook 回调
1. Messenger → Settings → **Webhooks** → **Add Callback URL**
2. 填：
   - **Callback URL**：`https://your-domain.com/fb/webhook`（必须 HTTPS、公网可达）
   - **Verify Token**：你自己定的 token（写到 config.yaml 的 `verify_token`）
3. 订阅以下事件：
   - `messages` ✅（最关键，用户给 Page 发消息）
   - `messaging_postbacks` ✅（按钮回调）
   - `messaging_optins` ✅（用户授权 opt-in）
   - 不要订阅 `message_echoes`（不需要回声，且代码已防御）

#### 步骤 D：申请权限走审核
开发模式下 Page admin 自己测可以；上线必须申请：
- **`pages_messaging`** ：必需
- `pages_messaging_subscriptions`：发周期性消息（可选，与 message tag 二选一）

填申请时要交：
- App 用途说明（中英）
- 录屏 demo（5-10 分钟，演示用户发消息→收到回复）
- 数据用法 + 隐私政策 URL

**审核周期**：通常 1-3 周。被拒重交无次数限制。

#### 步骤 E：本项目侧配置
编辑 `config/config.yaml`：

```yaml
facebook_messenger:
  enabled: true
  page_id: "1234567890"               # 你的 Page numeric ID
  page_access_token: "EAAxxx..."      # 步骤 B 拿到的（永久 token 更稳）
  app_secret: "abc123def456"          # App 控制台 → Settings → Basic → App Secret
  verify_token: "your-custom-token"   # 与步骤 C 填的一致
  webhook_path: /fb/webhook
  fallback_message_tag: ACCOUNT_UPDATE
  unsupported_type_reply: 目前仅支持文字消息。
```

启动后访问 `https://your-domain/fb/webhook?hub.mode=subscribe&hub.verify_token=your-custom-token&hub.challenge=test123`，
应返回 `test123` 字符串（200 OK）。

---

### 2.2 24 小时窗口规则（**最容易踩的坑**）

Meta 强制：**用户最近 24h 内主动给 Page 发过消息**，Page 才能用 `messaging_type=RESPONSE` 自由回复。

| 场景 | 24h 内 | 24h 外 |
|---|---|---|
| 普通文字回复 | ✅ RESPONSE | ❌（错误码 10:2534022） |
| 订单/账单确认 | ✅ RESPONSE | ✅ tag=POST_PURCHASE_UPDATE |
| 账户预警 | ✅ RESPONSE | ✅ tag=ACCOUNT_UPDATE |
| 活动确认 | ✅ RESPONSE | ✅ tag=CONFIRMED_EVENT_UPDATE |
| 24h 内人工干预 | ✅ RESPONSE | ✅ tag=HUMAN_AGENT（限7天） |
| 营销推广 | ❌ FB 直接拒（只能用付费短信） | ❌ |

**本项目自动处理**：`fb_send_with_window_fallback()` 先用 RESPONSE 发，遇 24h 错误自动降级 `MESSAGE_TAG=ACCOUNT_UPDATE` 重发（fallback_tag 可在 config 改）。

**重要**：滥用 message tag 会被 Meta 临时停 Page Messenger。tag 的内容必须**严格匹配** tag 的允许范围（不是营销话术）。

---

### 2.3 安全：必须做的 4 件事

1. **强制 X-Hub-Signature-256 校验**：本项目 `verify_fb_signature()` 已实现，未签名/签名错都直接 403。
2. **HTTPS only**：FB 拒绝 HTTP webhook。Cloudflare Tunnel / Nginx + Let's Encrypt 都行。
3. **响应 5 秒内 200**：FB 5 秒超时会重试，重试 N 次失败会暂停推送。本项目用 FastAPI 异步，将业务放在 await 里同步返回 200，已经满足。
4. **Page Access Token 永久化** + 旋转：从 60 天临时 token 升级到永久 token，需要在 Token Debugger 用 long-lived 的 user token 换。

---

### 2.4 Webhook 事件结构速查

```json
{
  "object": "page",
  "entry": [{
    "id": "<page_id>",
    "time": 1700000000,
    "messaging": [{
      "sender": {"id": "<PSID>"},        // PSID 是用户在该 Page 视野下的稳定 ID
      "recipient": {"id": "<page_id>"},
      "timestamp": 1700000000123,
      "message": {
        "mid": "m_abcd",
        "text": "hello",                 // 仅文字消息有
        "attachments": [...],            // 媒体/分享
        "is_echo": true                  // ★ Page 自己发出的会回声 -> 必须忽略
      },
      "delivery": {...},                 // 已送达回执 -> 忽略
      "read": {...},                     // 已读回执 -> 忽略
      "reaction": {...}                  // 反应 -> 视需求处理
    }]
  }]
}
```

本项目 `_extract_messaging_events()` + `_handle_one_event()` 已统一处理 echo / delivery / read 过滤。

---

## 3. RPA 路径（messenger_rpa）

### 3.1 适用边界

只用于：**个人 FB 账号** 的 Messenger 1v1（不是 Page、不是群聊）。

**违反 FB Terms of Service**，部署前要确认：
- 账号属于公司自有运营，**非真人朋友圈/客户朋友圈**
- 接受被 FB 风控的可能（轻则验证码、重则封号）
- **禁止用于陌生人主动外发**（这是封号红线）；只用于回复用户先发来的消息

### 3.2 当前能力（2026-04-20 已验证）

| 能力 | 状态 | 备注 |
|---|---|---|
| 设备 d113 上的 Inbox 扫描 | ✅ | vision 4 条未读全识别 |
| 消息 spam/friend 自动分级 | ✅ | vision quality_hint + 本地兜底关键词 |
| 选会话排序（friend > unknown > spam） | ✅ | 本地 score 函数 |
| 物理坐标精准点击（720×1600） | ✅ | CHAT_ROW_FIRST_Y=600, CHAT_ROW_HEIGHT=165 |
| 进入会话页 + Vision 抓对方最后一条文本 | ✅ | thread_vision_tag=zhipu_only |
| Bloks modal 守卫（白名单 4 类） | ✅ | 防止误闪避导致退出会话 |
| AdbKeyboard 输入文字 | ⚠️ | d160 上被 MIUI 拦截，d113 待测 |
| 真发消息 | 🚧 | reply_mode=off（PoC 安全档），待 P2 实战 |

### 3.3 何时切到 API 路径

**强烈建议尽快迁**：当用户/客户开始使用 Page Messenger（有 Page），就立刻去申请 Page Token 走官方通道。RPA 永远是 fallback，不是主线。

---

## 4. 部署架构推荐

```
              ┌─────────────────────┐
              │  Facebook 用户群   │
              └──┬───────────────┬──┘
                 │ Page DM       │ 1v1 个人号
                 ▼               ▼
         ┌──────────────┐ ┌──────────────┐
         │  Page 收件箱 │ │ 朋友圈 Inbox │
         │  （官方）    │ │  （Bloks UI）│
         └───────┬──────┘ └────┬─────────┘
                 │ webhook       │ ADB + Vision
                 ▼               ▼
       ┌────────────────────────────────────┐
       │   FastAPI (本项目 main.py)         │
       │ ┌─────────────────┐ ┌───────────┐  │
       │ │facebook_webhook │ │messenger_ │  │
       │ │ ↘             ↙ │ │   rpa     │  │
       │ └────────┬────────┘ └─────┬─────┘  │
       │          ▼                 ▼        │
       │     ┌───────────────────────┐       │
       │     │   SkillManager        │       │
       │     │ (人设 + 知识库 + AI)   │       │
       │     └───────────────────────┘       │
       └────────────────────────────────────┘
```

两个通道**共享 SkillManager**，所以人设/回复策略/知识库一份代码、一份数据，不会出现"Page 和个人号回复不一致"的问题。

---

## 5. 路线图（短中长期）

### 短期（本周内）
- [ ] 在 d113 验证 AdbKeyboard 是否已被 MIUI USB 安装拦截
- [ ] reply_mode=auto 在 d113 上跑一个安全会话（找个废账号互发）
- [ ] 申请 Meta App + 临时 Page Token，本地用 ngrok 跑 webhook 通真实事件

### 中期（2-4 周）
- [ ] 走 Meta App Review 拿 `pages_messaging` 永久权限
- [ ] 把 fb_webhook 加上 audit_store 持久化（全部 PSID/MID/消息历史入库）
- [ ] 给 fb_webhook 加 Web 管理页（与 line_rpa_routes 同构）
- [ ] RPA 端：把"未识别 modal"截图持久化归档供 prompt 迭代

### 长期（季度级）
- [ ] FB 主 App（katana）勘察：评论区自动回、PYMK 加好友
- [ ] Instagram Direct（与 Messenger 同 webhook 平台，复用代码）
- [ ] WhatsApp Business API（同样 Meta 体系）

---

## 6. 故障排查速查表

| 现象 | 可能原因 | 解决 |
|---|---|---|
| Webhook 注册失败 | verify_token 不一致 / URL 不通 | 检查 config 与 FB 后台一致；curl 测 GET 能拿到 challenge |
| 收不到 message 事件 | 没订阅 messages 或没把 Page 加入 App | App 控制台 → Messenger → Settings → Subscribed Pages |
| 回复发不出去 / 10:2534022 | 24h 窗口已关 | 自动降级 tag；或在 config 调 fallback_message_tag |
| 收到 200 但消息延迟很久 | 后端 SkillManager 慢 | webhook handler 必须 5 秒内 200，把重活做 background task |
| RPA 点错行 | 设备分辨率不是 720×1600 | 调 `cc.BASE_WIDTH/BASE_HEIGHT`；或重新校准 CHAT_ROW_FIRST_Y |
| RPA guard 误闪避 | confidence 太低被白名单豁免反而进入 modal | 把已知 modal 添加到 trusted_types 白名单 |
| RPA inbox 漏识别 unread | vision prompt 把 banner 当未读 | 在 INBOX_VISION_PROMPT 显式排除 banner / Stories 行 |

---

## 7. 关键文件索引

| 文件 | 作用 |
|---|---|
| `src/integrations/facebook_webhook.py` | Page API webhook 主模块（GET 校验 + POST 路由 + Send API） |
| `src/integrations/messenger_rpa/runner.py` | RPA 单次运行编排 |
| `src/integrations/messenger_rpa/inbox_scanner.py` | Vision 扫 Inbox + 质量评分 |
| `src/integrations/messenger_rpa/chat_reader.py` | Vision 抓对方最后一条 |
| `src/integrations/messenger_rpa/bloks_navigator.py` | Modal 守卫 + 白名单 |
| `src/integrations/messenger_rpa/coords.py` | 720×1600 物理坐标标定 |
| `src/web/admin.py`（5970 行附近） | 两个 webhook 注册入口 |
| `config/config.yaml`（messenger_rpa + facebook_messenger 段） | 配置 |
| `tests/test_facebook_webhook.py` | Webhook 7 项单元测试 |
