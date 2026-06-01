# AI 跨境电商客服平台 — 升级开发文档 v2（落地优化版）

更新日期：2026-05-31
状态：**权威升级蓝图**，对照实际代码校准

---

## 0. 本文档的定位与前置说明

本文档是 [`AI跨境电商客服平台_竞品调研与升级开发文档.md`](AI跨境电商客服平台_竞品调研与升级开发文档.md) 的**继任优化版**，二者关系：

- 竞品调研文档（v1）：负责**市场定位、竞品矩阵、产品愿景**——这部分依然有效，不重复。
- 本文档（v2）：负责**把愿景落到当前代码实况上**——重新盘点「已建成 / 半成品 / 真缺口」，并据此**重排优先级**与**点名架构债**。

> ⚠️ 校准原则（遵循 `CLAUDE.md` / `AGENTS.md`「以代码为准」）：v1 与更早的 [`开发升级与优化建议.md`](开发升级与优化建议.md) 里有若干已过期描述，本文档逐条纠正（见 §1.3）。后续若代码再变，**先 `grep` 验证再信任本文档**。

---

## 1. 现状校准：代码实况 vs 旧文档

### 1.1 真实分层架构（按实际目录）

| 层 | 实际位置 | 现状 |
|---|---|---|
| 渠道执行层 | `src/client/`(Telegram MTProto)、`src/integrations/line_rpa/`、`messenger_rpa/`、`whatsapp_rpa/`、`line_webhook.py`、`facebook_webhook.py` | 5 平台 × 2 接入（官方 API + RPA）已运行 |
| RPA 共享基建 | `src/integrations/rpa_base/`(协议)、`shared/`(设备协调/热插拔/事件总线)、`safety/guardrail.py`、`ha/leader_lock.py` | 设备注册、限流、内容护栏、leader 锁已成型 |
| 联系人/交接 | `src/contacts/`（store/gateway/handoff/merge/journey_fsm/reactivation/kpi_alerting/mobile_bridge） | 跨平台 Contact/Journey/HandoffToken 主线完整，feature-flag 控制 |
| AI 栈 | `src/ai/`(ai_client / translation_service / chat_assistant_service / tts_pipeline / audio_pipeline / llm_cost)、`src/skills/`、`src/trigger/four_layer_trigger.py` | LLM 客户端 + 翻译服务 + 规则版意图分析 + 四层触发 + TTS |
| 知识库 | `src/utils/kb_store.py`（11 张 kb_* 表，BM25）、`domains/*/kb/` | 多版本 KB + 反馈 + 漏检日志 + 图片 |
| 域包系统 | `domains/`（payment/ecommerce/general/conversion/community/crypto/education/it_helpdesk/legal） | 9 个域包，manifest/persona/hooks/kb/web 声明式插件化 |
| Web 后台 | `src/web/admin.py`(~6800 行) + `src/web/routes/`(11 个路由模块) | 含统一收件箱、各平台 RPA 管理、persona 编辑、KB 导入 |
| 可观测 | `src/monitoring/`、`src/utils/audit_store.py`/`event_tracker.py`、Grafana | metrics/audit/event 三套 + 仪表盘 |

### 1.2 关键发现：v1 路线图的一半已是「半成品」而非「待建」

竞品文档 v1 把以下列为「待开发」，但代码里**已有 MVP 级脚手架**，应改为「**升级到生产级**」而非「从零建」：

| v1 计划项 | 实际状态 | 证据（真实文件） |
|---|---|---|
| 统一收件箱 2.0 | **已有页面+API**（聚合 4 平台、translate/analyze/automation/send 端点；automation 模式 `manual/review/multi_choice/auto_ai`） | `src/web/routes/unified_inbox_routes.py`、`templates/unified_inbox.html` |
| Translation Service | **已有**（语言检测 + TTL 缓存 + provider-optional + `provider_unavailable` 优雅降级） | `src/ai/translation_service.py` |
| Intent & Context Analyzer | **已有规则版**（intent/emotion/risk/relationship_stage/next_step/suggestions，返回 shape 稳定，预留 LLM 升级位） | `src/ai/chat_assistant_service.py::ChatAssistantService` |
| Risk-Based Autopilot | **部分**（unified inbox 有 4 档 automation 模式；ChatAnalysis 已带 risk_level） | 同上 + 各平台 pending/approvals |
| Voice & Media | **已有**（TTS pipeline、audio pipeline、vision_client、image_recognizer、各 RPA 的 media_vision/voice_sender） | `src/ai/tts_pipeline.py` 等 |
| WhatsApp 渠道 | **已有 RPA**（含 intent_detector/lang_detect/media_vision/multi_msg_handler） | `src/integrations/whatsapp_rpa/` |
| CRM & Funnel | **已有 funnel + KPI 告警 + journey** | `src/contacts/`、`src/web/routes/contacts_routes.py` |

### 1.3 必须纠正的文档漂移（错了就别再被误导）

1. **AI 客户端文件名**：不存在 `claude-4.6-oups-high_client.py`。真实文件是 `src/ai/ai_client.py`（类 `AIClient`）。模型 ID `claude-4.6-oups-high` 是占位/虚构，已知出现在多份 deprecated 文档里（见 `CLAUDE.md` 列表）。
2. **AI provider 真实路由**：`ai_client.py` 只实现两条分支——`gemini`（默认，native `google-genai`）与 `openai_compatible`（OpenAI SDK + `ai.base_url`，用于 Ollama / DeepSeek / vLLM）。`config.example.yaml` 写的 `provider: "deepseek"` **不是独立分支**，会 fall through 到 Gemini；要真正用 DeepSeek 必须 `provider: openai_compatible` + `base_url: https://api.deepseek.com`。
3. **不是单账号单客户端**（旧 `开发升级与优化建议.md` 的说法）：已有多账号 registry（`src/client/`）、多平台多 service 实例（`*_rpa_services` 列表）。
4. **没有 `database.py`**：schema 分布在各 `*store*.py`，ALTER 用 `PRAGMA table_info` 内联迁移（与 `CLAUDE.md` 的「集中到 database.py」措辞不符——实际约定是「集中到各子系统的 store.py」）。
5. **不存在的命名抽象**：代码里**没有** `IntentAnalysisService`、`reply_drafts` 统一表、`ChannelAdapter` 类、`MessageNormalizer` 类。这些是 v1 文档的设计名词，落地时要么复用现有等价物（`ChatAssistantService`、`draft_log`、`RpaService` Protocol），要么本次新建（见 §3）。

---

## 2. 真正的缺口（v2 聚焦点）

把「半成品」与「真缺口」分开后，剩下的硬骨头才是 v2 该投入的地方：

### 2.1 数据持久化缺口（最高优先）

- **统一收件箱是「实时聚合」而非「持久会话」**：`unified_inbox_routes.py` 每次从各平台 state store 现读现拼（`_message_obj`/`_normalize_chat` 内联），**没有 `conversations` / `messages` 主表**。后果：跨平台会话历史、SLA 计时、转化漏斗都缺一个统一事实源。
- **草稿分散在 4 处**：`contacts.draft_log` + `line_rpa_pending` + `wa_rpa_pending` + `messenger_rpa_approvals`，没有统一草稿/审批层，UI 要分平台特判。
- **翻译记忆未持久化**：`translation_service` 只有进程内 TTL 缓存，重启即失、无术语库、无引擎成本统计、无命中率。

### 2.2 抽象缺口

- **没有 Channel Adapter 统一契约**：`rpa_base/protocols.py` 的 `RpaService`/`RpaStateStore` 只覆盖 RPA，官方 API（line_webhook/facebook_webhook）与 Telegram 各走各的。新增渠道仍要改多处。
- **没有 Message Normalizer**：各平台消息结构、媒体、语言、客户 ID 在 unified inbox 里临时归一，无共享模型。

### 2.3 能力缺口

- **意图分析仍是规则版**：`ChatAssistantService` 是 rule-first，未接 LLM（虽预留了 `ai_client` 入口与稳定返回 shape）。
- **电商工具层是空的**：`domains/ecommerce/` 只有话术 KB，无 Shopify/WooCommerce/物流/库存真实连接器，且该域包**缺 persona.yaml/hooks.py**（9 个域包里唯一缺的）。
- **无商业化层**：无多租户、套餐计量、子账号字符量、部署向导。

---

## 3. v2 优化后的开发路线（重排优先级）

设计原则：**先补「事实源 + 抽象」地基，再在地基上把半成品拉到生产级，最后做电商连接器与商业化。** 这样避免在临时聚合层上反复返工。

### Phase A — 统一数据地基（2 周）｜对应缺口 §2.1 + §2.2

> 这是 v1 没有显式拆出、但实为一切上层能力前提的一步。

**A1. 统一消息模型 + 持久层（新建 `src/inbox/`）**
- 新建 `conversations` / `messages` / `message_analysis` 三表（建议落在新 `src/inbox/store.py`，沿用现有 SQLite + `PRAGMA table_info` 迁移范式）。
- 字段对齐 v1 §10：messages 存原文/译文/语言/方向/媒体/平台 message id；conversations 存平台/联系人/状态/负责人/最后消息/风险等级。
- 与 `src/contacts/` 打通：conversation.contact_id 外联 contacts，复用已有跨平台身份合并（`merge.py`）。

**A2. Channel Adapter 协议（扩展 `rpa_base/protocols.py`）**
- 抽象出 `ChannelAdapter` Protocol：`fetch_recent(account)` / `send(task)` / `normalize(raw) -> Message`，让 RPA service、官方 webhook、Telegram client 都实现同一接口。
- `unified_inbox_routes.py` 改为面向 `ChannelAdapter` 列表，删除 `_get_line_services`/`_get_whatsapp_services` 等平台特判分支。

**A3. Message Normalizer（提炼 `src/inbox/normalizer.py`）**
- 把 unified inbox 里内联的 `_message_obj`/`_normalize_chat` 提为共享 `normalize()`，输出 A1 的统一 Message。

**验收**：
- 4 平台消息落入同一 `messages` 表，可按 conversation 跨平台查历史。
- 新增一个「假渠道」适配器只需实现 `ChannelAdapter`，不改 unified inbox 核心。
- 回归：`python -m pytest tests/ -n auto -q` 全绿 + 新增 inbox 持久层测试。

### Phase B — 草稿/审批统一 + 风险自动驾驶（2 周）｜对应 §2.1 草稿分散 + v1 Risk-Based Autopilot

**B1. 统一草稿层 `reply_drafts`**
- 新建 `reply_drafts`（AI 草稿/译文/审批状态/发送结果/操作者/风险等级/来源 conversation）。
- 让 `contacts.draft_log` 与各平台 pending/approvals **写入或镜像**到统一表（先做读聚合视图，再逐步迁移写入，避免一次性破坏 RPA 主线）。

**B2. 风险分层落地（复用 ChatAnalysis.risk_level）**
- 把 unified inbox 的 `automation` 4 档模式（`manual/review/multi_choice/auto_ai`）正式接到 §v1 的 L0–L4 风险策略：
  - L0 只译不回 / L1 草稿待审 / L2 低风险自动 / L3 中风险审批 / L4 高风险强制人工。
- 退款、优惠、支付、投诉、敏感信息 → 强制 ≥ L3，且所有自动动作写 `agent_actions` 审计（复用 `audit_store`/`event_tracker`）。

**验收**：
- 统一收件箱可跨 ≥2 平台看到草稿/批准/驳回/接管。
- 高风险意图不会自动发送（用例覆盖 refund/complaint/payment）。
- 每条自动回复可追溯命中的上下文与 KB。

### Phase C — 意图分析 LLM 升级 + 翻译产品化（2 周）｜对应 §2.3 + §2.1 翻译记忆

**C1. ChatAssistantService 接 LLM（保持返回 shape 不变）**
- 在现有 rule-first 之上加 LLM 评分通道（已预留 `ai_client` 入口）：规则做兜底、LLM 做提升，输出仍是 `ChatAnalysis`。
- 分析结果落 `message_analysis` 表（Phase A 建），供 SLA/漏斗复用。

**C2. 翻译产品化（升级 `translation_service`）**
- 新增 `translation_memory` 持久表（原文 hash/译文/引擎/术语版本/命中次数）。
- 术语库：电商专有词（尺码/颜色/物流/退款/材质/保修）+ 域包级术语（复用 payment 的 `terminology.yaml` 范式）。
- 多引擎可插拔接口（LLM / Google / DeepL / 腾讯 / 百度），默认 LLM，成本统计接 `llm_cost.py`。

**验收**：
- 意图识别在样本集 ≥85%，且 LLM 故障时自动回落规则版不报错。
- 重复句子命中缓存，跨重启仍有效；翻译成本可在后台看到。

### Phase D — 电商工具层（3–4 周）｜对应 §2.3 电商连接器（v1 的核心突破口）

**D0. 先补齐 `domains/ecommerce/`**：加 `persona.yaml` + `hooks.py`，与其它 8 域包对齐结构。

**D1. 连接器（二选一优先 Shopify）**
- `src/ecommerce_tools/` 下做工具调用层：订单查询、物流查询（17Track/AfterShip）、库存/SKU、退款政策判断。（实施时改名避开仓库顶层 `tools/` 命名空间包冲突——测试会把 `src/` 加进 sys.path）
- 插件式：通过域包 manifest 声明可用工具（沿用 Phase 3/4A 已建成的「manifest 声明式 + 注册表」模式，不在核心硬编码行业逻辑）。
- 新增 `orders_cache` 只读缓存表 + `agent_actions` 工具调用审计。

**D2. 回复事实校验**
- AI Reply Engine 在生成涉及订单/物流/库存/价格的回复前，强制经过工具结果校验——查不到就明确标注未知，不编造（接现有 KB direct-reply 的事实锚定范式，见 `docs/KB_DIRECT_REPLY_SPEC.md`）。

**验收**：客户问订单/物流，AI 能调工具并生成数据真实的回复；典型电商 FAQ 自动解决率 ≥50%。

### Phase E — 后台收敛 + 商业化（3–4 周）｜对应 §2.3 商业化 + 架构债

**E1. admin.py 拆分**（架构债，~6800 行）
- 沿用已建成的 `src/web/routes/` 模块化 + `WebContext` 依赖容器范式，把 admin.py 里仍内联的路由按域迁出，目标核心 admin.py < 1500 行。

**E2. 商业化基础**
- 多租户（租户/用户/角色/权限）+ 套餐计量（字符/AI token/语音分钟/账号数/设备数，接 `llm_cost.py` + 设备注册）。
- 部署向导（配置 AI/翻译/渠道/设备/Webhook）+ 账号健康面板（复用 `shared/device_*` + RPA 成功率/失败截图）。

**验收**：新客户按向导完成基础配置；单租户私有化稳定运行 7 天；关键错误有告警与恢复建议。

---

## 4. 路线对比：v1 vs v2

| 维度 | v1 竞品文档 | v2 落地优化版 |
|---|---|---|
| 起点假设 | 多数能力「待建」 | 多数能力「已有 MVP，需升级到生产」 |
| 第一步 | Phase 1 统一收件箱（从 pending 升级） | **Phase A 统一数据地基**（先补 conversations/messages 事实源 + Channel Adapter） |
| 翻译 | 新建 TranslationService | **升级**已有 service：加持久化记忆 + 术语库 + 多引擎 |
| 意图 | 新建 IntentAnalysisService | **复用** ChatAssistantService，规则版上叠 LLM，保持返回 shape |
| 草稿 | 新建 reply_drafts | 新建统一表 + **渐进镜像**现有 4 套 pending/approvals（不破坏 RPA 主线） |
| 风险自动驾驶 | 全新 L0–L4 | **接线**已有 automation 4 档 + ChatAnalysis.risk_level |
| 电商 | Phase 3 | **Phase D**，且先补 ecommerce 域包缺的 persona/hooks |
| 架构债 | 未点名 | 显式列出：admin.py 拆分、Channel Adapter、Message Normalizer、草稿统一 |

---

## 5. 架构债清单（独立追踪）

按 ROI 排序，可穿插进各 Phase：

| 优先级 | 债务 | 位置 | 建议 |
|---|---|---|---|
| 高 | 无统一会话/消息事实源 | `unified_inbox_routes.py` 实时聚合 | Phase A 建 `src/inbox/store.py` |
| 高 | 草稿/审批分散 4 处 | contacts + 3 个 RPA state store | Phase B 统一 `reply_drafts` |
| 中 | admin.py 巨石（~6800 行） | `src/web/admin.py` | Phase E 按域迁出到 routes/ |
| 中 | 渠道无统一适配契约 | `rpa_base/protocols.py` 仅覆盖 RPA | Phase A 扩 `ChannelAdapter` |
| 中 | AI provider 配置陷阱 | `config.example.yaml` 的 `deepseek` 不生效 | 改 example + 文档说明 `openai_compatible` |
| 低 | ecommerce 域包结构残缺 | `domains/ecommerce/` 缺 persona/hooks | Phase D0 补齐 |
| 低 | 翻译无持久记忆/成本统计 | `translation_service.py` 仅内存缓存 | Phase C2 |

---

## 6. 范围与边界（硬约束，勿越界）

遵循 [`docs/PROJECT_SCOPE.md`](PROJECT_SCOPE.md)：

- **不在本 repo**：Facebook 加好友/打招呼/FB App 内直发、`fb_contact_events`/`facebook_inbox_messages` 表、VLM Level 4 fallback 栈 → 全在 `github.com/victor2025PH/mobile-auto0423`。
- 本 repo 的 `messenger_rpa/` 是 **Android Messenger RPA**，与 mobile-auto0423 是两套独立实现，**不共享代码**，只通过 contacts 子系统的 Messenger→LINE 引流主线做业务衔接（`src/contacts/mobile_bridge.py` 走 `openclaw.db` 同步）。
- 新子系统一律默认 `enabled: false`（参考 `contacts.enabled`），schema 迁移集中到对应子系统的 `*store*.py`。

---

## 7. 节奏与里程碑

| 里程碑 | 包含 Phase | 周期 | 标志 |
|---|---|---|---|
| 地基就绪 | A | 2 周 | 统一 conversations/messages + Channel Adapter 落地，回归全绿 |
| 客服工作台可用 | A+B+C | ~6 周 | 跨平台会话/草稿/风险审批/LLM 意图/产品化翻译 闭环 |
| 电商突破 MVP | +D | ~10 周 | Shopify 订单/物流真实查询 + 事实校验，FAQ 自动解决率 ≥50% |
| 可商用交付 | +E | ~14 周 | 多租户 + 计量 + 部署向导 + 健康面板，单租户私有化稳定 7 天 |

---

## 8. 技术原则（沿用 v1，保留有效项）

1. 优先官方 API，其次移动端 RPA，最后才 Web 自动化；不把核心押在无头浏览器。
2. 所有渠道走统一消息模型（Phase A 是其落地）。
3. AI 自动发送必须风险分层 + 可审计。
4. 翻译产品化：有缓存、术语库、引擎、成本（Phase C2）。
5. 电商数据只能查不可编（Phase D2 事实校验）。
6. 优先私有化部署与数据主权。

---

*本文档与 2026-05-31 代码版本对应。后续功能/结构有较大变更时同步修订，并以 `git log` + `grep` 验证实况优先于本文档。*
