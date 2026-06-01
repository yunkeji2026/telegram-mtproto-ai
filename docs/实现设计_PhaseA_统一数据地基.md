# 实现设计 — Phase A：统一数据地基（可执行版）

更新日期：2026-05-31
对应蓝图：[`AI跨境电商客服平台_升级开发文档_v2_落地优化版.md`](AI跨境电商客服平台_升级开发文档_v2_落地优化版.md) §3 Phase A
状态：**可直接落地的实现设计**，所有签名/DDL/接入点对照真实代码

---

## 0. 为什么先做这一步

当前统一收件箱 `src/web/routes/unified_inbox_routes.py` 是**实时聚合、无持久层**：

- `_collect_all_chats()` 每次请求都现读各平台 state store 再用 `_normalize_chat()`/`_message_obj()` 临时拼装（见现有 254–370 行）。
- automation_mode 存在**进程内 dict** `app.state.unified_inbox_automation`，**重启即丢**（`unified_inbox_routes.py:71-76`；`docs/AI多语种聊天陪护平台_落地实施开发文档.md` 已标注此为「不能进生产」）。
- 没有 `conversations` / `messages` 事实源 → 跨平台历史、SLA 计时、漏斗、草稿统一都缺地基。

Phase A 交付三件事，且**不破坏现有 RPA 主线**（增量旁路写入，读路径先并存后切换）：

1. **InboxStore**：`conversations` / `messages` / `message_analysis` + 持久化 automation_mode。
2. **ChannelAdapter 协议**：把 4 平台收发统一成一个接口，消灭 unified_inbox 里的平台特判。
3. **Message Normalizer**：把内联 `_message_obj`/`_normalize_chat` 提为共享 `normalize()`。

---

## 1. 文件清单

### 新建

```
src/inbox/
  __init__.py
  models.py          # InboxMessage / InboxConversation / MessageAnalysis dataclass
  store.py           # InboxStore（SQLite，复刻 ContactStore 范式）
  normalizer.py      # normalize_message() / normalize_conversation()
  adapters/
    __init__.py
    base.py          # ChannelAdapter Protocol + 注册表
    line.py          # LineRpaAdapter
    whatsapp.py      # WhatsAppRpaAdapter
    messenger.py     # MessengerRpaAdapter
    telegram.py      # TelegramAdapter
tests/
  test_inbox_store.py
  test_inbox_normalizer.py
  test_channel_adapters.py
  test_unified_inbox_stage2.py   # 接 store 后的端到端（已有 stage1）
```

### 修改

| 文件 | 改动 |
|---|---|
| `src/integrations/rpa_base/protocols.py` | 追加 `ChannelAdapter` Protocol（与现有 `RpaService` 并存，不动旧的） |
| `src/web/routes/unified_inbox_routes.py` | `_collect_all_chats` 改为遍历 adapters；automation_mode 读写改走 InboxStore；保留旧 helper 作 fallback |
| `main.py`（~555–620 区块） | 构造 `InboxStore` + 注册 adapters 到 `web_app.state.inbox_store` / `web_app.state.channel_adapters` |
| `config/config.example.yaml` | 新增 `inbox:` 段（feature flag + db 路径），默认 `enabled: true`（纯旁路，安全） |

---

## 2. 数据模型（DDL）

复刻 `src/contacts/store.py` 的范式：单 connection + `threading.Lock` + WAL + `executescript(_DDL)` + `PRAGMA table_info` 幂等迁移。

```sql
-- conversations：跨平台会话事实源
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id   TEXT PRIMARY KEY,          -- 'line:acct1:U123'（沿用现有 _conv_id 格式）
    platform          TEXT NOT NULL,             -- line / whatsapp / messenger / telegram
    account_id        TEXT NOT NULL DEFAULT 'default',
    chat_key          TEXT NOT NULL,
    contact_id        TEXT NOT NULL DEFAULT '',   -- 外联 contacts.contact_id（可空，后填）
    display_name      TEXT NOT NULL DEFAULT '',
    language          TEXT NOT NULL DEFAULT 'unknown',
    last_text         TEXT NOT NULL DEFAULT '',
    last_ts           REAL NOT NULL DEFAULT 0,
    unread            INTEGER NOT NULL DEFAULT 0,
    risk_level        TEXT NOT NULL DEFAULT 'unknown',
    automation_mode   TEXT NOT NULL DEFAULT 'review',  -- manual/review/multi_choice/auto_ai
    status            TEXT NOT NULL DEFAULT 'open',     -- open/snoozed/closed
    assignee          TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_updated  ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_platform ON conversations(platform, account_id);
CREATE INDEX IF NOT EXISTS idx_conv_contact  ON conversations(contact_id);

-- messages：统一消息表（原文/译文/方向/媒体/平台 message id）
CREATE TABLE IF NOT EXISTS messages (
    message_id        TEXT PRIMARY KEY,           -- 'conv:platform_msgid'（缺失时用 hash 兜底）
    conversation_id   TEXT NOT NULL,
    platform_msg_id   TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL DEFAULT 'in', -- in / out
    text              TEXT NOT NULL DEFAULT '',
    original_text     TEXT NOT NULL DEFAULT '',
    translated_text   TEXT NOT NULL DEFAULT '',
    source_lang       TEXT NOT NULL DEFAULT 'unknown',
    target_lang       TEXT NOT NULL DEFAULT '',
    media_type        TEXT NOT NULL DEFAULT '',   -- ''/image/voice/file
    media_ref         TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL DEFAULT 0,
    ingested_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON messages(conversation_id, ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_msg_conv_platmsg
    ON messages(conversation_id, platform_msg_id);  -- 幂等去重，平台 id 重复不重复入

-- message_analysis：意图/情绪/风险（Phase C 的 LLM 升级写这里；A 先建表）
CREATE TABLE IF NOT EXISTS message_analysis (
    analysis_id       TEXT PRIMARY KEY,
    message_id        TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    intent            TEXT NOT NULL DEFAULT '',
    emotion           TEXT NOT NULL DEFAULT '',
    risk_level        TEXT NOT NULL DEFAULT 'low',
    risk_reasons_json TEXT NOT NULL DEFAULT '[]',
    relationship_stage TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    order_no          TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL DEFAULT 0,
    analyzer          TEXT NOT NULL DEFAULT 'rule',  -- rule / llm
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ana_msg  ON message_analysis(message_id);
CREATE INDEX IF NOT EXISTS idx_ana_conv ON message_analysis(conversation_id, ts DESC);
```

> 字段刻意对齐 `ChatAnalysis.to_dict()`（`src/ai/chat_assistant_service.py`）与 `TranslationResult.to_dict()`（`src/ai/translation_service.py`）的现有 shape，落库零转换成本。

DB 路径：`data/inbox.db`（与 contacts 的 `data/*.db` 同目录约定；路径由 `config.inbox.db_path` 决定，默认相对项目根解析——注意旧 `开发升级与优化建议.md` §3.1 提的 cwd 依赖坑，**用绝对路径解析**）。

---

## 3. InboxStore 接口（store.py 骨架）

```python
class InboxStore:
    def __init__(self, db_path: Path) -> None: ...
    def close(self) -> None: ...

    # ── 写入（adapter ingest 调用，幂等）──
    def upsert_conversation(self, conv: InboxConversation) -> None: ...
    def ingest_message(self, msg: InboxMessage) -> bool:
        """INSERT OR IGNORE（靠 uq_msg_conv_platmsg 去重）。返回是否新插入。"""
    def ingest_batch(self, conv: InboxConversation, msgs: list[InboxMessage]) -> int:
        """一个事务内 upsert 会话 + 批量 ingest 消息；返回新插入条数。"""

    # ── 读取（unified_inbox 路由调用）──
    def list_conversations(self, *, limit=50, platform="", status="open") -> list[dict]: ...
    def get_conversation(self, conversation_id: str) -> Optional[dict]: ...
    def list_messages(self, conversation_id: str, *, limit=50) -> list[dict]: ...

    # ── automation_mode 持久化（替换进程内 dict）──
    def get_automation_mode(self, conversation_id: str) -> str:  # 默认 'review'
        ...
    def set_automation_mode(self, conversation_id: str, mode: str) -> None: ...

    # ── 分析落库（Phase C 用）──
    def save_analysis(self, analysis: MessageAnalysis) -> None: ...
    def latest_analysis(self, conversation_id: str) -> Optional[dict]: ...
```

`__init__` 直接照抄 `ContactStore.__init__`（180–200 行）：`sqlite3.connect(check_same_thread=False)` + `row_factory=Row` + `threading.Lock` + 4 条 PRAGMA + `executescript(_DDL)` + 幂等迁移块。

---

## 4. ChannelAdapter 协议（接入点最关键）

在 `src/integrations/rpa_base/protocols.py` **追加**（不动现有 `RpaService`）：

```python
@runtime_checkable
class ChannelAdapter(Protocol):
    """统一渠道收发契约。RPA service / 官方 webhook / Telegram client 各自实现。

    与 RpaService 的区别：RpaService 是「控制面」(status/pause/resume)，
    ChannelAdapter 是「数据面」(拉对话 / 归一 / 发送)。一个 service 可同时满足两者。
    """
    platform: str          # 'line' / 'whatsapp' / 'messenger' / 'telegram'
    account_id: str
    account_label: str

    def fetch_recent(self, limit: int = 20) -> list[dict]:
        """返回该账号最近对话的 raw dict 列表（平台原始结构）。"""
        ...

    def normalize(self, raw: dict) -> tuple[InboxConversation, list[InboxMessage]]:
        """把一条 raw 对话归一成统一会话 + 消息。默认实现走 normalizer.py。"""
        ...

    async def send(self, *, chat_key: str, text: str) -> dict:
        """主动发送。不支持时抛 NotImplementedError（路由转 501）。"""
        ...
```

每个 adapter（如 `adapters/line.py::LineRpaAdapter`）是对现有 service 的**薄包装**——把 `unified_inbox_routes.py` 现在内联在 `_collect_all_chats` 里的「LINE 用 `list_chats`、WA 用 `list_pending`、Messenger 用 `list_approvals`、Telegram 用 `_recent_messages`」逻辑各搬一份进去：

```python
class LineRpaAdapter:
    platform = "line"
    def __init__(self, svc):
        self._svc = svc
        self.account_id = getattr(svc, "account_id", "default")
        self.account_label = (getattr(svc, "_merged_cfg", {}) or {}).get("label") or self.account_id
    def fetch_recent(self, limit=20):
        try: return self._svc.list_chats(limit) or []
        except Exception: return []
    def normalize(self, raw):
        return normalize_conversation(platform="line", account_id=self.account_id,
                                      account_label=self.account_label, raw=raw,
                                      field_map=LINE_FIELDS)
    async def send(self, *, chat_key, text):
        send = getattr(self._svc, "send_to_chat", None)
        if not send: raise NotImplementedError("LINE 需启用 approve 模式")
        return await send(chat_key=chat_key, text=text)
```

注册表（`adapters/base.py`）：`build_adapters(app_state) -> list[ChannelAdapter]`，从 `app.state.line_rpa_services` / `whatsapp_rpa_services` / `messenger_rpa_service` / `telegram_client` 装配。**新增渠道 = 加一个 adapter 文件 + 注册表里加一行**，路由零改动。

---

## 5. unified_inbox_routes.py 重构（增量、非破坏）

分三步，每步可独立合并、独立回归：

**步骤 1（旁路写入）**：`_collect_all_chats` 末尾把聚合结果 `ingest_batch` 进 InboxStore（best-effort try/except，失败只 log）。此时读路径不变 → **零行为变化**，但 DB 开始积累事实源。

**步骤 2（automation 切换）**：`_get` / `_set automation`（534–559 行）改走 `inbox_store.get/set_automation_mode`；`app.state.unified_inbox_automation` 进程内 dict 作为 store 不可用时的 fallback。→ 修掉「重启即丢」生产阻断点。

**步骤 3（读路径切换）**：`/api/unified-inbox/chats` 与 `/thread` 优先 `inbox_store.list_conversations/list_messages`；store 为空（冷启动）时回落到现有实时聚合。`_collect_all_chats` 改为遍历 `app.state.channel_adapters` 的 `fetch_recent + normalize`，删除 `_get_line_services` 等 4 个平台特判函数。

> 关键：每步都保留 fallback，任何一步出问题都退回旧实时聚合，不影响线上收件箱可用性。

---

## 6. main.py 接入点

在 `~555–620` 构造 `web_app` 之后、与 `line_rpa_service` 等注入并列处加：

```python
# ── 统一收件箱持久层 + 渠道适配器 ──
if self.config.get("inbox", {}).get("enabled", True):
    from src.inbox.store import InboxStore
    from src.inbox.adapters.base import build_adapters
    from pathlib import Path
    db_path = Path(self.config.get("inbox", {}).get("db_path", "data/inbox.db"))
    if not db_path.is_absolute():
        db_path = self._project_root / db_path   # 复用现有根目录解析，避免 cwd 坑
    self.inbox_store = InboxStore(db_path)
    web_app.state.inbox_store = self.inbox_store
    web_app.state.channel_adapters = build_adapters(web_app.state)
    self.logger.info("统一收件箱持久层已挂载（%s）", db_path)
```

`build_adapters` 在 service 注入（560–567 行）之后调用，确保能读到 `web_app.state.*_services`。关闭时 `self.inbox_store.close()`（仿 contacts 的 shutdown 钩子）。

---

## 7. 配置

`config/config.example.yaml` 追加：

```yaml
inbox:
  enabled: true            # 纯旁路持久化，安全默认开；不影响 RPA 主线
  db_path: "data/inbox.db"
  retention_days: 90       # 后续清理任务用
  max_messages_per_conv: 500
```

注意：虽然 §6 默认 `enabled: true`，但它是**只读旁路 + 可回落**，符合「新子系统 feature flag」精神且无主线风险；若运维要更保守可改 `false`，路由自动全程走旧实时聚合。

---

## 8. 测试计划（对齐 tests/ 命名习惯）

已有 `tests/test_unified_inbox_stage1.py`（用 `app.state.line_rpa_services = [LineSvc()]` 注入 stub），新测试沿用同款 stub 注入：

| 文件 | 覆盖 |
|---|---|
| `test_inbox_store.py` | DDL 建表、`ingest_message` 幂等去重（同 platform_msg_id 不重复入）、automation_mode 持久化、冷重启后读回、迁移幂等 |
| `test_inbox_normalizer.py` | LINE/WA/Messenger/Telegram 四套 raw → 统一 Message 字段映射正确；语言检测沿用 `detect_language` |
| `test_channel_adapters.py` | `runtime_checkable` 校验 4 个 adapter 满足 `ChannelAdapter`；`send` 不支持时抛 `NotImplementedError`；service 异常时 `fetch_recent` 返回 `[]` 不崩 |
| `test_unified_inbox_stage2.py` | 旁路写入后 DB 有数据；automation 重启不丢；读路径切到 store；store 空时回落实时聚合 |

回归命令（`CLAUDE.md` 全量）：
```bash
python -m pytest tests/ -n auto -q
```
预期：全绿 + 新增约 4 个测试文件，无回归。

---

## 9. 验收标准（Phase A 完成定义）

- [ ] 4 平台消息落入同一 `messages` 表，可按 `conversation_id` 跨平台查历史。
- [ ] automation_mode 写 `conversations` 表，**重启后保留**（修掉已知生产阻断点）。
- [ ] 新增一个「假渠道」adapter（实现 `ChannelAdapter`）即可进收件箱，**不改 unified_inbox 核心**。
- [ ] `_get_line_services` 等 4 个平台特判函数从路由中移除（逻辑下沉到 adapter）。
- [ ] store 故障/为空时自动回落旧实时聚合，收件箱始终可用。
- [ ] 全量回归全绿。

---

## 10. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 旁路写入拖慢 `/chats` 响应 | `ingest_batch` 包 try/except + 后续可挪到后台线程；失败只 log 不影响响应 |
| 平台 message_id 不稳定导致重复入库 | `uq_msg_conv_platmsg` 唯一索引 + 缺 id 时用 `sha256(text+ts)` 兜底 |
| 与 contacts.contact_id 关联不上 | `contact_id` 允许空，后续 Phase 经 `merge.py` 回填，不阻塞 A |
| DB 路径 cwd 依赖（历史坑） | §6 强制绝对路径解析 |
| 改动影响线上收件箱 | 三步增量 + 每步 fallback，任意步骤可单独回退到旧实时聚合 |

---

## 11. 与后续 Phase 的接口预留

- **Phase B（统一草稿）**：新表 `reply_drafts.conversation_id` 外联本表 `conversations`；草稿审批结果回写 `messages`(direction=out)。
- **Phase C（LLM 意图 + 翻译产品化）**：`ChatAssistantService.analyze()` 结果落 `message_analysis`（表已在 A 建）；`translation_memory` 表替换 `TranslationService` 的进程内 TTL 缓存，`messages.translated_text` 复用其结果。
- **Phase D（电商工具）**：`message_analysis.order_no` 字段已预留，供工具层按订单号触发查询。

---

*本设计与 2026-05-31 代码版本对应。实现前请 `grep` 复核 `unified_inbox_routes.py` 与 `main.py` 行号（代码增长会漂移），以代码实况为准。*
