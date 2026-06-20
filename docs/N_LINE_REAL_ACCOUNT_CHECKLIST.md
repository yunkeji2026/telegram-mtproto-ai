# N 线真号联调清单（扫码个人号全自动陪聊 + 坐席可见）

> 纯代码侧 N1–N4（含 N4b）骨架 + 优化1/3 已全绿（见 `docs/M_LINE_COMMERCIALIZATION.md` 执行日志）。
> 再往前**必须用真 Telegram 号联调**。本清单列出准备物 + 分步验证 + 每步预期与排错点。

## 0. 你需要准备的东西

| 项 | 说明 | 从哪拿 |
|---|---|---|
| `api_id` / `api_hash` | Telegram 应用凭证（一套即可，多号共用） | https://my.telegram.org → API development tools |
| 测试号 ×1–2 | 建议 1 个**新号** + 1 个**老号**：新号验预热爬坡，老号验稳定收发 | 自备 |
| 代理（可选但强烈建议） | 每号独立 socks5/http 代理，反封号命门 | 自备；填进 `proxy_pool` |
| 2FA 密码（若开了两步验证） | 手机号登录时需要 | 环境变量 `TG_2FA_PASSWORD` / `config.telegram.two_fa_password` |

## 1. 开启开关（config.yaml）

```yaml
telegram:
  api_id: 123456            # 真实值
  api_hash: "xxxxxxxx"      # 真实值

platform_login:
  orchestrator_enabled: true       # 编排器跑起来（否则协议号不会被拉起）
  telegram:
    protocol_enabled: true         # 扫码登录 provider 注册
    companion_runtime: true        # 协议号跑 A 线"有灵魂"client（N4）
    backfill_dialogs: 20

companion_send_gate:               # 可选：先关，跑通收发后再开验证反封号
  enabled: false
```

> 启动时会跑配置自检（`config_check`）。若 `companion_runtime` 开了但漏开
> `protocol_enabled` / `orchestrator_enabled`，会有 WARN 点名——按提示补齐。

## 2. 分步验证

### Step 1 · 扫码登录 → session 落盘 + 注册表登记
- 操作：统一收件箱「账号管理 → ＋ 扫码新增」→ 手机 Telegram「设置 → 设备 → 关联桌面设备」扫码。
- 预期：
  - `sessions/tg_login_*.session` 文件生成；
  - `account_registry` 出现一条 `mode=protocol, meta.session_name=tg_login_*, phone=...`；
  - `GET /api/accounts` 能看到该号（sources 含 registry）。
- 排错：DC 迁移失败/超时 → 多为代理不通或网络抖动；先确认代理可达再重扫。

### Step 2 · 编排器拉起 → A 线 client 用该 session 启动
- 预期：
  - 日志出现「Telegram 协议号将使用 A 线统一运行时（companion_runtime）」；
  - `GET /api/accounts/orchestrator` 中该号 `state=running`，worker `type=telegram_companion`；
  - **关键验证点（真号才能确认）**：扫码 session（经 DC 迁移）能否被 A 线
    `initialize()`+`start(block=False)` 直接拉起。若报未授权 → 见下方「优化候选」。
- 排错：`companion runtime 上下文未就绪` → 说明 app 启动没调 `set_companion_context`
  （正常启动会调；仅当 main 初始化顺序异常时出现）。

### Step 3 · 收发 + 收件箱镜像 + 坐席可见
- 用另一个号给测试号发消息。
- 预期：
  - A 线丰富管线生成回复并发出（有人设/记忆/情绪）；
  - **N4b 镜像**：统一收件箱出现该会话，用户原话(in) + AI 回复(out) 都在；坐席台可接管。
- **关键验证点**：镜像消息的 `chat_key` 归并是否与坐席台预期一致（A 线用 `chat.id`）。

### Step 4 · 反封号闸门 + 机群健康灯（开 `companion_send_gate.enabled: true` 后）
- 预期：
  - 新号当天日发到 `warmup_start_cap`(默认 2) 后被闸门拦截（日志 `[send_gate] ... warmup_cap`）；
  - `GET /api/accounts/fleet-health` + `rpa_overview` 看板「机群反封号健康」卡读数正确
    （活跃/预热中/受限封禁；`sends_today` 来自优化1 统一计数器）。
- 排错：闸门不拦 → 确认 `enabled:true`；读数为 0 → 确认 A 线发送走的是 `_send_reply`（已接计数器）。

## 3. 真号联调中可能触发的优化候选（待验证后定夺）

1. **session_string 优先**：若文件 session（DC 迁移后）拉起不稳，登录成功时顺手导出
   `session_string` 存进 `account_registry.meta`，A 线用 `session_string` in-memory 启动
   （代码已支持 `account_cfg.session_string`，只差登录侧导出）。
2. **N5 登录归一**：手机号+验证码登录（A 线 config 号）当前**不写** `account_registry`，
   故不出现在 fleet-health。验证 account_id 口径（A 线内部 id vs Telegram user id）后，
   把两条登录路径写进同一注册表（需加「外部托管」标记，避免编排器重复拉起 main.py 已管的号）。
3. **chat_key 归并**：若 Step 3 发现镜像会话与坐席台线程对不齐，统一 `chat_key` 规范化。

## 4. 回退（出问题随时关）
把 `platform_login.telegram.companion_runtime` 设回 `false` → 协议号回落 B 线薄连接 worker；
把 `orchestrator_enabled` 设回 `false` → 编排器不持有任何协议连接，主进程行为同改造前。
