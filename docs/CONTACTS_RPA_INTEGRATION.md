# RPA 与 Contacts 模块集成指南

**状态**：Gateway/Hooks 层已完备并测试覆盖；runner 真实接入**推迟到有真机阶段**。
本文档定义"接入 runner 时在哪些点调什么方法"，作为下一阶段开工的蓝图。

---

## 一、整体接入形态

所有 RPA runner **通过 `ContactHooks` Protocol 调用合约层**，不直接持有 store / service。

```
 ┌──────────────────────┐       ┌─────────────────────┐
 │  Messenger Runner    │──┬──▶│  ContactHooks       │
 └──────────────────────┘  │    │  (Protocol)          │
                           │    └────────┬────────────┘
 ┌──────────────────────┐  │             │ 实现：
 │  LINE Runner         │──┘             ▼
 └──────────────────────┘       ┌─────────────────────┐
                                │ GatewayContactHooks │
                                └────────┬────────────┘
                                         ▼
                                ┌─────────────────────┐
                                │  ContactGateway     │
                                ├─────────────────────┤
                                │  Store / Handoff /  │
                                │     Merge           │
                                └─────────────────────┘
```

Runner 里只需要：
```python
self._contact_hooks: ContactHooks = NoopContactHooks()   # 默认无动作

def set_contact_hooks(self, hooks: ContactHooks) -> None:
    self._contact_hooks = hooks
```

Service/main.py 启动时注入 `GatewayContactHooks`。

---

## 二、Messenger RPA 接入点（5 处）

### 1. Runner 首次读到一个 peer（新/老都要调，幂等）

**位置**：`src/integrations/messenger_rpa/runner.py` 解析完 inbox 行、即将处理某个 peer 时。

```python
ctx = self._contact_hooks.on_peer_seen(
    channel="messenger",
    account_id=self._account_id,              # 当前 Messenger 账号
    external_id=peer_fb_id,                    # 对方 FB id（稳定主键）
    display_name=peer_display_name or "",
    trace_id=trace_id,
)
```

**注意**：peer_fb_id 必须是**稳定**的（不要用昵称）。如果只能拿到昵称，用 `f"name:{hash}"` 做代理，但未来会导致合并错乱——优先想办法拿真实 fb_id。

### 2. 读到对方消息（每条）

**位置**：拿到 peer_text 后（Vision 返回或 OCR 返回），调 AI 生成回复之前。

```python
self._contact_hooks.on_message(
    channel="messenger",
    account_id=self._account_id,
    external_id=peer_fb_id,
    direction="in",
    text_preview=peer_text,                    # gateway 内部截 120 字
    display_name=peer_display_name,
    trace_id=trace_id,
)
```

此调用**自动把 Journey 从 INITIAL 推到 ENGAGED**（首条触发）。

### 3. 发出回复（每条）

**位置**：reply 实际 tap 发送成功后（进 `send_ok` 分支）。

```python
self._contact_hooks.on_message(
    channel="messenger", account_id=..., external_id=peer_fb_id,
    direction="out", text_preview=reply_text, trace_id=trace_id,
)
```

### 4. 决定引流：签发 token

**位置**：业务判断"该引导他去 LINE 了"的地方。**当前 W2 还没有自动判定逻辑**，W3 才接入 `HandoffReadinessScorer`。MVP 时机：可以**由人工在 Web 后台勾选 journey**手动触发。

```python
token = self._contact_hooks.issue_handoff_for_messenger(
    account_id=..., external_id=peer_fb_id, trace_id=trace_id,
)
if token:
    line_id = config["line_rpa"]["our_line_id"]
    handoff_text = render_handoff_script(line_id=line_id, token=token, persona=...)
    # 走正常的 send 流程发 handoff_text
```

### 5. handoff 话术发送成功后

**位置**：`send_ok` 紧接着的分支，仅当本次 send 是引流话术时。

```python
self._contact_hooks.on_handoff_sent(
    account_id=..., external_id=peer_fb_id, token=token, trace_id=trace_id,
)
```

这会把 Journey 推到 `HANDOFF_SENT`。

---

## 三、LINE RPA 接入点（4 处）

### 1. friend_request_scanner 扫出候选（半自动期）

**位置**：新建 `line_rpa/friend_request_loop.py`（不放 runner.py 里，单独的扫描循环）。

```python
from src.integrations.line_rpa.friend_request_scanner import scan_friend_requests

screenshot = adb.take_screenshot(serial)
requests = await scan_friend_requests(screenshot, vision_client.describe_image)

for req in requests:
    # 当前 W2：存表（W3 新建 line_friend_requests 表）
    #        + 运营 Web 后台审批 approved 后 runner 才 tap 通过
    # MVP 简化：所有 req 都入库待审，runner 不自动 tap
    store.enqueue_friend_request(account_id=..., req=req, screenshot_path=screenshot)
```

### 2. Runner 通过好友后（不管是自动还是手动）

**位置**：tap "接受" 成功后、Journey 还不存在这个 peer 时。

```python
self._contact_hooks.on_peer_seen(
    channel="line",
    account_id=self._account_id,
    external_id=line_chat_key,                 # LINE 那条聊天的稳定 key
    display_name=peer_display_name or "",
    trace_id=trace_id,
)
```

### 3. 对方首条文本到达（关键合并点）

**位置**：LINE chat_list_scanner 找到未读行 → 进入聊天 → 读到对方消息。
如果该 chat_key 之前没见过（新好友的首条），调 `on_line_first_text`；否则只调 `on_message(direction='in')`。

判断"是不是首条"：
```python
prior = store.get_ci_by_external("line", account_id, line_chat_key)
is_first = (prior is None) or _no_prior_msg_in(prior)   # 或查 journey_events 有没有 msg_in
```

首条分支：
```python
outcome = self._contact_hooks.on_line_first_text(
    account_id=..., external_id=line_chat_key,
    text=peer_text,
    display_name=peer_display_name,
    language_hint=detected_language,
    timezone_hint=detected_tz,
    trace_id=trace_id,
)
# 合并成功 → outcome.contact_id 是最终 Contact
# 进入 manual_review → outcome.review_id 运营去后台决定
# keep_isolated → LINE 侧独立 Contact
```

非首条：走正常 `on_message(direction='in')`。

### 4. 发出回复

同 Messenger 的第 3 点，`on_message(channel='line', direction='out')`。

---

## 四、main.py wire-up（W3 动工）

```python
from src.contacts import (
    ContactStore, HandoffTokenService, MergeService,
    ContactGateway, GatewayContactHooks,
)

# 初始化（单例）
contacts_db = Path(config_dir) / "contacts.db"
contacts_store = ContactStore(contacts_db)
handoff_svc = HandoffTokenService(contacts_store, ttl_seconds=72 * 3600)
merge_svc = MergeService(contacts_store)
gateway = ContactGateway(contacts_store, handoff_svc, merge_svc)
hooks = GatewayContactHooks(gateway)

# 注入
line_rpa_service.runner.set_contact_hooks(hooks)
messenger_rpa_service.runner.set_contact_hooks(hooks)

# Web 路由
register_contacts_routes(
    app, api_auth=_api_auth,
    contacts_store=contacts_store, merge_service=merge_svc,
    audit_store=audit_store,
)
```

---

## 五、各接入点幂等性 & 失败策略

| 接入点 | 幂等？ | 失败时 runner 该怎么办 |
|---|---|---|
| `on_peer_seen` | ✅ 完全幂等（unique key 保护） | 吞掉，继续正常流程 |
| `on_message` | ✅ 同上 + 事件追加是累积的 | 同上 |
| `issue_handoff_for_messenger` | ✅ 每次返回**不同** token（这是正确行为，上次没用的自动过期） | 返回 None 时不要发引流，走常规回复 |
| `on_handoff_sent` | ⚠️ 多次调用会多次落事件（但 stage 迁移幂等） | 吞掉 |
| `on_line_first_text` | ❌ 应该只调一次（首条）。重复调会重复触发 merge/review | runner 必须判"首条"（见上节 §3） |

---

## 六、跨平台 trace_id 约定

Messenger 每条消息处理开始时生成 `trace_id = secrets.token_hex(8)`，整条处理链（ensure_ci / on_message / issue_handoff / on_handoff_sent / AI / send）都带上它。
LINE 端同样方式，但合并发生时 Gateway 会在 journey_events 里留下**双 trace**（Messenger 侧的原 trace + LINE 侧的新 trace），Contact Timeline 页面（W3 做）按 Contact 聚合后可看完整链。

---

## 七、下一步（W3）开工前检查清单

- [ ] main.py 已 wire Gateway 单例
- [ ] line_rpa/runner.py 加 `set_contact_hooks` + 5 处 hook 调用
- [ ] messenger_rpa/runner.py 加 `set_contact_hooks` + 5 处 hook 调用
- [ ] 配置一个 feature flag `contacts.enabled`（默认 false），false 时注入 `NoopContactHooks`
- [ ] 加一张 `line_friend_requests` 表 + 审核 UI
- [ ] admin.py 挂载 `register_contacts_routes`
- [ ] 上线前跑一次"1 真机灰度 1 周 + merge 率 ≥80% + 零误合并"

接入完成后的回归测试必须覆盖：
1. Messenger/LINE RPA 关闭 contacts feature flag 时，行为与当前完全一致
2. 打开后，单 Contact 端到端（Messenger → token → LINE → merge）成功率 ≥ 85%
