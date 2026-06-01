# 实现设计 — Phase B：统一草稿/审批层 reply_drafts（可执行版）

更新日期：2026-05-31
对应蓝图：[`AI跨境电商客服平台_升级开发文档_v2_落地优化版.md`](AI跨境电商客服平台_升级开发文档_v2_落地优化版.md) §3 Phase B
依赖：[`实现设计_PhaseA_统一数据地基.md`](实现设计_PhaseA_统一数据地基.md)（复用 `InboxStore` + `conversations`）
状态：**可直接落地的实现设计**，所有表/方法对照真实代码

---

## 0. 问题与目标

草稿/审批现在**分散在 4 张表**，schema 与状态词汇各不相同，UI 要分平台特判：

| 来源 | 表 | 状态词汇 | service 方法（真实签名） |
|---|---|---|---|
| reunion 草稿（contacts 主线） | `draft_log`（`src/contacts/store.py:123`） | sent_ts/success 二元 | gateway/scheduler 内部写 |
| LINE 审核 | `line_rpa_pending`（`line_rpa/state_store.py:59`） | pending/approved/rejected/sent/cancelled/error | `list_pending(status,limit)` / `resolve_pending(id,action,text,by)` |
| WhatsApp 审核 | `wa_rpa_pending`（`whatsapp_rpa/state_store.py:48`） | pending/approved/rejected/... | `list_pending(...)` / `resolve_pending(...)` |
| Messenger 审批 | `messenger_rpa_approvals`（`messenger_rpa/state_store.py:102`） | pending/approved/rejected/sent/failed | `list_approvals(status,chat_key,limit)` / `resolve_approval(...)` |

**目标**：建统一 `reply_drafts` 层 + `DraftService`，让收件箱跨平台看草稿/批准/驳回/接管，并接入 L0–L4 风险分层。

**核心约束（不破坏 RPA 主线）**：每个平台的 pending/approval 表**仍是 runner 实际发送的事实源**（runner 轮询自己的表）。统一层做的是**索引 + 镜像 + 派发**——
- `reply_drafts` 通过 `(source_kind, source_id)` 反指来源记录；
- 统一 UI 的 resolve 动作**派发回 owning service** 的 `resolve_pending` / `resolve_approval`，runner 行为零改动；
- 镜像状态供跨平台视图 / SLA / 审计用。

---

## 1. 文件清单

### 新建

```
src/inbox/
  drafts.py            # DraftService（聚合 + 派发 + 镜像）
  draft_models.py      # UnifiedDraft dataclass + STATUS/RISK 枚举映射
src/web/routes/
  drafts_routes.py     # /api/drafts 统一端点
tests/
  test_draft_service.py
  test_drafts_routes.py
  test_draft_risk_gating.py
```

### 修改

| 文件 | 改动 |
|---|---|
| `src/inbox/store.py` | 追加 `reply_drafts` DDL + `upsert_draft` / `list_drafts` / `get_draft` / `mirror_status` 方法 |
| `src/web/routes/unified_inbox_routes.py` | `/api/unified-inbox/send` 走 DraftService（按 automation_mode 决定直发 vs 落草稿审核） |
| `main.py`（~620 区块） | 构造 `DraftService`（注入 3 个 RPA service + InboxStore + contacts.store）+ 注册 `drafts_routes` |
| `config/config.example.yaml` | `inbox.drafts` 子段 + `risk_policy`（L0–L4 阈值） |

---

## 2. 数据模型（reply_drafts DDL）

落在 `src/inbox/store.py`（与 Phase A 三表同库，复用同一 connection/lock）：

```sql
CREATE TABLE IF NOT EXISTS reply_drafts (
    draft_id          TEXT PRIMARY KEY,           -- 统一 id（uuid）
    conversation_id   TEXT NOT NULL DEFAULT '',    -- 外联 Phase A conversations
    platform          TEXT NOT NULL,
    account_id        TEXT NOT NULL DEFAULT 'default',
    chat_key          TEXT NOT NULL DEFAULT '',
    -- 来源回指（事实源仍在各平台表）
    source_kind       TEXT NOT NULL,               -- line_pending | wa_pending | messenger_approval | reunion | inbox
    source_id         TEXT NOT NULL DEFAULT '',     -- 各表主键（line/wa/messenger 是 INTEGER → 存字符串）
    -- 内容
    peer_text         TEXT NOT NULL DEFAULT '',
    draft_text        TEXT NOT NULL DEFAULT '',
    final_text        TEXT NOT NULL DEFAULT '',     -- 人工编辑后的实发文本
    draft_lang        TEXT NOT NULL DEFAULT '',
    translated_preview TEXT NOT NULL DEFAULT '',     -- 译回客户语言的预览（Phase C 填）
    -- 风险/策略
    risk_level        TEXT NOT NULL DEFAULT 'low',   -- low/medium/high（对齐 ChatAnalysis）
    risk_reasons_json TEXT NOT NULL DEFAULT '[]',
    autopilot_level   TEXT NOT NULL DEFAULT 'L1',    -- L0..L4
    -- 统一状态机
    status            TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|sent|failed|cancelled
    decided_by        TEXT NOT NULL DEFAULT '',
    decided_at        REAL NOT NULL DEFAULT 0,
    sent_at           REAL NOT NULL DEFAULT 0,
    error             TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON reply_drafts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_conv   ON reply_drafts(conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_source ON reply_drafts(source_kind, source_id);
```

> 字段是 4 张源表的**并集归一**：`peer_text`/`draft_text`/`status`/`decided_by` 各源都有等价列；`risk_*`/`autopilot_level` 是本层新增。`uq_drafts_source` 保证一条源记录只镜像一行。

### 统一状态映射（draft_models.py）

```python
# 各源状态词汇 → 统一 status
STATUS_MAP = {
    "line_pending":        {"pending":"pending","approved":"approved","rejected":"rejected",
                            "sent":"sent","cancelled":"cancelled","error":"failed"},
    "wa_pending":          {"pending":"pending","approved":"approved","rejected":"rejected",
                            "sent":"sent","error":"failed"},
    "messenger_approval":  {"pending":"pending","approved":"approved","rejected":"rejected",
                            "sent":"sent","failed":"failed"},
    "reunion":             {},  # draft_log 用 sent_ts/success 推导（见 §3）
}
```

---

## 3. DraftService（聚合 + 派发 + 镜像）

```python
class DraftService:
    def __init__(self, *, inbox_store, line_services=(), wa_services=(),
                 messenger_service=None, contacts_store=None): ...

    # ── 聚合（读）：统一视图 ──
    def list_drafts(self, *, status="pending", platform="", limit=50) -> list[dict]:
        """先 sync_mirror() 拉各源最新，再从 reply_drafts 统一返回。"""

    # ── 镜像同步：各源 → reply_drafts ──
    def sync_mirror(self) -> int:
        """遍历 line/wa list_pending + messenger list_approvals + draft_log，
        upsert 进 reply_drafts（靠 uq_drafts_source 幂等）。返回新增/更新条数。"""

    # ── 派发（写）：统一 resolve → owning service ──
    def resolve(self, draft_id: str, action: str, *, text=None, by="") -> dict:
        """action ∈ approve/reject/send/cancel/handoff。
        1) 查 reply_drafts 拿 source_kind/source_id
        2) 派发到对应 service.resolve_pending/resolve_approval（事实源）
        3) 回写 reply_drafts.status（mirror）
        runner 行为不变。reunion 源派发到 contacts gateway 的 mark-sent 路径。"""

    # ── 新建草稿（inbox 主动生成场景）──
    def create_draft(self, *, conversation_id, platform, account_id, chat_key,
                     peer_text, draft_text, analysis=None) -> dict:
        """风险分层决定落地方式（见 §4）。source_kind='inbox'。"""
```

`reunion` 源（`draft_log`）没有 pending 状态词汇，用现有列推导：`sent_ts IS NULL` → pending；`sent_ts` 有值 → sent；`success` 列供 SLA 不影响草稿状态。派发 approve/send 调 contacts 路由已有的 `/api/drafts/<id>/mark-sent`（见 `test_contacts_routes.py` 的 mark-sent 用例）。

---

## 4. 风险分层落地（L0–L4，复用 ChatAnalysis）

接 Phase A 的 `automation_mode`（conversations）+ Phase C 的 `ChatAnalysis.risk_level`：

| autopilot_level | 触发条件 | 行为 |
|---|---|---|
| L0 仅翻译 | automation_mode=`manual` | 不生成草稿，只译文 |
| L1 草稿待审 | 默认 / automation_mode=`review` | 落 reply_drafts(status=pending) |
| L2 低风险自动 | automation_mode=`auto_ai` 且 `risk_level=low` 且 intent ∈ 白名单（FAQ/物流已签收/欢迎语） | 直发 + 落 sent 记录 |
| L3 中风险审批 | `risk_level=medium`（生气/投诉） | **强制** pending，禁止自动发 |
| L4 高风险人工 | `risk_level=high`（money/privacy/self_harm/adult/stop_contact，见 `chat_assistant_service.py:127`） | pending + 标记 `handoff` + 告警 |

`create_draft()` 用 `ChatAnalysis.risk_level` + `risk_reasons` 计算 `autopilot_level`，**medium/high 一律落 pending 不自动发**（即使 automation_mode=auto_ai）。每个自动动作写 `audit_store`（复用现有 `audit_log` 表）+ `event_tracker`。

风险白名单/阈值放 `config.inbox.risk_policy`，可热更（沿用 `config_manager` mtime 范式）。

---

## 5. /api/drafts 统一端点（drafts_routes.py）

```
GET  /api/drafts                  ?status=pending&platform=&limit=50   → DraftService.list_drafts
GET  /api/drafts/{draft_id}                                            → get_draft
POST /api/drafts/{draft_id}/resolve   {action, text?, by?}            → DraftService.resolve
GET  /api/drafts/stats                                                 → 按 platform×status 计数
```

鉴权复用 admin 的 `api_auth` / `require_role`（参照 `main.py:582` contacts 路由的 `_contacts_api_auth`）。`unified_inbox.html` 的草稿区从分平台调用改为统一调 `/api/drafts`。

---

## 6. main.py 接入点

在 Phase A 的 `inbox_store` 构造之后、contacts 路由注册区块（~573–620）附近：

```python
if getattr(self, "inbox_store", None) is not None:
    from src.inbox.drafts import DraftService
    from src.web.routes.drafts_routes import register_drafts_routes
    draft_svc = DraftService(
        inbox_store=self.inbox_store,
        line_services=self.line_rpa_services or [],
        wa_services=self.whatsapp_rpa_services or [],
        messenger_service=self.messenger_rpa_service,
        contacts_store=(self.contacts.store if self.contacts else None),
    )
    web_app.state.draft_service = draft_svc
    register_drafts_routes(web_app, api_auth=_api_auth, draft_service=draft_svc)
    self.logger.info("统一草稿层已挂载（/api/drafts）")
```

---

## 7. 迁移策略（三步、非破坏）

**步骤 1（只读镜像）**：上线 `reply_drafts` + `sync_mirror()`，定时（或每次 list 前）把 3 表 + draft_log 拉进统一表。UI 新增统一草稿视图，旧的分平台审核页**保持不动**。→ 零风险，只多一份索引。

**步骤 2（统一 resolve 派发）**：`/api/drafts/.../resolve` 派发回 owning service。旧分平台 resolve 端点保留（双轨）。验证派发与直接 resolve 行为一致。

**步骤 3（新草稿走统一层）**：inbox 主动生成的草稿 `source_kind=inbox` 直接进 reply_drafts，并接 §4 风险分层。RPA runner 产生的草稿仍走各自表 + 镜像（不强行迁移，避免动 runner）。

> 任何一步出问题，旧分平台审核链路完整可用，回退只需关 `/api/drafts` 路由。

---

## 8. 测试计划

| 文件 | 覆盖 |
|---|---|
| `test_draft_service.py` | `sync_mirror` 幂等（4 源 → reply_drafts 不重复）；状态映射正确；reunion 源 sent_ts 推导；resolve 派发到正确 service（用 stub 验证 `resolve_pending`/`resolve_approval` 被调） |
| `test_drafts_routes.py` | list/get/resolve/stats 端点；鉴权；platform 过滤 |
| `test_draft_risk_gating.py` | medium/high **即使 auto_ai 也不自动发**；high 标 handoff + 告警；low+白名单 intent 才 L2 自动；每个自动动作写审计 |

stub 注入沿用 `test_unified_inbox_stage1.py` / `test_rpa_overview.py` 的 `_StubLineService` / `list_approvals` 风格。回归：`python -m pytest tests/ -n auto -q` 全绿。

---

## 9. 验收标准

- [ ] 统一收件箱跨 ≥2 平台看到草稿/批准/驳回/接管，状态实时一致。
- [ ] 统一 resolve 派发后，对应 RPA runner 正常发送（行为与旧分平台 resolve 等价）。
- [ ] 高风险意图（money/privacy/self_harm/投诉）**不会自动发送**，强制 pending；high 触发 handoff + 告警。
- [ ] 每条自动回复可在审计里追溯 risk_level / autopilot_level / 命中上下文。
- [ ] 旧分平台审核页与统一草稿页并存可用；关掉 `/api/drafts` 可无损回退。
- [ ] 全量回归全绿。

---

## 10. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 镜像与源表状态不一致（并发 resolve） | resolve 始终以 owning service 返回为准回写镜像；list 前 `sync_mirror` 兜底；`uq_drafts_source` 防重 |
| 派发到不支持发送的平台 | service 无 `resolve_*`/`send` 时返回 501，UI 提示「需启用 approve 模式」（沿用现有 `/send` 501 逻辑） |
| 风险策略误判把 FAQ 也拦成人工 | 白名单 intent + 阈值可热更；先 review 模式观察命中率再开 auto_ai |
| 动到 RPA runner | 不动 runner，统一层只镜像+派发；新草稿才走统一层 |

---

## 11. 与前后 Phase 的接口

- **依赖 Phase A**：`reply_drafts.conversation_id` 外联 `conversations`；与同库共用 connection。
- **依赖 Phase C**：`risk_level`/`risk_reasons`/`autopilot_level` 来自 `ChatAssistantService.analyze()`；`translated_preview` 来自 `TranslationService`（中文草稿译回客户语言）。
- **供 Phase E**：`reply_drafts` 的 decided_by/created_at/sent_at 是客服绩效（响应时长/解决率）与 SLA 报表的数据源。

---

*本设计与 2026-05-31 代码版本对应。实现前 `grep` 复核 4 张源表的 service 方法签名（`list_pending`/`resolve_pending`/`list_approvals`/`resolve_approval`），以代码实况为准。*
