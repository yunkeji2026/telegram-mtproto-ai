# 陪伴产品 · 分阶段开发与进度（DEVLOG / 持久记忆）

> **本文件是「记忆」**：每完成一个阶段就回写进度+测试结果+下一阶段优化笔记。
> 重入时（忘了做到哪）：先读 §0 重入指引 → §3 阶段进度表 → 找到第一个未完成阶段继续。
> **以代码为准**（教训：`docs/project_tasklist_drift.md`，文档曾落后于代码）。

---

## 0. 重入指引（忘记怎么工作时看这里）

1. 读 §3 进度表，找第一个 `🔄进行中` 或 `⬜未开始` 的阶段。
2. 进入该阶段的 §4.x 小节，按「任务清单」继续未打勾项。
3. 完成后：跑该阶段「测试命令」→ 把结果填进 §4.x「测试结果」+ 在 §3 标 ✅。
4. 写「本阶段优化笔记 / 下一阶段改进点」到 §4.x 末尾，再进入下一阶段。
5. 全部阶段 ✅ 后 → §5 收尾：连跑 2 次全量测试，任一不过就补测试再跑，直到全绿。

**回归命令**（本机务必带 timeout 兜底）：
```
python -m pytest tests/ -n auto -q --timeout=120 --timeout-method=thread
```
单阶段快测：只跑该阶段相关 `tests/test_*.py`（见各阶段）。

---

## 1. 产品定位（北极星）

**全球化、安全、可白标的 AI 情感陪伴数字员工 · 7×24 无人值守**。
不打「翻译聚合」红海（海王/拓译/OneChat 已同质化价格战）；护城河 = **陪伴深度（人设/共情/记忆/安全网）+ 真·全自动**。翻译与多平台只补到「不被否决」。

---

## 2. 代码实况盘点（4 档，2026-06-18 勘探）

- ✅ 成熟：文本翻译(AI)+出站一击译+TM/术语；TG 单号 MTProto 全链；统一收件箱(claim/SSE/草稿L1–L4/autosend/SLA)；KB；陪伴 prompt 层(persona_guard/empathy/wellbeing 默认开)；G1 Kill-Switch。
- 🟡 半成品/默认关：DeepL/Google 多引擎(需key,UI只读)；入站自动译(关)；图片/语音媒体译(关)；LINE/Msgr/WA RPA(成熟但默认关/需真机)；Web chat；自研指纹+代理+Electron partition；预热闸门(默认不拦)；语音克隆/TTS；License(enforce不生效)；Billing/白标(单租户)；Electron 桌面。
- 🔩 骨架待真号：TG/WA 协议多开+CompanionWorker；记忆 consolidation/salience/矛盾/来源分级(代码全默认全关)；IntimacyEngine/reunion/reactivation；Faceswap 客户端。
- ❌ 没有：Instagram/Zalo/Discord/TikTok/Signal/Skype/微信/QQ adapter；换装；多租户 SaaS；坐席多线路对照选译 UI。

**结论**：多数功能「代码在、默认关、未真号验证」。开发主线 = **激活+验证+补真空白**，而非重建。

---

## 3. 阶段进度表

| 阶段 | 目标 | 自治性 | 状态 |
|---|---|---|---|
| **A** | G2 封号信号自动急停（分类器+接线） | 全自治(代码+单测) | ✅ 完成 |
| **B** | G3 金丝雀放量（cohort+扩面） | 全自治 | ✅ 完成 |
| **C** | RPA 三端 kill-switch 覆盖（真·全局） | 全自治 | ✅ 完成 |
| **D** | 陪伴记忆闭环激活（extract 默认+preset） | 全自治 | ✅ 完成 |
| **E** | 翻译语种 20→60+ | 全自治 | ✅ 完成（80+） |
| **F** | 坐席多线路对照选译 API/UI | 全自治 | ✅ 完成 |
| — | N 线真号 E2E / 新平台 RPA / 多租户 SaaS / 截图翻译 | **需外部资源**(真号/真机/key) | ⏸ 暂挂（本轮只做代码侧准备/桩） |

图例：⬜未开始 🔄进行中 ✅完成 ⏸暂挂

---

## 4. 各阶段详情与进度回写

### 4.A · G2 封号信号自动急停

**目标**：发送时命中平台风控错误 → 按类型分级处置（退避/暂停/封禁）+ 告警，避免硬怼到死。

**任务清单**：
- [x] `src/ops/ban_signal.py`：纯函数 `classify(exc)`（按异常类名+属性，零 pyrogram 硬依赖）
- [x] 处置落地：pause/ban → **账号级 Kill-Switch**（复用 G1）；ban 另标 registry meta.banned + publish_alert
- [x] 接线 B 线 `protocol_autoreply._send`（包住 orch.send except → 分级处置后抛回既有熔断）
- [x] 接线 A 线 `sender._send_reply` except 分支
- [x] 单测 `tests/test_ops_ban_signal.py`（11 项）

**测试命令**：`python -m pytest tests/test_ops_ban_signal.py tests/test_ops_kill_switch.py tests/test_account_signals.py -q`

**测试结果**：✅ 45 passed（含 G1）；protocol_autoreply 回归 52 passed，无回归。

**优化笔记 / 下一阶段改进**：
- **重大优化（推翻原设计稿）**：原计划在 registry 写 `meta.paused_until` 并教 `gate` 认 paused。
  实施时改为「**pause = 账号级 Kill-Switch + TTL**」，**完全复用 G1**：自动 TTL 恢复、A/B(后含RPA)
  全路径强制、持久化、API 可见，**零新增拦截路径、且无视 gate 是否开**。因此**取消**了原
  `account_signals 识别 paused_until` 这一任务（不再需要）。
- classify 用「类名集合 + 关键词兜底」双保险，跨 pyrogram 版本命名差异更稳；明确把自家
  `send_gate_blocked/kill_switch_blocked` 归 none，避免把自己抛的控制流误判为封号。
- 下一阶段（G3）可复用同一 `ops` 模块范式 + runtime_flags.db。
- 待真号验证点：真实 FloodWait/PeerFlood 的异常类名与 `.value` 属性需真号回归确认（已用关键词兜底降低风险）。

---

### 4.B · G3 金丝雀放量

**目标**：自动化先在小批 cohort 真发，绿灯稳定才扩面，限制爆炸半径。

**任务清单**：
- [x] `src/ops/canary.py`：cohort（pinned ∪ 持久扩面集 CanaryStore@runtime_flags.db）+ `is_held` 白名单语义
- [x] 决策期接线 B 线 `run_autoreply`（kill_switch 之后 → `canary_hold` 早退；未启用零破坏）
- [x] `plan_expansion` 纯函数（绿灯才扩面，最多 step；非绿灯不推进）→ 供 watchdog 调
- [x] API `/api/ops/canary`（GET 状态 / POST 扩面 / DELETE 移除清空）+ admin 注册 + 路由清单基线
- [x] config schema（`ops.canary.*` 默认 enabled:false）
- [x] 单测 `tests/test_ops_canary.py`（12 项）

**测试命令**：`python -m pytest tests/test_ops_canary.py tests/test_admin_route_inventory.py -q`

**测试结果**：✅ 37 passed（含路由清单+config 预设）；autoreply 回归 52 passed，无回归。

**优化笔记 / 下一阶段改进**：
- **优化（推翻原设计稿）**：原计划把 canary 注入 `companion_send_gate.evaluate`，但 evaluate 仅在
  gate_enabled 时跑 → 会被绕过。改为**独立模块 + 决策期早退**，与 G1/G2 同范式，**独立于预热闸开关**。
- canary 语义定为「**默认拦截+白名单**」（与 Kill-Switch 的「默认放行+黑名单」正交），cohort 空=最保守全 hold。
- `canary_enabled(cfg)` 先做廉价 dict 判断，未启用绝不碰 DB → 热路径零开销。
- **下一阶段改进**：`auto_health` 自动扩面尚未接入运行中的 health_watchdog 循环（已备 `plan_expansion` 纯函数 + CanaryStore）。
  待 Phase C 后，在 watchdog tick 里读机群健康→ `plan_expansion` → `store.add`。当前 manual 模式已完整可用、可发布。

---

### 4.C · RPA 三端 kill-switch 覆盖

**目标**：让 `global` 冻结名副其实——LINE/Messenger/WhatsApp runner 发送前各加 `kill_switch.is_blocked`。

**任务清单**：
- [x] 共用守卫 `src/integrations/shared/rpa_send_guard.py::rpa_send_blocked`（薄封装、绝不抛、热路径走内存缓存）
- [x] LINE `_pace_and_send` 顶部接线（冻结→返回 `{ok:False,error:kill_switch}`）
- [x] WhatsApp `_pace_and_send` 顶部接线（同上）
- [x] Messenger `_send_reply` 顶部接线（冻结→`result["kill_switch"]` + 返回 False）
- [x] 单测 `tests/test_rpa_send_guard.py`（6 项：三端 global/platform/account 三级 + 故障放行 + 默认 id）

**测试命令**：`python -m pytest tests/test_rpa_send_guard.py -q`

**测试结果**：✅ 6 passed；全 RPA 回归 `-k rpa` **559 passed**，无回归。

**优化笔记 / 下一阶段改进**：
- 提取**共用守卫**而非 3 处各写 is_blocked → 单点单测、三端一致；runner 仅 1 行调用，改动面最小。
- **下一阶段改进**：runner 的 `self._cfg` 只是平台子配置（无 root `ops`），故 canary 暂未在 RPA 判；
  待把 root cfg 注入 runner 后，守卫可加 `canary.is_held` 让 G3 也覆盖 RPA。
- runner 本体「冻结即不物理发送」需**真机 E2E** 验证（已用守卫单测覆盖判定逻辑，降低风险）。
- 至此**反封号护栏三件套 G1+G2+G3 + RPA 覆盖全部落地**，「激进上量」有了不烧号底座。

---

### 4.D · 陪伴记忆闭环激活

**目标**：`memory.extract.intents` 现为空 → 永不写记忆。补合理默认 + preset，让「记得住」生效。

**根因**：抽取闸 `if intent not in intents`，而 `memory.extract.intents` 缺省空集 → **任何意图都被跳过**，记忆闭环静默失效（dead-closure）。

**任务清单**：
- [x] 把内联闸抽成纯函数 `skill_manager.should_extract_intent(intent, ex_cfg)`（可单测）
- [x] 新增 `extract.match_all` 显式开关（陪伴「全记」）；**未配置/空 intents 仍跳过 → 存量零回归**
- [x] `CHAT_FAMILY_INTENTS` 常量（与 P0-G chat family 对齐，防漂移）
- [x] companion preset 激活闭环：extract(match_all+chat family) + consolidation(去重/矛盾/supersede/source_aware) + salience 全开
- [x] 单测 `tests/test_memory_extract_gate.py`（8 项）

**测试命令**：`python -m pytest tests/test_memory_extract_gate.py tests/ -k "episodic or memory or config_init" -q`

**测试结果**：✅ 134 passed（episodic/memory/config/skill_manager 全域回归 + 新增 gate 单测），无回归。

**优化笔记 / 下一阶段改进**：
- 选择「**显式 match_all + preset 激活**」而非「改全局默认（missing→抽）」：后者会给所有存量部署
  突然加 LLM 抽取 token 成本（行为/成本回归）。当前方案陪伴产品满血、存量零扰动。
- consolidation/salience 这些「巩固/显著性」逻辑代码早已就绪（默认全关），本阶段只是**接通配置**让其在陪伴预设生效。
- 待真号验证点：真实多轮对话下 stable 晋升/矛盾消解的召回与误升率，需真号灰度盯 `[episodic] consolidate` 日志。

---

### 4.E · 翻译语种 20→60+

**目标**：`LANG_NAMES` 扩到 60+ 主流语种，对齐竞品广度。

**任务清单**：
- [x] 扩 `translation_service.LANG_NAMES` 20→**80+**（欧洲/中东中亚/南亚/东南亚/非洲全覆盖）
- [x] 确认确定性检测（脚本范围+拉丁关键词）零回归——扩表仅影响显示名 + 统计回退白名单
- [x] 单测 `tests/test_translation_lang_breadth.py`（含 detection 不回归断言）

**测试命令**：`python -m pytest tests/test_translation_lang_breadth.py tests/ -k "translat or lang or detect" -q`

**测试结果**：✅ 412 passed（新增广度单测 + 全翻译/语种/检测域回归），无回归。

**优化笔记 / 下一阶段改进**：
- 扩表是**纯增量**：确定性核心只产出它已知的码，不会因表大而误判；新增码主要供 AI 翻译 prompt
  命名 + 统计回退（默认 None）放行面。
- **下一阶段改进**：新增语种暂无确定性检测规则（如孟加拉/泰米尔脚本范围未进 `_SCRIPT_RE`）。
  若要「自动识别」这些语种，需补脚本正则或挂统计检测器（`set_statistical_detector`）。当前作为
  **目标语种**（用户指定译入）已全可用；作为**自动识别源语种**的覆盖待后续补脚本规则。

---

### 4.F · 坐席多线路对照选译

**目标**：坐席可手动选翻译引擎/线路（对标拓译 10+ 线路对照）。引擎矩阵 API 已在，补可选路。

**任务清单**：
- [x] `EngineRouter.translate_with(name,...)` 强制指定引擎（不故障转移）+ `engine_by_name`
- [x] `EngineRouter.compare(...)` 并发多引擎对照（不可用/不支持也返回行，前端灰显）
- [x] `TranslationService.compare_translations(...)`（含术语强制+品牌词保护 mask→译→restore；**不污染缓存/记忆**）
- [x] API `POST /api/unified-inbox/translate-compare`（支持 target_lang:auto）+ 路由清单基线
- [x] UI：unified_inbox 「⇄ 多线路对照」按钮 + 点选填入输入框的极简择优弹层
- [x] 单测 `tests/test_translation_compare.py`（10 项）

**测试命令**：`python -m pytest tests/test_translation_compare.py tests/ -k "translat or unified_inbox or admin_route" -q`

**测试结果**：✅ 264 passed（含 compare 单测 + 全翻译/收件箱/路由清单回归），无回归。

**优化笔记 / 下一阶段改进**：
- 对照结果**故意不写缓存/记忆**：择优是一次性比较，非首选引擎结果不应污染翻译记忆；坐席择优后走正常
  `/translate` 或 `/send` 落库。这与「翻译单一真相源」一致。
- `compare` 用 `asyncio.gather` 并发，多线路对照延迟 = 最慢单引擎，而非串行累加。
- **下一阶段改进**：可把择优结果一键「设为该会话偏好引擎」（持久化 per-conversation 引擎偏好），
  让后续一击直发默认走坐席选中的线路。当前为一次性对照，未持久化偏好。

---

## 5. 收尾（全部阶段 ✅ 后）

- [x] 连跑全量测试第 1 次：**5174 passed, 31 skipped, 0 failed**（351s）
- [x] 连跑全量测试第 2 次（确认稳定，非偶发）：**5174 passed, 31 skipped, 0 failed**（305s）
- [x] 两次连续全绿，无需补测试修复
- [x] 更新 §3 全部 ✅，§2 盘点同步代码新实况（见下）

### 代码新实况（本轮交付后，§2 增量）

- ✅ **反封号护栏三件套全落地**：G1 Kill-Switch（三级作用域+TTL+持久化+API）、G2 封号信号自动急停
  （classify 分级→pause/ban 复用账号级 KS）、G3 金丝雀放量（白名单 cohort+API），且 **A/B/RPA 三端发送路径全覆盖**。
- ✅ **陪伴记忆闭环可激活**：`should_extract_intent` + `match_all` 开关，companion preset 一键开
  extract/consolidation/salience（修复 intents 空导致的 dead-closure）。
- ✅ **翻译语种 80+**（原 20）。
- ✅ **坐席多线路对照选译**：`translate_with`/`compare` + `/api/unified-inbox/translate-compare` + UI 择优弹层。

### 新增测试文件（本轮）

`tests/test_ops_ban_signal.py`(11) · `tests/test_ops_canary.py`(12) · `tests/test_rpa_send_guard.py`(6) ·
`tests/test_memory_extract_gate.py`(8) · `tests/test_translation_lang_breadth.py`(~13) ·
`tests/test_translation_compare.py`(10)。全量 5117→5174（+57）。

### 仍待外部资源（⏸，本轮已备代码侧）

- N 线真号 E2E（FloodWait/PeerFlood 真实异常类名核对；记忆 stable 晋升召回率灰度）。
- RPA「冻结即不物理发送」真机 E2E（判定逻辑已单测）。
- canary 覆盖 RPA（需 root cfg 注入 runner）；auto_health 接入运行中 watchdog 循环。
- 新增语种作为「自动识别源语种」需补 `_SCRIPT_RE` 或挂统计检测器（作为目标语已全可用）。
- per-conversation 引擎偏好持久化（对照择优后默认走选中线路）。

---

## 6. 变更历史（每阶段追加）

- 2026-06-18：建档。G1 Kill-Switch 已于建档前完成（`src/ops/kill_switch.py` + A/B 接线 + API + 单测，全量回归 5117 passed）。
- 2026-06-18：Phase A ✅ G2 封号信号自动急停（`src/ops/ban_signal.py`，pause/ban 复用账号级 KS）。
- 2026-06-18：Phase B ✅ G3 金丝雀放量（`src/ops/canary.py` + `/api/ops/canary` + config schema）。
- 2026-06-18：Phase C ✅ RPA 三端 kill-switch 覆盖（`src/integrations/shared/rpa_send_guard.py` + 三 runner 接线）。
- 2026-06-18：Phase D ✅ 陪伴记忆闭环激活（`should_extract_intent`+`match_all` + companion preset）。
- 2026-06-18：Phase E ✅ 翻译语种 20→80+（`LANG_NAMES`）。
- 2026-06-18：Phase F ✅ 坐席多线路对照选译（`compare`/`translate_with` + compare API + UI）。
- 2026-06-18：**收尾全绿** —— 全量测试连跑 2 次均 5174 passed / 31 skipped / 0 failed。所有阶段完成。
- 2026-06-19：竞品 GAP 分析，定 Tier1-3 下一批（G–M）。用户选 Tier1 起做 Phase G。
- 2026-06-19：Phase G ✅ 官方 API 通道 —— G1 WhatsApp Cloud API 新增（`whatsapp_cloud.py`，+9 测试）；
  G2 LINE/Messenger/WA 官方发送统一纳入 Kill-Switch（+8 测试）。回归 102 passed 无回归。
  发现 LINE/Messenger 官方通道早已存在，本阶段补真空白 + 护栏统一。
- 2026-06-19：Phase G 延伸 ✅ 官方通道接入编排器 `mode=official` 出站 worker
- 2026-06-19：Phase G4 ✅ 官方三端入站/出站镜像进统一收件箱（`inbox_mirror.py`，+6 测试，全量 5211 passed）——闭合「官方入站绕过收件箱」裂缝，官方渠道成统一收件箱一等公民（可见/SLA/危机接管）
- 2026-06-19：Phase G4b ✅ 官方渠道坐席接管发送闭环（`dest_from_chat_key` 归一 chat_key + fb account_id 透传，+12 测试，全量 5223 passed）——坐席从收件箱发送经 orch.send→官方 worker 正确回到官方渠道（修「发错人」隐患）
- 2026-06-19：Phase G4c ✅ 官方入站迁入 protocol_autoreply 主管道（`official_pipeline.enabled` 开关默认关，+5 测试，全量 5228 passed）——开启后官方入站享 kill-switch/canary/陪伴记忆/限速熔断/审计/转人工，与协议号对齐；Phase G 收官
- 2026-06-19：Phase H ✅ 平台广度 Instagram + Zalo（`instagram_webhook.py`/`zalo_webhook.py`/共享 `official_inbound.py`，+12 测试，全量 5240 passed）——两端官方适配器一步到位接入 G2 护栏 + G4 收件箱镜像 + G4c 主管道 + 编排器 mode=official；Tier 1 基本完成
- 2026-06-19：Phase J ✅ License enforce + Billing（审计发现基建已成熟，唯 `seat_exceeded` 未 wired；补 `seat_block_on_online` 接入坐席上线边界，+8 测试，全量 5248 passed）——授权席位上限真正强制；多租户 SaaS 留作独立大版本
- 2026-06-19：Phase K ✅ C 端变现·月度消息配额软限（`message_quota_status`/`plan_included_messages` + 用量看板卡片，+9 测试，全量 5257 passed）——本月消息量 vs 套餐含量软提示（ok/warn/over），软限不硬切走超额计费
- 2026-06-19：Phase I ✅ 媒体 AI·官方入站媒体可见化（`media_placeholder`/`mirror_inbound_media` + LINE/WA/FB 非文字分支镜像占位，+13 测试，全量 5270 passed）——客户端层（faceswap/voice/tts）早已成熟待 GPU；本轮补「坐席台看见客户发的图片/语音」
  （`official_api_worker.py`，+12 测试）。主管道可经官方 API 发；orchestrator 回归 224 passed。
- 2026-06-24：**Phase H2 ✅ 官方通道发送错误统一分类 + IG 24h 窗口回退（可上线收口）**
  - **背景（回正轨·用户选"多平台官方通道收口"）**：盘点发现 Messenger 早有 `fb_send_with_window_fallback`（24h 窗外降级 MESSAGE_TAG 重发），但 **WhatsApp/Instagram/Zalo 零窗口感知**——窗外/token 失效/限速失败都被打包成不透明 `"HTTP 4xx: ..."` 串**默默丢**，回复没送达却无人知、无法分流。陪伴产品有延迟/主动回复，窗口外静默失败是真·生产隐患。
  - **改动**：① 新建纯模块 `src/integrations/shared/official_send_error.py::classify_official_send_error`——跨平台错误归一表（window_expired/invalid_token/rate_limited/recipient_unavailable/unsupported/transient），按各家 error.code（WA 131047… / Graph code+subcode 2534022 / Zalo -213…）+ 文本关键词 + HTTP 状态三级兜底，设计同 `ban_signal.classify`（纯函数、零网络、可注入假响应单测）。② wa/ig/zalo 三个 send 助手失败路径透出 `error_kind`+`retriable`（additive，旧测全过）。③ `OfficialApiWorker.send` 统一 `_result()`，失败时透出 `error_kind`（供 pipeline/可观测分流，不再当"没发出"）。④ `ig_send_with_window_fallback`（opt-in `instagram.human_agent_fallback`，默认关）——窗外降级 MESSAGE_TAG=HUMAN_AGENT 重发，与 Messenger 对称（HUMAN_AGENT 需账号权限，未开通时回退仍失败但已可观测）。
  - **再优化**：① 选**统一分类器**而非给每端各写一套窗口判断——一处分类多处复用，新增平台只加一张码表；② IG 回退 **opt-in 默认关**（HUMAN_AGENT 需平台审批，默认开会让回退本身失败）；③ 全程 additive + 软吞，零回归。
  - **能否再优化**：① WA/Zalo 窗外自由文本无模板无法补救——下一步可做 **window_expired→转人工/系统提示镜像**（让坐席台看见"这条没发出去，需模板/人工"）；② WA **模板消息**发送（需平台审批模板，属运营+代码）；③ 把 error_kind 汇成**官方通道送达率看板**。
  - **回归**：official 全家桶（send_error/phase_h/whatsapp_cloud/api_worker/kill_switch/inbox_mirror/pipeline/takeover/inbound_media）**92 passed**；send-path 审计 + fb/line webhook **16 passed**；五个官方模块编译通过；无 lint。

---

## 7. 竞品对标增量 GAP 与下一批阶段（G+，2026-06-19 分析）

### 7.1 逐竞品拆解（distinctive 能力 → 我方代码实况 → GAP）

| 竞品 | 它的杀手锏 | 我方现状 | GAP |
|---|---|---|---|
| **云译 yunyi** | 多引擎翻译 + 多开 SCRM + **群发/批量加粉/客户CRM** | 翻译✅多引擎✅；contacts/标签/关系阶段✅ | 批量加粉/群发营销（**与防封号冲突，刻意不全做**）；客户分组标签 UI 可补 |
| **DeepL** | MT 质量天花板 + **文档翻译** + AI 润色/改写 | LLM 翻译✅术语✅TM✅ | 文档(docx/pdf)翻译❌；AI 润色/改写❌ |
| **海王 haiwang** | 私域出海 SCRM + 自动翻译 + **朋友圈/状态营销** | 同云译 | 状态/朋友圈营销❌（陪伴弱相关） |
| **拓译 tranlico** | **多线路翻译对照** + 跨境电商客服 | 多线路对照✅(Phase F) | per-会话引擎偏好持久化🟡(余留) |
| **engagelab** | **官方 API 全渠道**(WhatsApp Business API/LINE/Push/SMS) 高送达不封号 + 营销 journey | WA/LINE 走 **RPA**(封号风险) | **官方 API 通道❌**(合规放量正路)；营销 journey 编排❌ |
| **onechat** | **AI 换脸/换装/语音克隆** 虚拟人陪聊 + **C 端变现** | faceswap🔩骨架；voice clone/TTS🔩骨架 | 换脸真实接入❌；换装❌；实时语音陪伴❌；**变现闭环(订阅/打赏/礼物)❌** |

### 7.2 下一批阶段（按北极星对齐度排序）

**Tier 1 · 合规放量 + 陪伴护城河（最该做）**
| 阶段 | 目标 | 自治性 | 解决的竞品差距 |
|---|---|---|---|
| **G** | 官方 API 通道适配器（WhatsApp Business API / LINE Messaging API / Messenger Platform）—— 合规、不封号、高送达；与 RPA 并存按账号选路 | 代码+单测自治；真发需企业号/token | engagelab；根治 RPA 封号 |
| **H** | 平台广度补 **Instagram + Zalo**（陪伴出海重点区）adapter 骨架 + 统一 inbox 接入 | 代码自治；真发需真号/真机 | 云译/海王平台广度 |
| **I** | 媒体 AI 深化：faceswap 真实接入 + 实时语音陪伴(ASR 入站→TTS 出站闭环) | 客户端代码自治；需推理服务 | onechat 差异化 |

**Tier 2 · 商业化变现（若要卖 SaaS / to C）**
| 阶段 | 目标 | 自治性 |
|---|---|---|
| **J** | SaaS 多租户隔离 + License enforce 真生效 + Billing 闭环 + 白标完善 | 代码自治 |
| **K** | C 端变现闭环（订阅/打赏/虚拟礼物/付费解锁剧情） | 代码自治 |

**Tier 3 · 翻译质量补齐（对标 DeepL）**
| 阶段 | 目标 | 自治性 |
|---|---|---|
| **L** | 文档翻译(docx/pdf 分段保格式) + AI 润色/改写按钮 | 代码自治 |
| **M** | 截图翻译质量（Vision OCR 接入，现为骨架） | 需 Vision key |
| **F+** | per-会话引擎偏好持久化（对照择优后默认走选中线路） | 代码自治 |

**刻意不做**（与北极星冲突）：大规模群发/批量加粉营销（封号风险，违背「不烧号陪伴」定位）、朋友圈刷屏营销。客户分组标签等「关系深度」侧的 CRM 可补，纯获客群发不做。

> 待用户拍板从哪个 Tier/阶段起做（部分需外部资源：企业 API token / 真机 / Vision key）。代码侧适配器与单测可先行自治完成。

### 7.3 Phase G 勘察结论（2026-06-19，重新界定范围）

**重大发现**：官方通道**已存在 2/3**——
- LINE Messaging API：`src/integrations/line_webhook.py`（reply/push + 验签 + 入站→SkillManager）✅
- Messenger Platform：`src/integrations/facebook_webhook.py`（Send API + 24h 窗口降级 + 验签）✅
- WhatsApp：**仅 Baileys(非官方)+RPA**，**官方 Cloud API 缺失** ❌

**且**：现有官方 webhook 的发送路径**绕过了 Kill-Switch 护栏**（Phase C 只覆盖 RPA）。

→ Phase G 重新界定为两子阶段：

| 子阶段 | 目标 | 自治性 | 状态 |
|---|---|---|---|
| **G1** | WhatsApp **Cloud API 官方适配器**（`whatsapp_cloud.py`：send text + webhook 验证/入站/签名，仿 facebook_webhook）+ admin 注册 + config + 单测 | 代码+单测自治；真发需企业号 token | ✅ 完成 |
| **G2** | **官方通道 Kill-Switch 护栏覆盖**（line_reply/push、fb_send、WA cloud 发送前查冻结）→ 把 Phase C「真·全局」延伸到官方 API | 全自治 | ✅ 完成 |

---

## 8. Phase G 进度回写

### 4.G1 · WhatsApp Cloud API 官方适配器 ✅
- [x] `src/integrations/whatsapp_cloud.py`：`wa_send_text`（Bearer token + 自由文本）+ webhook（GET 验证 / POST 验签 X-Hub-Signature-256 / 入站文字→SkillManager→回发）
- [x] `extract_inbound_messages`（whatsapp_business_account 结构，忽略 statuses/非文字）
- [x] 发送前内建 Kill-Switch 守卫（account_id=phone_number_id）
- [x] admin 注册（缺凭证不注册，与 line/fb 同策略）+ config schema（`whatsapp_cloud.*` 默认 enabled:false）
- [x] 单测 `tests/test_whatsapp_cloud.py`（9 项：验签/解析/payload/HTTP错误/空文本/冻结）
**测试**：✅ 9 passed。

### 4.G2 · 官方通道 Kill-Switch 护栏覆盖 ✅
- [x] LINE `line_reply`/`line_push` 加 `_line_kill_switch_blocked` 守卫（platform=line）
- [x] Messenger `fb_send_message` 加守卫（platform=messenger）+ 透传 account_id
- [x] WhatsApp Cloud 已内建（G1）
- [x] 单测 `tests/test_official_channel_kill_switch.py`（8 项：global/platform/account 作用域 + 精确性 + check 开关）
**测试**：✅ G1+G2 共 15 passed；line/fb/webhook/admin_route/config 回归 102 passed，无回归。

**Phase G 优化笔记 / 后续**：
- **复用既有 2/3**：LINE/Messenger 官方通道早已存在，本阶段只补 WhatsApp Cloud（真空白）+ 把三端官方发送
  统一纳入 Kill-Switch（此前绕过护栏）。官方通道 = 合规放量正路，比 RPA+护栏更治本。
- **复用 `rpa_send_guard`**：守卫本是通用 `kill_switch.is_blocked` 封装，官方通道直接复用，零新代码路径。
- **账号作用域**：官方 send 守卫的 account_id 现默认 "default"（global/platform 冻结已覆盖）；
  把 LINE bot id / FB page_id 精确透传到守卫是后续细化（webhook 处理器已知该 id）。
- **后续真发依赖**：WhatsApp 企业号 + phone_number_id + 永久 token；24h 客服窗口外的**模板消息回退**待补
  （现版窗口外自由文本会被官方拒，需 template）。
- **下一步候选**：① 官方通道作为编排器 `mode=official` 的出站 worker（让 companion/autoreply 主管道也能经官方 API 发）；② Phase H 平台广度 IG/Zalo。

### 4.G3 · 官方通道接入编排器 mode=official 出站 worker ✅（G 延伸）
- [x] `src/integrations/official_api_worker.py::OfficialApiWorker`（无状态 HTTP；start/stop no-op；healthy 校验凭证；send 按 platform 分发到 line_push/fb_send/wa_send_text）
- [x] 凭证解析：账号 meta 优先 → 平台 config 块回退（line/facebook_messenger/whatsapp_cloud）
- [x] `ORCHESTRATED_MODES` 加 `"official"`；`ensure_builtin_workers` 调 `register_official_workers`（门控：official.<p>.enabled 或通道块 enabled）
- [x] config schema：`platform_login.official.{line,messenger,whatsapp}.enabled`
- [x] 单测 `tests/test_official_api_worker.py`（12 项：凭证/门控/三端 send 分发/编排注册）
**测试**：✅ 12 passed；orchestrator/account/worker 回归 224 passed，无回归。
**意义**：自此 `orch.send(platform, account_id, chat_key, text)` 主管道（companion/protocol_autoreply）
可经**官方 API** 出站——账号只需 `mode=official`，与 RPA/Baileys 按账号并存；官方发送已内建 Kill-Switch。
**后续**：send_media 官方化（图片/语音）；官方入站 webhook → emit_incoming 统一收件箱（现走各自 reply 管道）。

### 4.G4 · 官方三端入站/出站镜像进统一收件箱 ✅（G 续，闭合「官方入站绕过收件箱」裂缝）
- 背景：G1–G3 让官方通道**能发**（mode=official + 护栏），但官方 webhook 入站仍**直接喂 SkillManager 自答**，
  绕过统一收件箱 → 坐席台看不到/接管不了官方渠道对话（核心卖点「全平台统一收件箱」缺口）。
- [x] `src/integrations/shared/inbox_mirror.py::mirror_to_inbox`（纯旁路 best-effort，经 `protocol_bridge.emit_incoming`+`make_message`；sink 未注册/异常皆静默 → 零回归）
- [x] LINE webhook（`line_webhook.py`）：入站镜像 `direction=in`、回复后镜像 `out`；account_id=`line.account_id` 或 `"official"`
- [x] WhatsApp Cloud（`whatsapp_cloud.py`）：入站/出站镜像，account_id=phone_number_id
- [x] Messenger（`facebook_webhook.py`）：入站/出站镜像，account_id=page_id（回退 `"official"`）
- [x] **不触发 maybe_auto_reply**（回复仍走各 webhook 既有 SkillManager 链，避免双回复）
- [x] 单测 `tests/test_official_inbox_mirror.py`（6 项：helper payload / 无 sink 静默 / 空 chat_key 跳过 / WA·FB 端到端 in+out 镜像 / sink 抛错不破坏主流程）
**测试**：✅ 6 passed；webhook+inbox+bridge 回归 716 passed；全量 **5211 passed / 31 skipped / 0 fail**（+6）。
**优化笔记 / 后续 G4b**：本步为**可见性闭环**（官方对话进收件箱、可监 SLA、危机可被坐席看到）。真正的
**接管发送闭环**需官方账号注册进 account_registry（mode=official），坐席从收件箱「发送」即经 orch.send→官方 worker
出站（出站已内建 Kill-Switch；orchestrator.send 自带 out 镜像，与本步 webhook 自答镜像不同路径，不重复计数）。
更深一层 G4c：官方入站完整迁到 `protocol_autoreply` 管道（享 kill-switch 决策期早退 / canary / 陪伴记忆），
需先关 webhook 自答以免双回复 —— 留作后续（有回归风险，分步上）。

### 4.G4b · 官方渠道坐席接管发送闭环 ✅（G 续，闭合「坐席从收件箱发不回官方渠道」）
- 背景：G4 让官方对话**进收件箱可见**，但坐席点「发送」走 `send_via_adapters`→`orch.send`→`OfficialApiWorker.send`，
  而收件箱 chat_key 是**前缀形式** `wa:user:<num>`/`line:user:<uid>`/`fb:user:<psid>`，worker 直接拿去当收件人 → **发错人**。
- [x] `official_api_worker.py::dest_from_chat_key`：取 `rsplit(":",1)[-1]` 归一为裸标识；对「已是裸标识」入参幂等（companion 主管道直传不受影响）
- [x] `OfficialApiWorker.send` 三端均先归一 `dest` 再喂官方助手；出站镜像仍由 `orch.send` 用**原前缀 chat_key** 回写（线程分组一致，不串话）
- [x] 顺手修：`fb_send_with_window_fallback` 增 `account_id` 透传 → Messenger 官方发送的 **account 级 Kill-Switch 作用域**生效（此前固定 default）；webhook 自答路径也补传
- [x] 单测 `tests/test_official_takeover_send.py`（12 项：chat_key 归一参数化 / 三端 worker.send 用裸 dest / fb 传 account_id / 端到端 orch.send 路由+出站镜像保留前缀）
**测试**：✅ 12 passed；fb+orchestrator+official+send_routes 回归 632 passed；全量 **5223 passed / 31 skipped / 0 fail**（+12）。
**闭环条件（ops，非代码）**：官方账号需注册进 account_registry（`mode=official`，account_id 与 G4 镜像一致：
WhatsApp=phone_number_id、Messenger=page_id、LINE=line.account_id 或 "official"）→ `orch.owns` 命中 → 坐席发送即经官方 API 回去。
**后续 G4c**：官方入站完整迁 `protocol_autoreply` 管道（享 kill-switch 决策期早退/canary/陪伴记忆），需先关 webhook 自答防双回复——独立 PR、带回归基线。

### 4.G4c · 官方入站迁入 protocol_autoreply 主管道（开关默认关）✅（G 收官）
- 背景：官方入站此前各 webhook **自答**（直接 SkillManager），不享 kill-switch 决策期早退 / canary / 陪伴记忆 / 限速熔断 / 审计 / 转人工——与协议号不对齐。
- [x] `official_api_worker.py::official_pipeline_enabled(config)`：读 `config.official_pipeline.enabled`，**默认 False → 零回归**
- [x] LINE/WhatsApp/Messenger webhook：开启时 `make_message`→`maybe_auto_reply(payload)`，并 `continue`/`return` **跳过自答**（避免双回复）；回复由 hook→run_autoreply→`orch.send`→官方 worker 出站（出站镜像由 orch.send 负责）
- [x] payload 契约 = run_autoreply 所需 `platform/account_id/chat_key/text/direction=in`（persona/闸门 run_autoreply 自查 registry+cfg）
- [x] handler 签名加 `use_pipeline: bool=False`（加默认值，既有调用/测试零改动）；register 处一次性读开关下传
- [x] config schema：新增顶层 `official_pipeline.enabled`（含静默风险注释）
- [x] 单测 `tests/test_official_pipeline_gate.py`（5 项：开关默认关/开 / WA·FB pipeline 模式委托 maybe_auto_reply 不自答 / WA 默认模式自答保留）
**测试**：✅ 5 passed；官方通道全家桶 50 passed；全量 **5228 passed / 31 skipped / 0 fail**（+5）。
**优化笔记**：开关式灰度（默认走旧自答）是关键安全设计——开启需配套官方账号 `mode=official` 注册 + autoreply 全局闸门开，
否则 run_autoreply 判 disabled/无 worker 不发（官方渠道静默）。已在 config 注释 + DEVLOG 双处警示。
**Phase G 至此收官**：官方通道 = 能发（G1/G3）+ 护栏（G2）+ 收件箱可见（G4）+ 坐席接管发回（G4b）+ 入站可选走主管道（G4c）。

## 9. Phase H 进度回写（平台广度 Instagram + Zalo）

### 4.H1 · 共享 official_inbound 骨架 ✅
- [x] `src/integrations/shared/official_inbound.py`：`process_official_inbound`（镜像 in →（开关）maybe_auto_reply，返回 True=已托管跳过自答）+ `mirror_official_outbound`（自答出站镜像）
- 价值：把 G4/G4c 的「镜像→管道 or 自答」流程收敛一处，新平台复用不漏接护栏。

### 4.H2 · Instagram Messaging 适配器 ✅
- [x] `src/integrations/instagram_webhook.py`：`ig_send_text`（Graph API `/<IG_ID>/messages` + 内建 Kill-Switch platform=instagram）/ `extract_ig_messages`（object=instagram、跳 echo/非文字）/ `register_instagram_routes`（GET 验证 + POST 事件，复用 fb `verify_fb_signature`）/ `_handle_ig_message`（走共享骨架）
- [x] config `instagram` 块 + `platform_login.official.instagram`

### 4.H3 · Zalo OA 适配器 ✅
- [x] `src/integrations/zalo_webhook.py`：`zalo_send_text`（OA OpenAPI v3.0 `/message/{cs|transaction|promotion}` + Kill-Switch platform=zalo + error!=0 判失败）/ `verify_zalo_signature`（HMAC-SHA256，best-effort，确切公式以控制台为准）/ `extract_zalo_messages`（user_send_text）/ `register_zalo_routes`（POST，配 oa_secret 才验签）/ `_handle_zalo_message`（共享骨架）
- [x] config `zalo` 块 + `platform_login.official.zalo`

### 4.H4 · 编排器/注册接入 ✅
- [x] `official_api_worker`：`OFFICIAL_PLATFORMS` 加 instagram/zalo；`_creds`/`_creds_ok`/`send` 三端分发（chat_key 经 `dest_from_chat_key` 归一）；`official_enabled` 映射补 instagram→instagram、zalo→zalo
- [x] `admin.py` 注册 IG + Zalo webhook（门控 enabled）
**测试**：`tests/test_phase_h_ig_zalo.py` 12 项（骨架 2 / IG 4 / Zalo 4 / worker 分发 2）；
官方+编排+config 回归 638 passed；全量 **5240 passed / 31 skipped / 0 fail**（+12）。
**优化笔记**：IG 复用 Messenger 的 Graph API 验签/send 体系（仅端点/对象不同）；Zalo 独立但照
`whatsapp_cloud` 骨架。两者一步到位接入 G2 Kill-Switch + G4 镜像 + G4c 主管道开关，零额外胶水。
**Tier 1 至此基本完成**（合规放量 + 陪伴护城河 + 平台广度）；剩 Phase I 媒体 AI 属 Tier 1 尾。

## 10. Phase J 进度回写（SaaS / License enforce / Billing）

### J 代码实况审计（先盘后做，遵「以代码为准」）
开工前 grep 审计，发现 **License/Billing 基础设施已高度成熟**（勿重复造）：
- `src/licensing/license_manager.py`：Ed25519 离线签发/验签 + LicenseStatus（active/grace/expired/invalid/unlicensed）+ 单例
- `src/licensing/gate.py`：只读拦截 `is_write_blocked` / `feature_allowed` / `channel_allowed` / `seat_exceeded` 纯原语
- 只读强制 middleware 已接 `admin.py:159`；启动 `configure_license_manager` 已接 `main.py:269`
- `src/utils/billing.py`：自然月对账单 + 价目表 + CSV；`license_routes.py` / `unified_inbox_usage_routes.py` / `branding_routes.py` 齐
- `channel_allowed`（渠道接入向导）/ `feature_allowed`（white_label 品牌）**已 wired**

**唯一真空缺**：`seat_exceeded` 原语**定义且导出，却无人调用**——授权席位上限未真正强制。

### 4.J1 · 授权席位强制接入上线边界 ✅
- [x] `gate.py::seat_block_on_online(status, online_ids, agent_id)`：纯函数——「他人在线 + 自己」过 `seat_exceeded`；已在线坐席重复 set/heartbeat 不被踢（不 flapping）；enforce 关/seats=0 恒放行
- [x] `unified_inbox_workspace_presence_routes.py`：`POST /api/workspace/presence` status=online 时 `_seat_block` → 超额 403 `seat_limit`；`_online_agent_ids` 近 120s online 统计；任何异常放行（不误伤）
- [x] `src/licensing/__init__.py` 导出 `seat_block_on_online`
- [x] 单测 `tests/test_license_seat_enforce.py`（8 项：seat_exceeded 回归 + 上线拦截/已在线不踢/去重/enforce 关放行）
**测试**：✅ 8 passed；license+workspace+billing 回归 319 passed；全量 **5248 passed / 31 skipped / 0 fail**（+8）。
**优化笔记**：用「prospective = 他人在线数 + 1」而非「当前在线数 + 1」，天然避免把正在工作的坐席挤掉线。
**多租户 SaaS 评估**：当前单租户模型（一部署一授权）。真多租户需全表 tenant_id + 查询隔离，属大改+高回归风险，
**本阶段不做**（诚实记录，非「未完成」而是「ROI/风险权衡后留作独立大版本」）。J 的可自治闭环（enforce+billing+seat）至此完整。

## 11. Phase K 进度回写（C 端变现：月度消息配额软限）

### K 代码实况审计
- 用量看板 `unified_inbox_usage_routes.py` 已有，但 `_quota_status` **只对照席位**；
- `billing.py` 价目表有 `included_messages` 且能算超额，但**无「消息含量」软状态**（本月用量 vs 含量）。

### 4.K1 · 月度消息配额软状态 ✅
- [x] `billing.py::plan_included_messages(plan, pricing)` + `message_quota_status(used, included)`（纯函数；included=0 不限；ok/warn≥80%/over；**只提示不阻断**）
- [x] `unified_inbox_usage_routes.py`：`build_usage_summary` 增 `message_quota`（`_month_to_date_messages` 取自然月至今 `messages_total` vs 套餐含量；复用 `month_window`/`_pricing`，不引第二套统计）
- [x] `workspace_usage.html`：消息配额卡片（included>0 才显示，按 level 上色）
- [x] 单测 `tests/test_message_quota.py`（9 项：含量解析/配额分级/边界 100%=warn/接入概览端到端）
**测试**：✅ 9 passed；usage+billing+license 回归 83 passed；全量 **5257 passed / 31 skipped / 0 fail**（+9）。
**优化笔记**：刻意**软限不硬切**——消息超额只上看板提示 + 走超额计费（billing 已支持），避免月中切断付费客户；
硬限若需，应是独立 enforce 开关（与 seat 同范式）。配额口径复用 `get_usage_stats`，与 ROI/对账同源。

## 12. Phase I 进度回写（媒体 AI）

### I 代码实况审计
媒体 AI **客户端层已成熟**（非骨架）：`faceswap_client.py`（FaceFusion HTTP）/ `voice_clone_client.py`
（fish_speech 零样本克隆，LAN 健康缓存）/ `tts_pipeline.py`（lazy soft-fail，LAN 优先云兜底），
均 disabled 默认 + 失败结构化降级，等外部 GPU/语音主机。出站 `orch.send_media` 已存在。
**真空缺**：官方通道**入站非文字**（图片/语音/贴纸）只回「不支持」且**不镜像**——坐席台看不到客户发了媒体。

### 4.I1 · 官方通道入站媒体可见化 ✅
- [x] `shared/official_inbound.py`：`media_placeholder(type)`（image→[图片]/voice→[语音]…）+ `mirror_inbound_media(...)`（占位镜像，best-effort）
- [x] `inbox_mirror.mirror_to_inbox` 增 `media_type`/`media_ref` 透传 `make_message`（向后兼容）
- [x] LINE / WhatsApp / Messenger 非文字分支：先镜像占位（坐席台「[图片]」可见可接管）再回不支持；Messenger 顺带补 `account_id` 传入
- [x] 单测 `tests/test_official_inbound_media.py`（13 项：占位映射参数化 + 镜像 payload + WA 图片入站 + FB 附件入站）
**测试**：✅ 13 passed；webhook+media+inbox 回归 1294 passed；全量 **5270 passed / 31 skipped / 0 fail**（+13）。
**优化笔记**：先做**可见化**（占位，无需下载媒体二进制）——零外部依赖、即时提升坐席对媒体对话的掌控；
真二进制拉取（媒体 id+token 下载存档 + media_ref 实链）属 I1b，按需再做。IG/Zalo 因 extractor 早滤非文字，
其媒体可见化留作 I1c（小改 extractor）。faceswap/voice 真陪伴回复待 GPU 主机（客户端已就绪）。

## 13. Phase F+ 进度回写（会话级首选翻译引擎持久化）

### F+ 代码实况审计
F（多线路对照 `translate-compare`）已落地：坐席能各引擎各译一遍择优填入。**但择优是一次性的**——
`/translate`、`/send` 的出向翻译走 `EngineRouter.translate` failover（固定主引擎优先），坐席为某客户挑的
线路（如某德语客户 DeepL 读感更佳）**记不住**，每次还得重新对照。`conversations` 表有 `language` 持久化
范式（migration + `_resolve_conv_language`），但**无 `pref_engine`**；`TranslationService.translate` 无 `engine` 参数。

### 4.F+1 · 会话首选引擎持久化 ✅
- [x] `store.py`：migration `ALTER TABLE conversations ADD COLUMN pref_engine`（幂等）+ `set_conversation_pref_engine(cid, engine)`（归一小写；空串清除；会话不存在回 False）；`get_conversation` 用 `SELECT *` 自动暴露
- [x] `translation_service.py`：`translate(..., engine="")`——指定且可用 → `router.translate_with` 强制单引擎，**失败再回落** `router.translate` failover；`_cache_key` 纳入 engine（按引擎分桶，互不串味）
- [x] `unified_inbox_services.py::_resolve_conv_engine(...)`：读 `conversations.pref_engine`（读不到/空回 ""→走 failover，零回归）
- [x] `/api/unified-inbox/translate` + `/send` 出向翻译：调用方未显式传 `engine` 时回落会话偏好后传入 `svc.translate`
- [x] `POST /api/unified-inbox/conv-engine`（新端点，已入 admin 路由基线）：设置/清除会话首选引擎
- [x] `unified_inbox.html`：多线路对照择优弹窗加「记住所选线路用于本会话」勾选（默认勾）→ `_rememberConvEngine` best-effort 持久化
- [x] 单测 `tests/test_conv_pref_engine.py`（10 项：store 落库/清除/缺会话 + translate 偏好命中/无偏好主引擎/偏好失败回落/不可用回落/缓存分桶 + 解析器读出/未设空）
**测试**：✅ 10 passed；translate+inbox+admin-route 回归 75 passed；全量 **5280 passed / 31 skipped / 0 fail**（+10）。
**优化笔记**：偏好引擎失败**静默回落 failover**（绝不因坐席记的引擎临时挂了就发不出译文）；缓存按引擎分桶
（同句不同引擎译文独立缓存，切换偏好即时生效不被旧桶污染）；软偏好——清除偏好即回原 failover，零破坏。
**再优化预案（F+2，按需）**：会话列表/抬头显示当前 pref_engine 徽标 + 一键清除；引擎不可用时抬头提示「偏好引擎离线，已临时兜底」。

## 14. Phase M 审计结论（截图翻译——已成熟，无需重做）

**以代码实况为准**：M 早已端到端落地，本轮审计确认无空缺：
- `src/ai/image_translate.py`：`ImageTranslateService`（OCR→translate）+ `decode_image_to_temp`（8MB 限/MIME 白名单/隐私不持久化）+ `build_vision_ocr_fn`（VisionClient Ollama→智谱故障转移 + OCR_PROMPT 逐字提取）+ 媒体缓存（同图免重识别）+ ProviderStats 观测
- 端点 `POST /api/unified-inbox/translate-image`（vision.enabled 门禁 + backend 探测 + 临时文件清理）
- 前端「🖼 图片翻译」按钮 + 面板 + handler（OCR/译文分段展示 + 填入/复制）
- 单测 `tests/test_image_translate.py`
**结论**：M 不开发，避免重复造轮子（诚实记录）。真空白是 **L 文档翻译**，本轮转做 L。

## 15. Phase L 进度回写（文档 / 长文整篇翻译）

### L 代码实况审计
全仓**无任何文档翻译逻辑**（`.docx` 仅出现在附件发送的 `accept` 白名单，非翻译）。`/translate` 仅单段，
长文需坐席逐段复制——真空白。

### 4.L1 · 长文整篇翻译（.txt / 粘贴，零新依赖） ✅
- [x] `src/ai/document_translate.py`：`split_segments`（按行切，空行作结构标记）+ `DocumentTranslateService.translate_document(...)`——逐段复用 `TranslationService.translate`（享 L1/L2 缓存 + 术语强制 + 品牌词保护 + **F+ 会话首选引擎**），**有界并发**（Semaphore）+ 段序严格保持 + 空行原样透传 + 单段失败回退原文（整体仍 ok，best-effort）+ 上限保护（200k 字符 / 2000 段）+ 整篇探一次源语言给各段兜底
- [x] 端点 `POST /api/unified-inbox/translate-document`（已入路由基线）：target_lang 支持 auto（复用 `_resolve_conv_language`）+ engine 回落会话偏好（`_resolve_conv_engine`）
- [x] `unified_inbox.html`：「📄 文档翻译」按钮 + 面板（粘贴 textarea / 上传 .txt / 翻译整篇 / 进度统计 / 复制全文 / 下载 .txt）
- [x] 单测 `tests/test_document_translate.py`（8 项：分段保留空行 + 整篇序/空行 + 单段失败回退 + 引擎透传 + 空输入 + 字符超限 + 段数超限 + 缓存命中计数）
**测试**：✅ 8 passed；doc+image+route+conv-engine 回归 34 passed；全量 **5288 passed / 31 skipped / 0 fail**（+8）。
**优化笔记**：刻意**纯文本零新依赖**——.docx/.pdf 抽取需 python-docx/pdfminer，属 L2 按需再加（先覆盖「粘贴长文/纯文本」这一最高频场景）；
逐段复用单段 `translate` 而非新写批量管道——自动继承缓存/术语/F+ 偏好/成本统计，零重复逻辑；有界并发护住翻译后端不被大文档打爆。
**再优化预案（L2，按需）**：python-docx/pdfminer 抽取保留段落样式；超长文档分块流式返回进度；译文回填 .docx 保版式。

## 16. Phase N 进度回写（真号扫码陪聊上线收口 · preflight 红绿灯）

### N 代码实况审计
`docs/N_LINE_REAL_ACCOUNT_CHECKLIST.md` 列了真号扫码陪聊的开关一致性（§1）+ 分步验证（§2）+ 反封号验证（§4），
但**真号联调本身需真实 Telegram 号**（无法纯代码完成）。代码侧真空缺：checklist §1 的开关一致性（companion_runtime
开却漏开 orchestrator/protocol → 协议号根本拉不起来）**只在启动后由 config_check 出 WARN**——operator 扫码前看不到。
`src/utils/golive.py` 有「上线红绿灯」范式（纯函数 + 路由采集 I/O），但**不含陪聊扫码维度**。

### 4.N1 · 扫码陪聊上线前自检（preflight 红绿灯） ✅
- [x] `src/ops/companion_preflight.py::build_companion_preflight(config)`（纯函数，与 golive 同形 {ok,applicable,light,ready,checks,summary}）：
  - companion_runtime=false → applicable=False 单条 info（不拦上线）；
  - 开启后校验：Telegram 凭证非占位（fail）/ orchestrator_enabled（fail）/ protocol_enabled（fail）/ 反封号闸门（warn）/ 代理池（warn）
- [x] 端点 `GET /api/setup/companion-preflight`（管理员，已入路由基线）
- [x] `rpa_overview.html`：机群健康板新增「扫码陪聊就绪」红绿灯格（点击展开自检明细），随看板轮询独立拉取
- [x] 单测 `tests/test_companion_preflight.py`（9 项：未启用不适用 / 全绿 / 凭证缺失·占位 fail / orchestrator·protocol 关 fail / 闸门关 warn 不拦 / 无代理 warn / 空配置不适用）
**测试**：✅ 9 passed；preflight+route 回归 13 passed；全量 **5308 passed / 31 skipped / 0 fail**（+13；首跑有 1 例 `-n auto` worker 偶发崩溃，隔离重跑 + 二次全量均全绿，与本次改动无关）。
**优化笔记**：preflight 与 golive **同形不同源**——刻意不塞进 golive（golive 是「能不能开张」的通用上线，preflight 是「敢不敢拉真号陪聊」的专项），各自聚合、互不耦合；
warn（闸门/代理）**不拦上线**只提示（先跑通收发再开闸门是 checklist 推荐路径）；`applicable=False` 让未启用 N 线的部署看到「未启用」而非误报红灯。
**真号联调仍需人工**（扫码/收发/镜像 chat_key 校验 = §2 Step1-3，须真号），preflight 把「扫码前可机检的部分」前置兜住。
**再优化预案（N2，按需）**：preflight 接入 golive 总表作为子项；扫码登录成功时顺手导出 session_string 存注册表（checklist §3 优化候选 1）。

## 17. Phase N2 进度回写（session_string 导出 + preflight 接入 golive 总表）

### N2 代码实况审计
N（preflight）落地后，checklist §3「优化候选 1」仍空：扫码登录**只落文件 session**（`tg_login_*.session`），
经 DC 迁移后 A 线拉起有时不稳。A 线 `telegram_client.py` 早已支持 `session_string` in-memory 启动，
`telegram_companion_worker` 也已读 `meta.session_string` 喂入 account_cfg——**唯独登录侧不导出**，链路断在源头。
另：N 的 preflight 红绿灯独立于 golive 总表，老板看「能不能开张」时看不到陪聊维度。

### 4.N2 · session_string 导出 + golive 纳入陪聊就绪 ✅
- [x] **N2-A**：`telegram_protocol_login.TelegramQrLogin._finish` 趁连接未断 `export_session_string()` → 存 `self.session_string`（失败吞掉，回落文件 session）；`_poll` 注册时一并写入 `account_registry.meta.session_string`。打通「扫码→注册表→worker→A 线 in-memory 启动」全链（下游早已就绪，只补源头导出）
- [x] **N2-B**：`golive.build_checklist` 第 6 项——N 线启用（applicable）时纳入「扫码陪聊就绪」子项（preflight red→fail / yellow→warn / green→ok）；未启用则不出现（零破坏既有 5 项总表）
- [x] 单测 `tests/test_n2_session_string.py`（6 项：init 空 / _finish 导出 / 导出失败保持空 / golive 启用纳入 ok / orchestrator 关→companion fail 阻断 / 未启用不出现）
**测试**：✅ 6 passed；preflight+golive 回归 18 passed；全量 **5322 passed / 31 skipped / 0 fail**。
**优化笔记**：刻意**只补源头导出**——下游 worker→client 消费链早就建好（N4 核心4），N2 是「最后一厘米」接通，零重复；
session_string 导出失败**静默回落文件 session**（绝不因导出异常阻断登录成功）；golive 子项**条件纳入**（applicable 才加）保既有总表语义不变。
**安全提示**：session_string 等价完整登录凭证，存 `account_registry.meta`（本地 SQLite）；如需更强保护，可后续接 N3「meta 敏感字段加密落盘」（按需）。

## 18. Phase N3 进度回写（registry meta 敏感字段静态加密）

### N3 代码实况审计
N2 把 `session_string`（≈ 完整登录凭证，可被他人直接登录该号）**明文**写进 `account_registry.meta`
（本地 SQLite `meta_json` 列）。`cryptography>=42` 已是依赖（license Ed25519 用），但**全仓无对称加密设施**——
敏感凭证落盘零保护，属 N2 引入的安全缺口。

### 4.N3 · meta 敏感字段 Fernet 静态加密 ✅
- [x] `src/integrations/registry_crypto.py`：`encrypt_meta`/`decrypt_meta`（Fernet 对称）——只加密 `_SENSITIVE_KEYS`（session_string/two_fa_password/session_secret），密文带 `enc:v1:` 前缀；密钥来源 env `ACCOUNT_REGISTRY_KEY` → 否则 `config/registry.key`（首次自动生成 + chmod 0600，已被 `.gitignore *.key` 覆盖）
- [x] `account_registry.py`：写盘前 `_meta_json` 加密、`_row_to_dict` 读出解密——**全透明**，上层（worker/路由）零改动
- [x] **向后兼容**：无 `enc:v1:` 前缀的旧明文（N2 已写）原样读出，不破；已加密值再 encrypt 幂等不套娃
- [x] **容错**：无 cryptography/取不到密钥 → 明文回落（warn 一次，绝不阻断登录）；换钥/丢钥解不开 → 该字段**置空**（回落文件 session / 重新扫码，不喂 garbage）
- [x] 单测 `tests/test_registry_crypto.py`（8 项：往返 + 非敏感不动 + 旧明文透传 + 幂等 + 换钥置空 + 密钥文件自动生成 + 空 meta + registry 端到端密文落盘/读出透明 + 仅改状态保留密文）
**测试**：✅ 8 passed；registry+companion 回归 14 passed；全量 **5330 passed / 31 skipped / 0 fail**。
**优化笔记**：加密**透明嵌入 registry 读写两点**，上层全链（N2 导出 / worker 消费 / 路由展示）零感知零改动；
`enc:v1:` 版本前缀为未来换算法/轮钥留路；解密失败**置空而非抛错**——安全降级（最坏重新扫码）优于把密文喂给 pyrogram；
best-effort 明文回落保证「没装/没配密钥的部署」照常工作（安全是增强非前置依赖）。
**再优化预案（N3b，按需）**：密钥轮换工具（批量 decrypt→以新钥 re-encrypt）；扩展 `_SENSITIVE_KEYS` 覆盖代理密码等；env 密钥接入 KMS/密管。

## 19. Phase F+2 进度回写（会话引擎徽标 + 偏好引擎离线提示）

### F+2 代码实况审计
F+ 让坐席能为会话记住首选翻译引擎，但记住后**前端无任何可见反馈**——坐席看不到当前会话用哪条线路，
也不知道偏好引擎是否临时离线（已被 failover 兜底）。`xlate-engine` 提示位只显示「目标语的有效引擎」，
不含会话偏好维度。F+ 只有 POST 写入端点，无 GET 读取，前端取不到当前偏好。

### 4.F+2 · 引擎徽标可见化 ✅
- [x] 端点 `GET /api/unified-inbox/conv-engine`（已入路由基线，复用 `_resolve_conv_engine`）：前端切会话时取回偏好
- [x] `unified_inbox.html`：`_loadConvPrefEngine`（切会话异步取偏好）+ `_prefEngineBadge(matrix)`（引擎提示后追加「📌 <引擎>」徽标；偏好引擎对当前目标语不可用→红色「⚠」离线警示 + tip）+ 点 ✕ 一键清除偏好（`_clearConvPrefEngine` 复用 POST engine=""）
- [x] 多线路对照择优时 `_rememberConvEngine` 即时更新 `_convPrefEngine` + 刷新徽标（无需重取）
- [x] 单测 `tests/test_conv_pref_engine.py` 扩展（+3：GET 端点取回 / 缺参空 / TestClient 集成；总 12 项）
**测试**：✅ 12 passed；conv-engine+route 回归通过；全量 **5333 passed / 31 skipped / 0 fail**。
**测试踩坑**：minimal app 测端点时 `api_auth` 形参须 `Request` 类型注解，否则 FastAPI 误判为 query 参数 → 422（已修）。
**优化笔记**：徽标**复用既有 `xlate-engine` 提示位**追加，不新增 UI 区块（视觉零负担）；离线判定**复用引擎能力矩阵**（`_engineMatrixCache`，零额外请求）——
偏好引擎在 matrix.engines 标记 unavailable 或干脆不在矩阵里→判离线红警；偏好即时反映（pick 后本地置位刷新，不等服务端往返）。
**再优化预案（F+3，按需）**：会话列表项也显示偏好引擎小徽标（无需进会话即可一览）；偏好引擎离线时弹一次 toast 主动提示而非仅徽标变色。

## 20. Phase L2 进度回写（.docx 带版式文档翻译）

### L2 代码实况审计
L 只覆盖纯文本/粘贴（.txt）。竞品 DeepL 的强项是**文档翻译**（上传 docx/pdf 整篇翻译并保版式）。
审计：`python-docx 1.2.0` / `openpyxl` / `pdfminer` **本机已装但未列入 requirements**（隐性依赖）。

### 4.L2 · .docx 保版式整篇翻译 ✅
- [x] `src/ai/document_file_translate.py`：`translate_docx(data, xlate=...)`——`python-docx` 遍历正文段落 + 表格单元格（递归嵌套表），逐段复用 `TranslationService.translate`（享 F+ 引擎/术语/缓存），译文写回**段落首 run、清空余 run**（保字体/样式不割裂）；有界并发 + 单段失败保留原文 + 段数上限 5000 + `python-docx` 缺失/损坏文件**软失败**
- [x] 端点 `POST /api/unified-inbox/translate-document-file`（已入路由基线）：JSON base64 进出（≤10MB）；仅 `.docx`（其他提示用粘贴）；target_lang auto + engine 会话偏好回落；返回译后 .docx base64 + 文件名 `<name>.<lang>.docx`
- [x] `unified_inbox.html`：文档翻译面板加「📎 .docx 保版式翻译」——上传→翻译→base64 解码 Blob 自动下载 + 段落统计
- [x] `requirements.txt` / `requirements-ci.txt` 补 `python-docx>=1.1.0`（此前隐性依赖显式化）
- [x] 单测 `tests/test_document_file_translate.py`（7 项：段落+表格单元格 / 空段保留 / 单段失败留原文 / 引擎透传 / **run 加粗格式保留** / 损坏文件软失败 / 输出可重新打开；真 python-docx 构造内存文档）
**测试**：✅ 7 passed；docfile+route+doc 回归 19 passed；全量 **5345 passed / 31 skipped / 0 fail**。
**优化笔记**：复用 `TranslationService` 单段 translate（非新批量管道）——自动继承 F+/术语/缓存，与 L1 同源；
版式保真用「首 run 写入 + 余 run 清空」折中——保住段落主样式且避免 run 边界把译文切碎（完美 per-run 对齐翻译会因 run 拆分破坏语义，得不偿失）；
JSON base64 进出而非 multipart——与既有 image_b64 端点同范式、可单测、前端 Blob 下载体验不打折；隐性依赖显式化进 requirements 杜绝「换机即崩」。
**再优化预案（L2b，按需）**：.pdf 文本抽取→译→纯文本输出（pdf 不可结构化回填，只做文本）；.xlsx（openpyxl 已装）单元格翻译；超大文档进度流式。

## 20b. Phase L2b 进度回写（.xlsx 保版式 + .pdf 文本抽取翻译）

### L2b 代码实况审计
依赖审计：`openpyxl 3.1.5` + `pdfminer.high_level`（pdfminer.six）**本机已装**。设计分两类：
- `.xlsx`：可结构化回填 → 与 docx 同范式真往返（openpyxl 自动保表格/样式）。
- `.pdf`：**不可结构化回填**（pdf 是版面坐标流，非语义块）→ 只做「抽取文本→译→纯文本」诚实降级。

### 4.L2b · .xlsx 真往返 + .pdf 文本翻译 ✅
- [x] `document_file_translate.py` 扩 `SUPPORTED_EXT=(.docx,.xlsx,.pdf)` + `xlsx_available()` / `pdf_available()`
- [x] `translate_xlsx(data, xlate=...)`：openpyxl 遍历全工作表全单元格，**仅译字符串**（数字/日期/`=`公式原样保留），复用 `TranslationService`（F+/术语/缓存）；有界并发 + 单格失败留原文 + 单元格上限 20000 + 缺库/损坏**软失败**；保表格样式回填
- [x] `translate_pdf_to_text(data, xlate=...)`：pdfminer 抽取文本→复用 **L1 `DocumentTranslateService`** 逐段翻译→返回纯文本；扫描件/加密/无文本/损坏均**软失败**并给出指引（扫描件转用「图片翻译」OCR）
- [x] 端点 `POST /api/unified-inbox/translate-document-file` **按扩展名分派**（同一端点，不新增路由）：`.docx/.xlsx`→`kind:file`（base64 下载，文件名带语言）；`.pdf`→`kind:text`（纯文本 + .txt 下载）
- [x] `unified_inbox.html`：原「📎 .docx」按钮升级为「📎 文档翻译 (.docx/.xlsx/.pdf)」，`accept` 扩三类；handler 按 `kind` 分流（file 自动下载，正确 MIME 区分 docx/xlsx；text 展示只读文本框 + 下载 .txt）
- [x] `requirements*.txt` 补 `openpyxl>=3.1.0` + `pdfminer.six>=20221105`（隐性依赖显式化）
- [x] 单测扩 `tests/test_document_file_translate.py`（+6 项：xlsx 字符串单元格翻译/数字与公式不译、xlsx 引擎透传、xlsx 单格失败留原文、xlsx 损坏软失败、pdf 无文本软失败、pdf 损坏软失败；共 **13 passed**）
**测试**：✅ 13 passed；route inventory 4 passed；全量 **5368 passed / 31 skipped / 0 fail（295s）**。
**优化笔记**：
1. **单端点扩展名分派** 而非新增 3 个端点——前端一个文件选择器吃三类，路由基线零变更，鉴权/参数解析单点维护。
2. **诚实降级边界**：pdf 不强行版面回填（会产出错位垃圾文档），改纯文本 + 明确提示扫描件走 OCR——比「假装支持」更可信。
3. **本轮再优化（实施中追加）**：`openpyxl.load_workbook/save` 与 `pdfminer.extract_text` 都是**同步 CPU/IO**，原会阻塞 ASGI 事件循环（大文件拖垮全站）；改用 `asyncio.to_thread` 放线程池——翻译网络 IO 早已并发，解析/序列化也不再卡 loop。
**再优化预案（L2c，按需）**：超大文档翻译进度 SSE 流式（前端进度条）；.pptx（python-pptx）；译后文件服务端临时存储 + 短链下载（避免大 base64 往返内存翻倍）。

## 20c. Phase L2c-1 进度回写（译后文档短链下载，去 base64 内存翻倍）

### L2c-1 动机
L2/L2b 的 `.docx/.xlsx` 译后二进制走 **JSON base64** 进出：base64 膨胀 ~33%，且服务端同时持 raw bytes + base64 串 + JSON 体、客户端再 `atob`+`Uint8Array` 重建——10MB 文件实际内存翻数倍，是当前架构最实在的健壮性短板（非锦上添花）。

### 4.L2c-1 · 临时令牌存储 + GET 短链下载 ✅
- [x] `src/web/translated_file_store.py`：进程内 `TranslatedFileStore` 单例——`put(data,filename,ctype)→token`（`secrets.token_urlsafe(24)` 不可猜）、`take(token)→FileEntry`（**一次性消费**，取回即删）；TTL 600s + 条目数上限 64 + 总字节上限 256MB（双上限逐出最早到期者，防堆积 OOM）；`threading.Lock` 线程安全；重启清空（临时产物无需持久化）
- [x] `translate-document-file` 端点 `.docx/.xlsx` 分支改为：译后 bytes 存令牌存储 → 返回 `{kind:"file", download_url, filename, stats}`（**JSON 只剩几十字节**，不再 `file_b64`）
- [x] 新端点 `GET /api/unified-inbox/translated-file/{token}`（已入路由基线）：凭一次性 token 取回，`Response` 二进制直传 + `Content-Disposition: attachment; filename*=UTF-8''…`（RFC5987 兼容非 ASCII 名）；过期/不存在 → 404。鉴权复用 `api_auth`——浏览器 `<a download>` 导航携**会话 cookie** 即可过（坐席本就 session 登录）
- [x] `unified_inbox.html`：`.docx/.xlsx` 下载从「base64→atob→Blob」改为直接 `a.href=download_url` 导航（二进制直传，零 base64）；`.pdf` 纯文本仍内联（无膨胀问题，不变）
- [x] 单测 `tests/test_translated_file_store.py`（8 项：put/take 往返、一次性、未知/空 token、TTL 过期、条目数逐出、总字节逐出、count 排除过期、单例）+ `tests/test_translate_document_file_route.py`（4 项端点契约：返回 download_url 非 base64、短链往返取回有效 docx + 二次取 404、未知 token 404、不支持扩展名）
**测试**：✅ 12 passed（store 8 + route 4）；route inventory 含新 GET 端点；全量 **5383 passed / 31 skipped / 0 fail（381s）**。
**优化笔记**：
1. **一次性消费**（take 即删）而非 TTL 内多次可取——下载完立即释放内存，最省 RAM；配 10 分钟 TTL 兜底「点了链接没下成」。
2. **双上限逐出**（条目数 + 总字节）防恶意/异常堆积撑爆进程内存。
3. **会话 cookie 鉴权可用**是关键判断：`_api_auth` 接受 session，故 `<a download>` 浏览器导航天然带 cookie 过鉴权，无需把 token 放 header（那样 `<a>` 下不了）。
4. **本轮再优化（实施中追加）**：`filename*=UTF-8''` RFC5987 编码——译后文件名常含中文（`合同.zh.docx`），不编码会在部分浏览器乱码/截断。
**再优化预案（L2c-2，按需）**：超大文档进度 SSE 流式（前端进度条）；.pptx（python-pptx）；存储后端可插拔（量大时换磁盘临时目录，避免进程内存）。

## 20d. Phase L2c-2 进度回写（大文档翻译进度 SSE 流式 + 前端进度条）

### L2c-2 动机
大文档翻译耗时长（逐段翻译，几十~上千段），前端原本只有「翻译中…」黑盒等待，是体验上最被感知的短板。改为**逐段进度条**。

### 架构抉择（关键）
EventSource 只能 GET 且不能携 10MB 文件体；fire-and-forget 后台任务在 ASGI 请求结束时可能被取消、且难测。
→ 选**两步**：`POST stream=true` 上传校验后把输入载荷暂存换 `job_id` 即返回；前端用 `job_id` 开 SSE，**翻译在该 GET 长连接内执行**并逐段推进度。无孤儿任务、可被 TestClient 完整测。

### 4.L2c-2 · 进度回调 + 作业暂存 + SSE 流 ✅
- [x] 底层翻译加 `progress(done,total)` 回调：`translate_docx`/`translate_xlsx` 每段/格完成回调；`translate_pdf_to_text`→`DocumentTranslateService.translate_document` 按**非空段**计数回调（空行瞬时跳过不计）；回调异常被吞（best-effort，不影响翻译）
- [x] `src/web/document_job_store.py`：`DocumentJobStore` 单例——`create(payload)→token` / `take(token)→payload`（一次性消费）；TTL 120s + 条目上限 128 + 线程安全
- [x] `translate-document-file` 端点加 `stream=true` 分支：暂存输入换 `job_id`，返回 `{job_id, progress_url}`（非 stream 走原同步路径，**零回归**）；抽出模块级 `_do_document_translation` 统一分派（同步/流式共用）
- [x] 新端点 `GET /api/unified-inbox/translate-document-progress/{job_id}`（已入路由基线）：SSE `text/event-stream`，翻译在本 GET 内 `ensure_future` 执行，轮询进度 holder（200ms）推 `{status:running,done,total}`，结束推 `{status:done,download_url|text,stats}` 或 `{status:error,reason,message}`；鉴权复用 `api_auth`（EventSource 同源带 cookie）
- [x] `unified_inbox.html`：`_onDocxFilePick` 改流式（`stream:!!window.EventSource`，旧浏览器回退同步）；`_docxlStream` 渲染进度条 + EventSource 监听；`_docxlApplyResult` 抽出最终结果处理（file 短链下载 / text 文本展示），流式与回退共用
- [x] 单测 `tests/test_document_job_store.py`（6 项）+ `tests/test_translate_document_file_route.py` 扩 4 项 SSE（POST 返回 job_id 不含 download_url、SSE 进度→done→短链下载有效 docx、pdf 错误也走 SSE error 事件、未知 job_id error 事件）
**测试**：✅ 47 passed（job store/SSE/docfile/doctranslate/filestore/route inventory 聚焦集）；全量 **5400 passed / 31 skipped / 0 fail（253s）**。
**优化笔记**：
1. **翻译在 SSE GET 内执行**而非后台任务——避开 ASGI 请求生命周期取消孤儿任务的坑，且 TestClient 能消费完整流来测（可测性是工程债的关键）。
2. **进度按非空段计**（pdf 路径）——空行瞬时跳过不污染进度百分比，与用户感知一致。
3. **同步路径零回归**：stream 仅在 body 显式 `stream=true` 时启用，所有既有调用/测试走原路径不变；前端按 `window.EventSource` 能力自动选择。
4. **本轮再优化（实施中追加）**：进度回调用闭包 holder（`prog` dict）+ 轮询，而非给翻译函数塞 asyncio.Queue——翻译层只认简单 `progress(done,total)` 回调，与 web/SSE 解耦，底层可单测、可复用于非 web 场景。
**再优化预案（L2c-3，按需）**：.pptx（python-pptx）覆盖面；令牌存储磁盘后端可插拔（大文件不占进程内存）；SSE 断线自动重连（EventSource 自带重连，但 job 一次性 take 后重连会丢——可改 job 多次可读 + 完成态缓存）。

## 21. 收口体检（进程实跑 + 配置自检 + 路由探活）

各阶段「测试全绿」后做一轮**进程级体检**，把「测试通过」升级为「真实进程加载无碍」：

### 体检方法与结论
- **配置自检** `python main.py --check`：0 错误 / 1 警告（`voice_recognition.openai.api_key` 空——pre-existing，与本轮无关）。
- **新模块烟雾导入**：`document_file_translate` / `document_translate` / `companion_preflight` / `registry_crypto` + translate/setup 路由注册——全部 IMPORT OK。
- **进程实跑** `python main.py`：完整跑到「Mobile Bridge 已启动」全子系统初始化，**本轮各阶段触及的子系统全部就绪、零 import/attribute/traceback**：
  - `Kill-Switch 已就绪`（G1）
  - `统一收件箱持久层已挂载`（inbox.db——含 N3 `pref_engine` migration，无报错）
  - `Phase C/P56 服务已预置（翻译记忆=True, 引擎=ai, 术语=9）`（L/L2/F+ 翻译栈底座）
  - 唯二失败是 **端口占用**（18787/19190）+ Telegram session `database is locked`——均因**另有一个常驻旧实例在跑**（AGENTS.md 已警示的「常驻服务争端口」场景），非本轮代码问题。
- **常驻实例路由探活**（127.0.0.1:18787）：可靠信号取 GET——旧路由 `/api/setup/checklist` 401（在）、新路由 `/api/setup/companion-preflight` 与 `/api/unified-inbox/conv-engine` 均 **404（不在）**→ 证实**常驻实例是本轮改动前启动的旧版**，下次重启即纳入新端点（属运维动作，未强杀他人常驻服务）。
  - 排错笔记：POST 到 `/api/unified-inbox/*` 任何路径（含不存在）均返回 **403**（前置鉴权中间件早退），故 POST 的 403 **不能**作为「路由存在」证据；探活须用 GET 区分 401（在）/ 404（不在）。
**结论**：本轮 F+/M(审计)/L/N/N2/N3/F+2/L2 的代码在真实进程内加载运行无碍；全量 **5345 passed / 31 skipped / 0 fail**。生产实例重启后即生效。

### §3 主表新增
| 阶段 | 目标 | 状态 |
|---|---|---|
| **G** | 官方 API 通道（WhatsApp Cloud + 三端护栏 + 入站镜像 G4 + 接管发送 G4b + 入站主管道 G4c） | ✅ 完成（真发待企业号 token） |
| **H** | 平台广度 Instagram + Zalo（官方适配器 + 护栏/镜像/主管道复用 + 编排接入） | ✅ 完成（真发待各平台 token） |
| **J** | License enforce + Billing（基建已成熟；补 seat 强制接入） | ✅ 完成（多租户 SaaS 留作独立大版本） |
| **K** | C 端变现：月度消息配额软限 + 看板提示（软限不硬切） | ✅ 完成 |
| **I** | 媒体 AI（客户端已成熟；补官方入站媒体可见化 I1） | ✅ 完成（真模型待 GPU 主机；I1b/c 按需） |
| **F+** | 会话级首选翻译引擎持久化（多线路对照择优后记住，跨刷新/重启） | ✅ 完成 |
| **F+2** | 会话引擎徽标（📌）+ 偏好引擎离线红警（⚠）+ 一键清除（GET 端点 + 复用引擎矩阵） | ✅ 完成（列表项徽标/toast F+3 按需） |
| **M** | 截图翻译（Vision OCR→译） | ✅ 审计确认早已成熟，无需重做 |
| **L** | 文档/长文整篇翻译（.txt/粘贴，保排版，复用 F+/术语/缓存） | ✅ 完成 |
| **L2** | .docx 带版式整篇翻译（段落+表格，首run写回保样式，下载译后docx） | ✅ 完成 |
| **L2b** | .xlsx 保版式真往返（仅译字符串，护数字/公式）+ .pdf 文本抽取→译→纯文本（诚实降级）；单端点扩展名分派 + 同步解析放线程池 | ✅ 完成 |
| **L2c-1** | 译后文档短链下载（进程内令牌存储 TTL+双上限+一次性消费 + GET 二进制直传），去 JSON base64 内存翻倍 | ✅ 完成 |
| **L2c-2** | 大文档翻译进度 SSE 流式（progress 回调 + 作业暂存 + GET 内执行翻译 + 前端进度条），同步路径零回归 | ✅ 完成（.pptx/磁盘存储后端 L2c-3 按需） |
| **N** | 真号扫码陪聊上线收口：preflight 开关一致性 + 护栏就绪红绿灯 | ✅ 完成（真号联调 §2 仍需人工真号） |
| **N2** | session_string 导出（扫码→注册表→A 线 in-memory）+ preflight 接入 golive 总表 | ✅ 完成 |
| **N3** | registry meta 敏感字段 Fernet 静态加密（透明读写 + 向后兼容 + 容错降级） | ✅ 完成（轮钥工具 N3b 按需） |
| **O** | 主动关怀引擎（记忆驱动的约定/事件跟进）——把「沉默才捞」升级为「记得你说的事，到点主动关心」 | ✅ O1抽取+O2入库+O3派发+O4 Web/config/接线+O5 后台页/周期清理/立即发（默认关，见 §22） |
| **P** | 单人关系健康卡 + 流失预警榜（per-contact health）——把全域 digest 下沉到「该对谁、做什么」 | ✅ P1打分器+P2 care按contact查询+P3 API+P4 后台页/发现→话术→发送闭环（见 §23） |
| **Q** | 跨域身份桥（首步）——健康卡自动聚合 care `pending_care`（CI 反查 conversation_id，零迁移） | ✅ Q1纯函数桥+Q2 单卡/榜自动聚合（见 §24） |

## 22. 竞品差距重扫 + 立项：Phase O 主动关怀引擎（Memory-driven Proactive Care）

### 扫描方法
以代码实况为准（AGENTS.md 教训）：`dir src\*.py` 全量盘点 + grep 关键能力面（campaign/broadcast/segment/group/proactive）+ 读 reactivation/episodic 源码定边界。对标六竞品（DeepL=翻译质量/文档、yunyi/tranlico=出海聊天翻译、haiwang=多关系私域陪聊、EngageLab=全渠道营销自动化、onechat=多平台聚合收件箱）。

### 能力面对标结论（已覆盖 vs 真空白）
- **已强覆盖**：文本/文档/图片/语音翻译 + TM/术语/多引擎对照（对标 DeepL/yunyi/tranlico，文档线已超）；六平台接入（TG/LINE/Msgr/WA/IG/Zalo，对标 onechat）；统一收件箱全家桶（草稿/copilot/SLA/QA/CSAT/churn/AB/协作/工作流，超 onechat）；陪伴 AI（persona/empathy/memory/crisis/wellbeing）；私域 CRM（journey/relationship_stager/portrait/reactivation，对标 haiwang）；反封号护栏（G1/G2/G3）；License/Billing/白标。
- **刻意不追（偏离北极星）**：EngageLab 式群发营销自动化（audience 营销批量推送 + email/SMS/push 多渠道）——本品定位「**AI 情感陪伴数字员工·7×24 无人值守**」，护城河是陪伴深度而非群发触达，追之即同质化红海（§1 已定调）。
- **真空白（高价值、强化护城河、无需外部资源）**：**记忆驱动的主动关怀**。现状 `reactivation_scheduler.list_candidates` 仅按「距 journey.updated_at 超 min_silent_days」**沉默触发**；`episodic_memory` 表有 content/category/salience/tier 但**无事件时间维度**。→ AI **无法对用户提过的「时间约定/未来事件」到点主动跟进**（"你说周五面试…怎么样？"／"明天复查记得带报告"／"生日快乐"）。这正是把「客服式被动回复 / 沉默才捞」升级为「**像真的记得你、到点主动关心你**」的陪伴质变点，且翻译工具/营销平台/聚合收件箱**结构上无法复制**。

### 立项：Phase O 主动关怀引擎
**目标**：从对话中抽取**带时间的约定/事件/情绪线索**，落「到期主动关怀」队列；到点由 AI 生成**引用具体事**的个性化主动消息，复用既有 send-gate/quiet-hours/Kill-Switch/canary 护栏 + 去重，绝不发空话。

**分片（拟）**：
- **O1**：`care_commitment` 抽取层——纯函数从消息文本识别未来时间锚点（相对「明天/下周五/月底」+ 绝对日期）+ 事件主题 + 情绪极性，产出 `{due_at, topic, sentiment, source_msg}`；可单测、零外部依赖（先规则+轻量解析，AI 抽取作增强可选）。
- **O2**：`care_schedule` 持久层（新表 / 复用 episodic 旁路）——存待跟进项 + 状态机（pending→sent/skipped/expired）+ 去重（同 contact+topic 不重复）+ TTL/过期清理。
- **O3**：到期派发——接 reactivation 的 send pipeline（deferred 队列，自动享 gate/pacing/quiet_hours/kill-switch），LLM prompt 强制引用 `topic`，找不到上下文就 skip。
- **O4**：Web 可见化——后台看「今日待关怀 / 已发 / 跳过」列表 + 手动增删改 + 开关（默认 enabled:false，与新子系统约定一致）。

**护栏复用**：发送一律走 reactivation 既有 deferred 队列（已含 gate/staleness/pause/pacing/quiet_hours），不新开发送路径；O 默认关，灰度同 canary。

**测试**：每片纯函数优先单测；O3 用 stub send_callback 验证「到点→引用 topic→去重→护栏早退」；全量回归保持全绿。

**状态**：🔄 实施中（O1 完成，见下）。

### 4.O1 · care_commitment 抽取层 ✅
- [x] `src/contacts/care_commitment.py`：纯函数 `extract_commitments(text, now=)` → `List[CareCommitment{due_at,event_at,topic,sentiment,anchor_text,source_text,confidence}]`
- [x] 时间锚点解析（确定性、注入 now 可测）：绝对日期（M月D日 / MM-DD，过期滚明年）、周几（裸/这/本/下/下下周X 精确自然周语义）、英文周几（(next) friday…）、相对日（明天/后天/大后天/tomorrow）、X天后 / X周后、月底、周末/下周末
- [x] 主题词典命中（面试/复查/生日/出差…中英）→ 命中 confidence 0.85；无主题用摘要兜底 0.5
- [x] 情绪极性**复用既有 `analyze_emotion`**（valence→positive/negative/neutral），失败软降级 neutral
- [x] **只认未来**：跟进时刻（事件日 20:00，道贺类 09:00）≤ now 即丢弃
- [x] 单测 `tests/test_care_commitment.py`（16 项，固定 now=周三 2026-06-17 10:00）
**测试**：✅ 16 passed；全量 **5436 passed / 31 skipped / 0 fail（229s）**。
**优化笔记**：
1. **情绪不另造词典**——复用 `analyze_emotion`（与记忆 salience 同源），口径一致、少维护。
2. **跟进时刻而非事件时刻**：抽取产出的 `due_at` 直接是"事件当晚 20 点回访"（道贺类当日 9 点），下游 O2/O3 无需再算时机；同时保留 `event_at` 供展示。
3. **自然周语义精确化**（实施中修正）：初版"下周X"用 `(delta+7)` 在今天恰为该周几时会偏移；改为"先到下一个自然周周一再加目标周几"，下/下下周一致且无边界 bug（已被 `test_next_next_week` 覆盖）。
4. **宁缺毋滥**：仅在有明确时间锚点时产出；"今天好累"这类无未来锚点 → 空（不污染关怀队列）。
**O1 已知取舍 / 下一片改进**：裸"周X"可能在闲聊里误触（"周五见过他"是过去）——O1 不做时态判别，交由 **O2 去重 + confidence 阈值 + O3 LLM「无具体上下文就 skip」** 三道下游过滤兜底。**下一片 O2**：`care_schedule` 持久层（新表 + 状态机 pending→sent/skipped/expired + 同 contact+topic 去重 + 过期清理），并定义"从哪条入站消息喂入抽取"的接线点（companion worker / inbox ingest 旁路，默认关）。

### 4.O2 · care_schedule 持久层 ✅
- [x] `src/contacts/care_schedule.py`：`CareScheduleStore`（SQLite，镜像 `crisis_event_store` 约定：单连接 `check_same_thread=False` + 写操作 `threading.Lock` + 绝不抛）
- [x] 表 `care_schedule`（contact_key/platform/account_id/chat_key/due_at/event_at/topic/topic_norm/sentiment/source_text/confidence/status/时间戳）+ 索引（status+due_at、contact+status）
- [x] **入库即收敛 O1 的「宁滥」**：`min_confidence=0.6`（挡 O1 无主题兜底 0.5）+ **同 contact+topic_norm+due 邻近窗口（默认 3 天）去重**
- [x] `add_commitment` / `add_from_text`（便捷接线：抽取一条消息→入库，返回新增 id）
- [x] 状态机 `pending→sent|skipped|expired|cancelled`（`_set_status` 仅 `status='pending'` 可转，幂等安全）；`mark_sent` 落 sent_at
- [x] 查询 `list_due(now)` / `list_pending` / `list_recent(status=)`（按 due_at 升序）；`expire_overdue(grace_days)` 标记错过时机者；`count(status=)`
- [x] 单测 `tests/test_care_schedule.py`（12 项：往返/低分过滤/同主题去重/异主题放行/远 due 放行/按 contact 隔离去重/add_from_text/list_due/状态流转仅 pending 可转/skip+cancel/expire 逾期/expire 不误伤未到期）
**测试**：✅ O1+O2 共 28 passed；全量 **5448 passed / 31 skipped / 0 fail（229s）**。
**优化笔记**：
1. **去重在 SQL 层用 `ABS(due_at-?)<=window`**——而非拉全表内存比对，O(索引) 命中，量大也稳。
2. **状态流转加 `WHERE status='pending'` 守卫**——天然幂等：重复 mark_sent / 并发派发只有一个成功（rowcount=1），防 O3 重复发送同一关怀。
3. **置信度阈值默认 0.6 而非 0.5**：正好卡在 O1「有主题 0.85 / 无主题 0.5」之间——默认只收高质有主题项，无主题项需调用方显式降阈才入库（把"宁滥→宁缺"做成默认行为）。
4. **`:memory:` 与文件路径双模式**：测试零落盘、生产落 config 目录，构造参数统一。
**下一片 O3**：到期派发器——读 `list_due(now)` → 拉 episodic memory + topic 构造 LLM prompt（强制引用具体事，无上下文 skip+`mark_skipped`）→ 经 **reactivation 既有 deferred 队列**发送（复用 gate/pacing/quiet_hours/kill-switch）→ 成功 `mark_sent`；接线点定在 companion worker tick 或独立 loop，默认 `companion.proactive_care.enabled:false`。**改进点**：派发前再查一次 contact 近期是否已主动聊过该 topic（防"机器到点打卡"感）；quiet_hours 命中则顺延而非跳过。

### 4.O3 · care_dispatcher 到期派发器 ✅
- [x] `src/contacts/care_dispatcher.py`：`CareDispatcher`（与 reactivation_loop 同范式：可注入 store/ai/send_callback/context_provider/already_discussed + start/stop loop + run_once 可单测）
- [x] `run_once(now)`：`list_due` → 逐条 `_dispatch_one`，`max_per_tick` 限流；**不查平台身份**（直接用入库时存的 platform/account_id/chat_key）
- [x] LLM prompt 强制紧扣 `topic` 具体事（`_when_desc` 给"今天/昨天/这几天"口语化时态）；身份泄露/空回复/无上下文 → `mark_skipped`（不发空话）
- [x] 发送复用注入的 `send_callback`（reactivation 同款 deferred 队列签名）→ 自动享 gate/pacing/quiet_hours/kill-switch；`row_id>0` 才 `mark_sent`，enqueue 失败/异常**留 pending 下 tick 重试**
- [x] **O3 改进①** `already_discussed(contact_key,topic)` 复查 → 近期已聊过该事则 skip（防到点打卡）
- [x] **O3 改进②** `shift_out_of_quiet_hours` 纯函数：发送时刻命中安静窗（默认 23–8）→ 顺延到结束（隔夜/清晨/无窗三态正确），而非跳过
- [x] dry_run：只生成+log+mark_sent（note=dry_run），不真 enqueue（灰度看质量）
- [x] 单测 `tests/test_care_dispatcher.py`（14 项：quiet 四态 + 成功 mark_sent + prompt 含 topic + 无上下文/already_discussed/LLM空/身份泄露 skip + send 失败留 pending + max_per_tick 限流 + dry_run + 未到期不发 + quiet 顺延发送时刻）
**测试**：✅ 14 passed；O1+O2+O3 共 42 passed；全量 **5462 passed / 31 skipped / 0 fail（213s）**。
**优化笔记**：
1. **不做平台身份查找**（与 reactivation 不同）：care 项入库即带 platform/account_id/chat_key，派发零额外 IO、零"找不到身份"分支，更稳更省。
2. **失败留 pending 而非标 fail**：LLM 异常 / send 异常 / enqueue 被 gate 拦（row_id=0）都不消费该项，下个 tick 自然重试；真正"到点没送出"由 O2 `expire_overdue` 兜底，避免卡死或重复。
3. **mark_skipped 带 note**（no_context/already_discussed/identity_leak/llm_empty）——O4 后台可见"为什么没发"，便于调参，而非黑盒丢弃。
4. **quiet_hours 顺延（改进②）做成纯函数**，与派发解耦、独立单测三态边界；关怀"该送只是择时"，不因撞安静窗被 expire 误杀。
**下一片 O4（收尾）**：Web 可见化——后台页/接口看"今日待关怀 / 已发 / 跳过(含原因) / 过期"列表 + 手动 取消/立即发/编辑 + 总开关；config schema `companion.proactive_care.*`（默认 enabled:false）；并把抽取接线进 companion worker 入站旁路（gated）。**改进点**：O4 同时补"接线点"——让真实入站消息喂 `add_from_text`，闭合 O1→O4 全链；接线默认关、灰度同 canary。

### 4.O4 · Web 可见化 + config + 接线闭环 ✅（Phase O 收尾）
- [x] **config schema** `companion.proactive_care.*`（config.example.yaml）：`enabled/capture/min_confidence/dedup_window_days/max_per_tick/interval_sec/skip_if_no_context/quiet_start_hour/quiet_end_hour/dry_run/grace_days`，**默认全关**（与新子系统约定一致）
- [x] **单例** `get_care_schedule_store(db_path)`（care_schedule.py）：进程内复用，落 `config/care_schedule.db`
- [x] **接线点（改进✦）** `src/contacts/care_capture.py::make_care_inbound_cb`：复用 inbox 既有 `register_new_inbound_cb(cb(conv_dict,text))` 钩子——**零改 ingest.py**、gated（enabled+capture）、best-effort 不抛；contact_key 用稳定 conversation_id，O1→O2 闭环
- [x] **Web API** `src/web/routes/care_routes.py`（`/api/care/schedule*`，均过 api_auth）：`GET schedule`（列表+各状态计数 summary）/ `GET schedule/due`（到期预览）/ `POST schedule`（手动加，confidence=1 不受阈值/去重拦）/ `POST schedule/{id}/cancel`；store 经 app.state 注入，缺则按 config 目录懒建单例
- [x] admin.py 注册 + `test_admin_route_inventory.py` 基线补 4 条路由
- [x] **main.py 接线（gated）** `_maybe_start_proactive_care`：enabled 时建单例 store→挂 web_app.state→`expire_overdue` 清逾期→注册捕获 cb；messenger_rpa+ai 就绪则起 `CareDispatcher`（复用 reactivation 的 messenger deferred 发送 + `list_messages` 做 context_provider），shutdown 优雅 stop
- [x] 单测 `tests/test_care_routes.py`（9 项：手动加→列表/summary、缺字段/过去时间软失败、到期预览+取消、取消未知项、捕获 cb 默认关/开启捕获/capture=false/坏输入不抛）
**测试**：✅ 9 passed；`import main` ok；care 全栈（O1+O2+O3+O4）68 passed；全量 **5471 passed / 31 skipped / 0 fail（246s）**。
**优化笔记（O4 实施中的再优化）**：
1. **接线点改用既有 inbound 回调钩子**（原计划"改 companion worker 入站旁路"）：发现 inbox `_new_inbound_cbs` 已是带完整 {conversation_id/platform/account_id/chat_key} 的入站 chokepoint 且 best-effort 包裹——**零改 ingest.py、零 worker 耦合**，比改 worker 风险低一个数量级。O2 去重让 ingest 周期重扫天然幂等。
2. **手动加走 confidence=1.0 + min_confidence=0/dedup=0**：运营手动补的约定是可信意图，不该被 O1 防噪阈值/去重误拦；与"自动抽取宁缺"分流，各取所需。
3. **派发循环仍 messenger-only**（沿用 reactivation 现状）：care 项平台无关、dispatcher 平台无关，`_care_send` 对非 messenger 返回 0 → 留 pending，等其它平台 deferred 队列就绪即自动接入，不做提前抽象。
4. **context_provider 复用 `inbox_store.list_messages`**：无须新建上下文层；`skip_if_no_context=true` 下无消息历史 → 安全跳过（不发空话），默认关 + 灰度 dry_run 双保险。
**Phase O 全链闭合**：入站消息 →(O1 抽取)→(O2 入库去重)→(O4 捕获接线)→ 后台可见/可改 →(O3 到点派发，复用全套发送护栏)。下一步可选：O5 派发循环周期性 `expire_overdue` + 后台 HTML 页（当前仅 JSON API）+ 多平台 deferred 队列接入。

### 4.O5 · 后台 HTML 页 + 周期清理 + 立即发（Phase O 加固）✅
- [x] **派发循环周期性 `expire_overdue`**：`CareDispatcher.run_once` 每轮先清逾期 pending（新增 `expire_grace_days` 参数，best-effort 不抛），关怀错过时机自动收敛、不堆积、不补发
- [x] **「立即发」** `CareScheduleStore.bring_forward(sid)`：把 pending 的 due_at 提前到 now → 下个派发 tick 即到期处理（仍走全套发送护栏，非绕过）；`POST /api/care/schedule/{id}/send-now`
- [x] **后台 HTML 页** `care_schedule.html`（`/care-schedule`，role=care/master+admin+viewer）：状态计数 pills + 状态筛选 + 列表（联系人/平台/主题/原话/到期/状态/备注）+ 手动加一条 + 每行「立即发 / 取消」；纯 fetch 既有 JSON API，复用 base.html 风格
- [x] 导航接入 base.html（桌面+移动两处，crisis-audit 之后）+ `web_user_store` 注册 `care` 页权限 + admin.py 两处 path-map + 页面路由
- [x] 路由清单基线补 `/care-schedule GET` + `/api/care/schedule/{sid}/send-now POST`
- [x] 单测 +5：send-now 提前到期/未知项 not_pending（route）、bring_forward makes-due/仅 pending（store）、run_once 先 expire 逾期（dispatcher）
**测试**：✅ care 全栈 65 passed；`import main` ok；Jinja 模板解析 ok；全量 **5476 passed / 31 skipped / 0 fail（224s）**。
**优化笔记（O5 实施中的再优化）**：
1. **expire 放进 `run_once` 而非独立维护循环**：派发器本就周期 tick，复用其节律清逾期 → 零新增后台任务、零新增 asyncio.Task 生命周期管理，最小面。
2. **「立即发」做成 `bring_forward`（改 due_at）而非直接旁路 dispatch**：保持「所有发送都过同一 gate/pacing/quiet_hours/kill-switch 漏斗」的单一通道纪律——运营点「立即发」也只是把它排到队首，不开后门，安全可控且与 dry_run/灰度兼容。
3. **页面零新增 API**：HTML 页全部复用 O4 已测的 JSON 端点（list/summary/add/cancel + 新 send-now），页面只是壳；逻辑都在已单测的后端，降低「页面带未测逻辑」的风险面。
**Phase O 完整收尾**：抽取→入库→捕获接线→后台可见可改可立即发→到点派发（含周期清理），默认全关、灰度 dry_run、全程复用既有发送护栏。竞品（翻译工具/营销平台/聚合收件箱）结构上无法复制的「记得你说的事、到点主动关心」护城河能力面已落地闭环。

## 23. 竞品差距重扫（第二轮）+ 立项：Phase P 单人关系健康卡 + 流失预警榜

### 扫描结论（以代码实况为准，避免重造）
重扫六个对标产品（yunyi / DeepL / 海王 / tranlico / EngageLab / onechat）+ 对仓库做「关系健康数据资产盘点」（[Audit relationship-health data assets](71f12a42-fcd0-47ea-ac09-0ff37d3a0c93)）后发现：**关系健康看板的多数零件已存在**——
- `/api/relations/digest`（全域 health_score 0-100 + grade A/B/C/D + insights）、`/api/funnel/*`、`/api/relations/intimacy-trend`
- inbox 侧 `ChurnPredictor` + `/api/workspace/churn-risks`（**会话粒度**流失预警）
- `IntimacyEngine`（事件流重放出 score / 真实沉默天数 / 对称性 / 7d 活跃）、`ReactivationScheduler`

**真空白**：contacts/companion 域**没有逐联系人的健康卡**——digest 只有全盘聚合（活跃率/漏斗深度比），回答不了运营每天最需要的「**该对谁、做什么**」。这是高价值、非冗余（不与全域 digest / inbox ChurnPredictor 重复）、强护城河（建立在我们独有的关系模型上，翻译/营销/聚合类产品结构上无此数据）的缺口。

### 立项：Phase P 单人关系健康卡 + 流失预警榜
**目标**：把分散关系信号融成**逐联系人**健康卡（score+grade+risk+原因+建议动作），并排出「最该干预」的流失预警榜，复用 `IntimacyEngine` 事件重放 + 全域 digest 的 grade 带，**避免重造**沉默/亲密度/趋势。

### 4.P1 · 关系健康度打分器（纯函数）✅
- [x] `src/contacts/relationship_health.py`：`ContactHealthSignals`（入参）→ `score_contact_health` → `HealthCard`（score 0-100 + grade A/B/C/D + risk_level healthy/watch/at_risk/critical + value_at_risk + action + action_hint + reasons + components）
- [x] 4 维加权：recency 0.40（**流失第一信号**）+ intimacy 0.25 + trend 0.20 + mutuality 0.15；分段确定性（recency/trend 分档），grade 带沿用 digest（A≥80/B≥65/C≥45/D）
- [x] **value_at_risk**：高亲密(≥50)或 BONDED+ 阶段 且沉默 ≥7d → 单列标记，排序置顶
- [x] 建议动作优先级：care_pending > reactivate > schedule_care > deepen > maintain > none
- [x] 单测 `tests/test_relationship_health.py`（10 项：健康/value_at_risk/从无消息/pending优先/cooldown不重唤醒/降温deepen/单向标记/无基准中性/沉默单调/grade带）
**P1 实施中的再优化**：初版把 action 完全挂在 raw risk 带上 → 测试发现「20 天沉默的高亲密关系」被历史亲密度+对称性撑到 watch、漏掉唤醒建议。**修正**：让 `value_at_risk` 直接进入「需干预」分支（`needs_intervention = value_at_risk or risk in at_risk/critical`）——最该抢救的高价值流失不能被均值掩盖。

### 4.P2 · CareScheduleStore 按 contact 查询 ✅
- [x] `list_by_contact(contact_key, status, limit)` + `count_pending_by_contact` + **`pending_counts_by_contacts`（批量，防 N+1）**
- [x] 单测 +2（按 contact 列表/计数 + 取消后下降；批量计数 IN 查询）
**动机**：盘点指出 care_schedule 此前无按 contact 查询 API，健康卡的 `pending_care` 信号取不到。

### 4.P3 · 健康卡 + 流失预警榜 API ✅
- [x] `GET /api/relations/health/{journey_id}?contact_key=`：单人卡；可选 contact_key 关联 inbox 会话补 pending_care
- [x] `GET /api/relations/health-board?limit=&risk=&min_intimacy=&scan=`：按关系强度扫 top-N、逐个打分、**排序（value_at_risk 优先 + 健康分升序）**、可按 risk 过滤
- [x] 复用 `IntimacyEngine.compute_intimacy_from_events`（**classmethod**，now / now-7d 两快照算趋势）+ `list_events_for_journeys`（批量加载，零 N+1）；与 digest 同 `if intimacy_engine is not None` 守卫
- [x] care store 经 `app.state.care_schedule_store` best-effort 读取（缺则 pending_care=0）
- [x] 单测 `tests/test_relations_health_routes.py`（6 项：活跃健康卡/未知404/contact_key补pending/榜单value_at_risk置顶+升序/risk过滤/min_intimacy过滤）
**测试**：✅ P 全栈 18 passed（10+2+6）；contacts_routes 全绿；全量 **5494 passed / 31 skipped / 0 fail（222s）**。
**P3 实施中的再优化（在原方案上又改进）**：
1. **board 排序按「关系强度 DESC」扫描而非「last_active DESC」**：初想按最近活跃扫，但流失风险恰恰在**不活跃**的人身上——按 last_active 倒序会先捞到健康的、漏掉沉默的。改按 intimacy_score DESC 扫 top-N（最有价值的关系），让其中沉默者经 value_at_risk + 升序排序自然冒头。
2. **不强行打通跨域 contact_key**：盘点指出 care_schedule 用 inbox conversation_id、contacts 用 contact_id，是两套 ID 空间。**不**为补 pending_care 而现造一个映射层（scope creep + 误 join 风险）——board 纯用 contacts 域信号（intimacy/沉默/趋势/对称性，全部齐备），单卡端点用可选 contact_key 显式关联。诚实标注、留待未来统一身份层。
3. **复用 classmethod 而非引擎实例**：`compute_intimacy_from_events` 是 classmethod，打分逻辑零依赖引擎状态，端点用 `if intimacy_engine is not None` 仅作「contacts 子系统就绪」守卫（与 digest 一致），不为本功能新增装配。
### 4.P4 · 流失预警榜后台页 + 发现→话术→发送闭环 ✅
- [x] **后台 HTML 页** `relations_health.html`（`/relations-health`，role=care）：风险等级/最低亲密度/条数筛选 + 汇总 pills（扫描数/榜单数/高价值流失数）+ 榜单表（健康分/grade/亲密度/沉默天/风险原因/建议动作/journey/操作）；value_at_risk 行标红 + 「高价值」徽标
- [x] **操作闭环**：风险行「生成话术」→ 调既有 `POST /api/reactivation/{jid}/draft-reunion`（**不自动发**）→ 弹窗显示草稿 + 元信息（亲密度/沉默天/阶段/语言）→「复制」+「标记已发」（调既有 `mark-sent`，联动 draft_log）
- [x] 导航接入 base.html（桌面+移动，主动关怀之后）+ admin.py 页面路由（role=care）+ `_PATH_TO_ACTIVE`=relations_health（高亮）/`_PATH_TO_PAGE`=care（权限）+ 路由清单基线补 `/relations-health GET`
**测试**：✅ Jinja 模板解析 ok；`import main` ok；路由清单 4 passed；全量 **5494 passed / 31 skipped / 0 fail（223s）**。
**P4 实施中的再优化（在原方案上又改进）**：
1. **零新增后端**：页面全部复用已测端点（health-board + draft-reunion + mark-sent）——发现榜 P3 已测、话术生成/已发是 contacts_routes 既有且已测逻辑，页面只是壳，无未测后端面。
2. **draft-reunion 不自动发**（沿用其设计）：生成草稿交人工审核/复制/发送，符合 `contacts.enabled` 灰度精神（短期不开自动外呼）；「标记已发」联动 draft_log 给后续反馈闭环（draft 成功率 by_silent_band）铺底。
3. **nav 高亮 vs 权限分离**：`/relations-health` 的 active key 用 `relations_health`（独立高亮），权限 page_key 复用 `care`（同受众，免新增角色）——两套 map 各司其职，不混淆。
**Phase P 完整闭环**：digest 全域分 → **逐人健康卡 + 流失预警榜（发现该唤醒谁）** → 一键生成重逢话术 → 复制发送 + 标记。竞品无此「基于真实关系模型的逐人健康 + 可执行话术」闭环。

**下一阶段建议（新立项方向）**：
- **跨域统一身份层**：把 contacts `contact_id` ↔ inbox `conversation_id` ↔ care `contact_key` 建映射，让 pending_care / churn / qa_score 等信号在单卡上聚合（中长期基础设施，收益面广但需谨慎设计）。
- **多平台 deferred 队列**：reactivation + care 当前主动发送仅 messenger，扩到 telegram/line/whatsapp（偏工程补齐）。
- **关怀/重逢质量闭环**：dry_run 真机采样 + 人工评分回流，调 prompt/阈值（数据驱动优化已落地的 O/P 两线发送质量）。

## 24. 跨域身份盘点 + 立项：Phase Q 身份桥（健康卡自动聚合 care 信号）

### 扫描结论（以代码实况为准）
对三套 ID 空间做了映射盘点（[Audit cross-domain identity mapping](64e835d1-51db-41eb-8ea3-0681d9a47f2c)）：
- **现成**：`channel_identities(channel,account_id,external_id)→contact_id` 三键唯一索引 + `get_ci_by_external` / `list_channel_identities_of` / 批量版；`conv_id=platform:account_id:chat_key`（inbox+care 已共用）；**care `contact_key` = inbox `conversation_id`**（O4 已对齐）。
- **真 blocker（不是缺表）**：Messenger/WhatsApp 的 `channel_identities.external_id`（裸 peer 名）≠ inbox `chat_key`（带前缀）——彻底打通需改 RPA 写入做 external_id 规范化（**大、风险高、触 runner**）；以及 ingest 热路径回写 `conversation_meta.contact_id`（未接）。

### 立项决策：**不**做大而全的身份层，先做「最高价值 + 最低风险」首步
**目标**：让 Phase P 的健康卡/预警榜**自动**聚合 care 域 `pending_care`，消除 P3「需手传 contact_key」的诚实局限——纯读路径 join，**零 schema 变更、零 RPA 改动、零迁移**。external_id≠chat_key 的平台暂漏匹配（计 0、不报错），作为已知局限留待未来 external_id 规范化层。

### 4.Q1 · 身份桥纯函数 ✅
- [x] `src/contacts/identity_bridge.py::conversation_ids_for_identities(channel_identities) -> List[str]`：把 contact 的 CI 列表镜像成 `{channel}:{account_id}:{external_id}`（= `conv_id` 格式）候选 conversation_id（去重保序、account_id 缺省 default、缺字段跳过、对象/dict 兼容）。纯函数、不触 DB。
- [x] 单测 `tests/test_identity_bridge.py`（7 项，含**钉死与 `inbox.normalizer.conv_id` 格式一致**防双边漂移）

### 4.Q2 · 健康卡 / 预警榜自动聚合 ✅
- [x] `contacts_routes` 新增 `_pending_care_for_contact(contact_id, extra_keys=)`：CI 反查 conversation_id → `care_store.pending_counts_by_contacts` 求和；best-effort，care store 经 `app.state` 读、缺则 0
- [x] `GET /api/relations/health/{journey_id}`：**自动**填 pending_care（不再要求手传 contact_key；contact_key 仍可叠加补充）
- [x] `GET /api/relations/health-board`：SELECT 加 `contact_id` → 批量 `list_channel_identities_for_contacts` → 批量 `pending_counts_by_contacts`，**零 N+1** 地给每行填 pending_care → 之前永不触发的 `care_pending` 建议现在能正确亮起
- [x] 单测 +2：单卡不传 contact_key 自动聚合到「已排 2 条」；预警榜沉默强关系自动出 `care_pending`
**测试**：✅ Q 全栈 9 passed（7+2）；`import main` ok；全量 **5503 passed / 31 skipped / 0 fail（252s）**。
**Q 实施中的再优化（在原方案上又改进）**：
1. **果断收敛 scope**：盘点本指向「统一身份层」，但发现真 blocker 是 RPA external_id 规范化（大改、触 runner、易回归）。**改为只做读路径 join 的首步**——用既有 CI + conv_id 反查，零迁移拿到 80% 价值（LINE/TG/web 命中），把高风险的 external_id 规范化诚实留到单独立项。符合「以代码为准、不重造、控风险」铁律。
2. **桥做成纯函数 + 钉死格式一致性**：`conversation_ids_for_identities` 不依赖 store，单测覆盖；并加一条断言钉死它与 `inbox.normalizer.conv_id` 输出逐字一致——防 contacts 侧镜像格式与 inbox 权威源未来漂移（不引入 contacts→inbox 反向 import）。
3. **单卡 contact_key 从「唯一来源」降级为「可叠加补充」**：自动 CI 反查为主，手传 key 仍生效并去重叠加（`dict.fromkeys`）——向后兼容 P3 既有调用方，同时默认就对。
4. **榜单批量两段查询**：先批量 CI（`list_channel_identities_for_contacts`）再批量 care 计数（`pending_counts_by_contacts`），全程 2 次 SQL，避免逐 journey N+1；与 digest/board 既有「批量重放」性能纪律一致。
**已知局限（已在代码 docstring 标注）**：Messenger/WhatsApp 因 external_id≠chat_key 暂漏匹配 → 这类 pending_care 计 0。彻底打通＝下一步「external_id 规范化层 / ingest 回写 contact_id」单独立项。

**下一阶段建议**：
- **Q 延伸·external_id 规范化 + ingest 回写 contact_id**：让 Messenger/WA 也命中，并把运行时 join 持久化进 `conversations.contact_id`——打通后 churn/qa_score 也能同样聚合进健康卡（中期基础设施，需谨慎灰度）。
- **多平台 deferred 队列**：reactivation+care 主动发送扩到 telegram/line/whatsapp。
- **质量闭环**：O/P 发送 dry_run 真机采样 + 人工评分回流调参。

## 25. Phase R · 健康卡跨域富集（inbox 语境并入单卡）

### 立项重扫（以代码实况为准）
重扫 Q 延伸的两条路（external_id 规范化 / ingest 回写 contact_id）改动面：两者都触 **RPA 写入或 ingest 热路径**，风险高、收益要等下游消费方才可见。重扫 inbox 侧发现 `get_conv_meta(conversation_id)` 已在 ingest（I1 `quick_analyze`）**可靠填充** `last_intent/last_emotion/emotion_history(→emotion_trend)/last_risk/msg_count/summary/churn_risk`——即「真数据」就在那。
→ **决策**：先不碰 Q 延伸的高风险面，做「最高价值 + 最低风险 + 立即可见」的 **Phase R**——复用 Q 已建的 `conversation_ids_for_identities` 反推，把 inbox 跨域语境（情绪趋势/意图/流失风险/最近文本/摘要）并进**单人健康卡**（深看视图），**纯读路径、best-effort、零写回/零 RPA/零 ingest 改动**。这同时把 Q 的身份桥价值「吃满」（不止 care，再加 inbox 信号）。

### 实施
- [x] `contacts_routes` 重构 Q 的聚合 helper：拆出 `_conv_ids_for_journey(j, extra_keys=)`（CI 反推候选 conv_id，去重保序）+ `_care_pending_for_keys(keys)`（替代旧 `_pending_care_for_contact`），让 conv_ids **算一次、care/inbox 共用**。
- [x] 新增 `_inbox_enrichment(conv_ids)`：遍历候选 conv_id 取 `get_conversation`，挑 **last_ts 最新**那条为主，叠加 `get_conv_meta` → 返回紧凑块 `{conversation_id, conversations_matched, last_ts, unread, last_text, emotion_trend, last_emotion, last_intent, last_risk, msg_count, summary, churn_risk}`；inbox_store 经 `app.state` 读、缺/无匹配 → `None`（不报错）。
- [x] `GET /api/relations/health/{journey_id}` 响应加 `"inbox"` 块（榜单保持精简、不做 N+1 meta 查，单卡才富集——card=深看 / board=排序，scope 边界清晰）。
- [x] 前端 `relations_health.html`：打开「生成话术」弹窗时并发拉单卡，弹窗内多一行 `📥 情绪恶化↑ · 流失风险:… · 意图:… · 摘要:…`，发送前一眼看到 inbox 侧语境（附加 JS、低风险）。
- [x] 单测 +2：`test_health_card_inbox_enrichment`（造会话+两次 update 出 emotion_trend/last_intent，断言单卡带回正确 inbox 块）、`test_health_card_inbox_none_when_no_match`（无匹配会话 → `inbox=None` 不报错）。fixture 加 `InboxStore(":memory:")` 到 `app.state`。

**测试**：✅ `test_relations_health_routes.py` 10 passed；`import main` ok；全量 **5505 passed / 31 skipped / 0 fail（217s）**。

**R 实施中的再优化（在原方案上又改进）**：
1. **从「Q 延伸」转向「R 富集」的 scope 重判**：原计划下一步是 Q 延伸（external_id 规范化/ingest 回写），但重扫发现它**触 RPA/ingest 热路径且收益要等下游消费方**。改做 R——同样复用 Q 的身份桥，却走纯读路径拿到「单卡跨域语境」这个**立即可见**的价值，把高风险面继续往后压。坚持「先低风险高价值、风险面单独立项」。
2. **helper 重构消除重复算 conv_ids**：原 Q 的 `_pending_care_for_contact` 内部自算 conv_ids；R 需要同一批 conv_ids 做 inbox 查。拆成 `_conv_ids_for_journey` + `_care_pending_for_keys`，单卡里 conv_ids 只算一次、care/inbox 共用——比「两个 helper 各自反推 CI」省一半 CI 查询。
3. **挑「最近活跃」会话为主**：一个 contact 可能多会话（多平台/多 peer），不堆所有 meta，而是按 `last_ts` 选最新那条富集——对运营「这人现在什么状态」最相关，且查询有界。
4. **字段截断防爆**：`last_text[:80]`、`summary[:160]`，避免长文本撑爆响应/弹窗。

**已知局限**：与 Q 同源——Messenger/WA 因 `external_id≠chat_key` 反推漏匹配时，inbox 富集同样为 `None`（不报错）。彻底打通仍待 external_id 规范化层。

**下一阶段建议（R 完成后已实施 → 见 §26）**：
- ~~R 上榜（轻量）~~ ✅
- **Q 延伸·external_id 规范化 + ingest 回写 contact_id**（仍是中期基础设施，打通 Messenger/WA 命中率，需谨慎灰度）。
- **多平台 deferred 队列** / **O·P 质量闭环（dry_run 采样+人工评分回流）**。

## 26. Phase R2 · 预警榜 inbox 列（情绪/流失轻量上榜）

### 实施
- [x] `InboxStore` 新增 `get_conversations_for_ids` / `get_conv_meta_for_ids`（IN 查询 + `_row_to_conv_meta` 复用 emotion_trend 计算）；`get_conv_meta` 改走 `_row_to_conv_meta` 去重。
- [x] 新模块 `src/contacts/inbox_enrichment.py`：纯函数 `pick_primary_conversation` / `build_inbox_block` / `inbox_enrichment_for_conv_ids` / `inbox_enrichment_batch_for_journeys`——单卡（full）与榜单（compact）共用，避免 routes 重复。
- [x] `health-board`：`convkeys_by_contact` **从 care 专用块提升为 Q/R2 共用**（CI 反查只算一次）；排序截断 `top = items[:lim]` 后**仅对上榜行**批量富集 inbox（2 次 SQL），响应每行加 compact `"inbox": {emotion_trend, churn_risk, last_intent}`。
- [x] 单卡 `_inbox_enrichment` 改走同一套 helper + 批量 API（meta 只查命中会话，不再逐 cid 循环 get）。
- [x] 前端 `relations_health.html`：表头加「情绪」「流失」两列（`badge-trend` / `badge-churn`）；流失格 title 悬停显示 `last_intent`。
- [x] 单测：`tests/test_inbox_enrichment.py`（5 项纯函数+批量 store）+ `test_health_board_inbox_columns`（榜单 compact inbox 断言）。

**测试**：✅ R2 栈 16 passed（inbox_enrichment + relations_health_routes）；`import main` ok；全量 **5525 passed / 31 skipped / 0 fail（233s）**。

**R2 实施中的再优化（在原方案上又改进）**：
1. **「只 enrich 上榜行」而非全 scan**：原建议是对 scan 内所有 journey 批量 meta。实施时发现 scan 可达 400、limit 仅 30——改为排序截断后再批量查 inbox，SQL 规模随 **limit** 而非 **scan** 增长，scan 大时省一个数量级查询。
2. **抽 `inbox_enrichment` 模块 + store 批量 API**：单卡与榜单共用 pick/build 逻辑；单卡也从逐 cid `get_conversation/get_conv_meta` 改为 2 次 IN 查询——为榜单铺路的同时让单卡路径更一致。
3. **compact vs full 分块**：榜单只回 4 字段（conversation_id + 三列展示字段），单卡仍回全字段——响应体控大小，card=深看 / board=列表 边界保持。
4. **CI 反查提升为 care+inbox 共用前置步**：health-board 里 `convkeys_by_contact` 不再嵌在 care store 条件内，inbox 无 care store 时也能富集。

**已知局限**：与 Q/R 同源——Messenger/WA 漏匹配时榜单 inbox 列为「—」。

**下一阶段建议（R3 已实施 → 见 §27）**：
- ~~R3·排序加权~~ ✅
- **Q 延伸·external_id 规范化 + ingest 回写 contact_id**（中期基础设施，打通 Messenger/WA）。
- **多平台 deferred 队列** / **O·P 质量闭环**。

## 27. Phase R3 · 预警榜 inbox 次级排序（tie-break，默认关）

### 实施
- [x] `inbox_enrichment.py` 新增 `parse_churn_level`（解析 JSON/裸字符串 churn_risk）、`inbox_sort_tiebreak_key`、`health_board_sort_key`；`build_inbox_block` 统一输出规范化 level（修复生产 JSON churn 在 UI 显示整段 JSON 的隐患）。
- [x] `config.example.yaml` → `companion.relations_health.health_board.inbox_sort_tiebreak: false`（**默认关**，不替代关系健康主排序）。
- [x] `health-board` 双路径：**关**时保持 R2「sort → slice → enrich top」；**开**时先对全部候选批量 enrich 再 sort（仍 2 次 SQL），排序键 `(value_at_risk, score, churn_prio, emotion_prio)`。
- [x] API 响应加 `inbox_sort_tiebreak: bool`；前端状态行随 flag 切换文案。
- [x] 单测 +5（纯函数 3 + 路由 tie-break/disabled 2）。

**测试**：✅ R 栈 21 passed；全量 **5530 passed / 31 skipped / 0 fail（186s）**。

**R3 实施中的再优化（在原方案上又改进）**：
1. **双路径 enrich 策略**：tie-break 关 → R2 高效路径（enrich 仅 limit 行）；开 → 先 enrich 全部 risk-filter 后候选再 sort——避免为默认路径牺牲 R2 性能，又为 R3 保证排序所需信号完整。
2. **顺带修 churn_risk 解析**：生产存 JSON `{"level":"high",...}`，原 compact 块直接 str() 会在 UI 露 JSON；`parse_churn_level` 在 build 阶段规范化，榜单/弹窗均受益。
3. **纯函数排序键可单测**：tie-break 权重 `(high=0, medium=1, low=2)` × `(rising=0, stable=1, falling=2)` 钉死在单测，不污染 `score_contact_health` 主模型。

**已知局限**：tie-break 仅在 **同 value_at_risk 且同 score** 时生效；Messenger/WA 漏匹配仍无 inbox 信号参与排序。

**下一阶段建议（Q 延伸安全子集 → 见 §28）**：
- ~~Q 延伸·ingest 回写 contact_id（安全子集）~~ ✅
- **弹窗 inbox 去重请求**：榜单行已有 compact inbox 时弹窗先展示、后台再拉全量单卡（小 UX 优化）。
- **Q 延伸·external_id 规范化（RPA 写入对齐）** / **多平台 deferred 队列** / **O·P 质量闭环**。

## 28. Phase Q 延伸 · ingest 回写 contact_id（安全子集，默认关）

### 立项重扫
Q 延伸完整方案含 external_id 规范化（触 RPA）+ ingest 回写。重扫后先做**安全子集**：仅 ingest 热路径回写 `conversations.contact_id` + `conversation_meta.contact_id`，**零 RPA 改动**；读侧用 `chat_key` 后缀候选（`messenger_rpa:Bob` → `Bob`）扩 CI 命中，对齐 `unified_inbox_context` 既有回落逻辑。

### 实施
- [x] `identity_bridge.py`：`external_id_lookup_candidates` + `resolve_contact_id`（三键查表 + channel-only 回落）。
- [x] `InboxStore.register_contact_resolver` + `list_conversation_ids_for_contact`（ingest 写 / 读侧反查）。
- [x] `ingest.py`：`_apply_contact_id` → `ingest_batch` 写 conversations + `update_conv_meta(contact_id=…)`。
- [x] `contacts_routes`：`_conv_ids_for_journey` / health-board 批量路径并入 inbox 反查（补 CI 桥漏匹配，如 prefixed chat_key）。
- [x] `main.py::_maybe_wire_ingest_contact_writeback` + config `companion.relations_health.ingest_contact_id_writeback: false`。
- [x] 单测 +4：前缀 alias 反查、ingest 双表回写、health-board inbox contact_id fallback。

**测试**：✅ Q 延伸栈 28 passed（identity_bridge + inbox_ingest + relations_health_routes 子集）；全量 **5534 passed / 31 skipped / 0 fail（236s）**。

**Q 延伸实施中的再优化（在原方案上又改进）**：
1. **读侧后缀候选 vs RPA 规范化**：不碰 runner 写入，用 `chat_key.split(':',1)[-1]` 作 CI 查表 alias——Messenger/WA 常见 prefixed key 立刻多一层命中，风险远低于改 RPA external_id。
2. **双表持久化一次解析**：同一次 `_apply_contact_id` 结果写入 `conversations`（ingest_batch）与 `conversation_meta`（update_conv_meta）——health-board / 单卡共用，无需二次 join。
3. **读路径闭环**：ingest 回写后 `list_conversation_ids_for_contact` 并入 health-board 批量 conv_id 并集——即使 CI 镜像 conv_id 与 inbox 真实 conv_id 不一致（前缀问题），care/inbox 富集也能命中。

**已知局限**：后缀候选不能覆盖所有 WA/Messenger 命名差异；彻底打通仍要 RPA external_id 规范化（单独立项）。

**开启方式**：
```yaml
companion:
  relations_health:
    ingest_contact_id_writeback: true
```

**下一阶段建议（存量回填 → 见 §29）**：
- ~~存量回填 job~~ ✅
- **Q 延伸·external_id 规范化（RPA 写入对齐）**：runner on_peer_seen 与 inbox chat_key 统一命名。
- **弹窗 inbox 去重** / **多平台 deferred 队列** / **O·P 质量闭环**。

## 29. Phase Q 延伸·存量回填 · 历史会话补 contact_id（默认关，可 dry_run）

### 立项确认
先 grep 确认无现成 contact_id 回填（仅有 episodic memory 的 `backfill_*`，无关）。§28 的 ingest 回写**只对新进消息生效**，存量会话仍 `contact_id=''`——故补一个离线回填 job。

### 实施
- [x] `InboxStore.list_conversations_missing_contact_id(limit, platform)` + `set_conversation_contact_id(cid, contact_id)`（写 conversations + 仅在 conv_meta 行已存在且为空时回填，不凭空建 meta 行）。
- [x] 新模块 `src/contacts/contact_backfill.py`：`backfill_contact_ids(inbox_store, resolver, *, limit, platform, dry_run)` → `BackfillResult{scanned, resolved, written, dry_run, hit_rate, samples}`；best-effort、单条失败不影响其余。
- [x] `main.py`：抽 `_build_contact_resolver()`（ingest 回写 + 回填共用）；新增 `_maybe_run_contact_id_backfill()` 一次性启动任务（DB 扫描走 `asyncio.to_thread` 不阻塞事件循环）。
- [x] config `companion.relations_health.contact_id_backfill.{enabled, limit, dry_run, delay_seconds}`（默认关）。
- [x] 单测 `tests/test_contact_backfill.py`（5 项：查缺失 / 双表写回 / 回填写+报告 / dry_run 不写库 / None resolver 安全）。

**测试**：✅ backfill 栈 5 passed；`import main` ok；全量 **5549 passed / 31 skipped / 0 fail（273s）**。

**存量回填实施中的再优化（在原方案上又改进）**：
1. **resolver 抽取共用**：原 §28 resolver 闭包嵌在 writeback 方法内；本阶段抽 `_build_contact_resolver()`，ingest 回写与回填**同一份解析逻辑**，避免双实现漂移。
2. **dry_run + hit_rate 先评估再写**：回填前可只跑解析、log 命中率（`resolved/scanned`）与样本——上线前先量化「后缀候选能救回多少存量」，决定是否值得全量写。
3. **conv_meta 回填保守**：`set_conversation_contact_id` 只在 conv_meta 行**已存在且 contact_id 空**时更新，不凭空建 meta 行（meta 由 ingest 智能分析负责，回填不越界造数据）。
4. **DB 扫描入线程池**：`asyncio.to_thread` 跑回填，避免大 limit 扫描阻塞事件循环（与 episodic backfill 的异步纪律一致）。

**已知局限**：回填命中率上限＝后缀候选规则覆盖面（与 §28 同源）；彻底打通仍待 RPA external_id 规范化。

**开启方式**：
```yaml
companion:
  relations_health:
    contact_id_backfill:
      enabled: true
      dry_run: true   # 先评估命中率，再改 false 真写
```

**下一阶段建议（回填可视化 → 见 §30）**：
- ~~回填结果暴露到 admin~~ ✅
- **Q 延伸·external_id 规范化（RPA 写入对齐）**：根治命名差异（中期，触 runner）。
- **弹窗 inbox 去重** / **多平台 deferred 队列** / **O·P 质量闭环**。

## 30. Phase Q 延伸·回填可视化 + 按需 dry_run 触发（运营闭环）

### 立项确认
grep `backfill-status`/`backfill_result` 确认无现成实现（仅 §29 DEVLOG 作为建议）。§29 回填命中率只在 log，运营看不到。

### 实施中的方案升级（比原建议更进一步）
原建议只是「被动展示启动回填结果」。深入思考后改为**被动展示 + 主动 dry_run 触发**：运营可随时点「评估(dry-run)」预演命中率、再「执行回填」——比只看一次启动日志有用得多，且 dry_run-first 默认安全。

### 实施
- [x] `GET /api/relations/backfill-status`：读 `app.state.last_contact_backfill`（启动自动跑 or 手动触发的最近一次），未跑 → `{status:"not_run"}`。
- [x] `POST /api/relations/backfill-run?dry_run=&limit=&platform=`：**默认 dry_run=true**（只评估不写库），显式 false 才真写；结果缓存回 app.state。端点在 register 函数顶层注册（不依赖 intimacy_engine），复用 contacts_store 闭包 + `app.state.inbox_store`，DB 扫描走 `run_in_threadpool`。
- [x] `main.py` 启动回填把结果（带 `trigger:"startup"` + ts）写 `app.state.last_contact_backfill`，供 status 端点统一读。
- [x] 前端 `relations_health.html`：可折叠「跨域归档诊断」面板——显示最近回填扫描/命中率/写回数 + 扫描上限输入 + 「评估(dry-run)」「执行回填」按钮（真写前 confirm）。
- [x] 单测 +4：status 未跑 / dry_run 评估不写库且回填 status / 真写命中 / （store+orchestrator 仍走 §29 的 5 项）。

**测试**：✅ 回填栈 22 passed（routes + contact_backfill）；`import main` ok；全量 **5555 passed / 31 skipped / 0 fail（250s）**。

**回填可视化实施中的再优化（在原方案上又改进）**：
1. **被动展示 → 主动 dry_run 触发**：核心升级，把「事后看日志」变「随时可预演+按需执行」，运营无需改 config 重启即可评估。
2. **dry_run-first 安全默认**：`POST` 端点 `dry_run` 默认 true，前端真写按钮额外 confirm——避免误触大规模写库。
3. **status 单一数据源**：启动任务与手动触发都写同一 `app.state.last_contact_backfill`（带 trigger 区分），status 端点只读一处，前端展示「启动自动/手动」来源。
4. **端点解耦 intimacy_engine**：放 register 顶层而非 health-board 块内——即使未接 intimacy_engine（如纯收件箱部署）回填诊断仍可用。

**已知局限**：`app.state` 缓存重启即失（但回填本就是启动/按需跑，重启后再跑即刷新，可接受）；命中率上限仍＝后缀候选覆盖面。

**下一阶段建议（前向后缀匹配 → 见 §31，替代了高风险的 RPA 规范化）**：
- ~~Q 延伸·external_id 规范化（RPA 写入对齐）~~ → **改为零 RPA 的读路径后缀匹配（§31）**
- **弹窗 inbox 去重**（小 UX）/ **多平台 deferred 队列** / **O·P 质量闭环**。

## 31. Phase Q 延伸·前向后缀匹配（读 inbox 真实 chat_key，替代 RPA 规范化）

### 立项重扫与方案决策（关键）
原计划下一步是 **RPA external_id 规范化**（让 runner 写 `external_id` 与 inbox `chat_key` 同源）。重扫实况：
- Messenger runner 写 `external_id = peer_name`（裸名 "Bob"，见 `runner.py` on_message/maybe_before_reply）；
- inbox `chat_key` 经 approval 行可能带前缀（`messenger_rpa:Bob` / `acc_<id>:Bob`，见 `messenger_rpa_routes._account_id_from_chat_key`）。

→ **决定不改 RPA**：(a) 触 runner、改既有 CI 数据语义、要迁移，**高风险**（AGENTS.md 红线）；(b) §28-30 的 writeback+回填已用「inbox→CI 后缀剥离」覆盖大半。剩下唯一缺口是**前向**（CI external_id → 真实 conv_id）。改用**零 RPA 的读路径后缀匹配**：读 inbox **真实 chat_key**，匹配 `chat_key==external_id` 或 `chat_key endswith ':external_id'`——用真实数据、不猜前缀、不依赖 writeback flag。

### 实施
- [x] `InboxStore.find_conversation_ids_by_external(platform, account_id, external_id)`：platform+account_id 走 `idx_conv_platform` 收窄 + `chat_key=? OR chat_key LIKE '%:ext' ESCAPE '\'`（转义 `_`/`%`/`\` 防 peer 名含通配符误匹配），按 last_ts 优先。
- [x] `_conv_ids_for_journey` 升级为**三路合并**（去重保序）：①前向镜像 `conversation_ids_for_identities` ②writeback 反查 `list_conversation_ids_for_contact` ③前向后缀匹配（新）——单卡聚合 care/inbox 对 Messenger/WA 现在**不开 writeback 也命中**。
- [x] scope 收敛：**只增强单卡**（每联系人 CI 数少，无 N+1）；榜单仍走 writeback 路径（规模场景正路，避免对全 scan 做后缀 LIKE）。
- [x] 单测 +3：后缀命中带前缀真实 conv / 通配符转义 / 单卡不开 writeback 经后缀匹配聚合 care+inbox。

**测试**：✅ Q 延伸前向栈 39 passed；`import main` ok；全量 **5562 passed / 31 skipped / 0 fail（207s）**。

**前向后缀匹配实施中的再优化（在原方案上又改进）**：
1. **果断否决高风险 RPA 改动**：原 roadmap 写「external_id 规范化」，重扫后判定高风险且已被 writeback 覆盖大半 → 改零 RPA 读路径，用真实 chat_key 后缀匹配，价值等价、风险趋零。坚持 AGENTS.md「不轻易触 runner」。
2. **读真实数据 vs 猜前缀**：没有在桥里硬编码 `messenger_rpa:`/`acc_<id>:` 等前缀（脆弱、会漂移），而是查 inbox 真实 chat_key 做后缀匹配——前缀约定变了也自动跟得上。
3. **LIKE 通配符转义**：peer 名可能含 `_`/`%`，用 `ESCAPE '\'` + 手动转义防误匹配（如 `a_b` 不误中 `axb`）。
4. **scope 单卡收敛**：榜单不引入逐 CI 的 LIKE 查询（N+1/全表风险），保留 writeback 作规模正路——card=精准深看 / board=规模排序 的边界再次落实。

**已知局限**：榜单（非单卡）对未 writeback 的 Messenger/WA 仍可能漏聚合——规模场景应开 writeback/回填。后缀匹配的 `%:ext` 理论上可能匹配到同账号下另一前缀但同尾名的会话（同账号同 peer 名概率低，且按 last_ts 优先）。

**下一阶段建议**：
- **榜单后缀匹配批量版（可选）**：若要榜单也不依赖 writeback，加 `find_conversation_ids_for_externals(items)` 单查询 + 内存后缀索引（按 platform 收窄），控 N+1。
- **弹窗 inbox 去重**（小 UX）/ **多平台 deferred 队列** / **O·P 质量闭环**。

## 32. Phase O 质量闭环·care 派发对齐 reactivation（dislike 防重 + dry_run 样本审核）

### 立项重扫与方案决策（关键）
原 roadmap 候选「多平台 deferred 队列」与「O·P 质量闭环」。重扫实况：
- **多平台 deferred = 高风险大改**：`main.py::_care_send`/`_reactivation_send` 都 `if channel!="messenger": return 0`；messenger deferred 队列提供 gate/staleness/pause/pacing/quiet_hours/kill-switch/daily_cap **整套护栏**。要安全复刻到 LINE/WA/TG 等于在每个平台重建发送护栏栈——触发送路径、风险高、工作量大。**暂缓**。
- **O 质量闭环 = 现成缺口、低风险**：`reactivation_loop` 已有①dry_run 样本落 metrics（dashboard 审核 + like/dislike→黑名单）②`is_similar_to_disliked` 防重生成；而 **care_dispatcher 两者皆无**（只 dry_run + log）。补 care 到同等闭环：dispatch 路径插桩 + metrics + 只读端点，**不触 send 路径**。

→ **决定先做 O 质量闭环**（价值即时、风险低、与既有基建天然复用），多平台 deferred 留作后续大阶段。

### 实施
- [x] `MetricsStore`：`_care_dry_samples` deque(200) + `record_care_dry_run(sample)` + `care_dry_samples(limit, before_ts)`（与 reactivation 同范式，支持增量加载）。
- [x] `CareDispatcher._avoid_disliked(prompt, reply, sid)`：reply 命中 **共享** dislike 黑名单 → 重生成一次（带 anti-similar hint）；重生成仍相似/泄露身份 → `mark_skipped("disliked_similarity")`。dry_run 分支落 `record_care_dry_run` 样本。
- [x] `care_routes`：`GET /api/care/dry-run-samples`（运营审核）+ `POST /api/care/dry-run-feedback`（dislike→共享黑名单）。
- [x] `dashboard.html`：新增「待审核关怀话术」面板（与 reactivation 面板并列，增量加载 + 👍/👎 反馈）。
- [x] 单测 +6：care 重生成成功发新版 / 两次相似则跳过 / dry_run 落样本；端点空→有样本 / dislike 进共享黑名单 / 非法 verdict。route inventory 白名单 +2。

**测试**：✅ care 全栈 64 passed；route inventory 4 passed；全量 **5585 passed / 31 skipped / 0 fail（200s）**。

**实施中的再优化（在原方案上又改进）**：
1. **果断暂缓高风险多平台 deferred**：识别其本质是「在每平台重建护栏栈」，触发送路径，超出单阶段安全边界 → 转做低风险高价值的质量闭环，先把已有单平台质量护栏补全。
2. **dislike 黑名单共享而非各建一份**：被运营标记不合适的话术风格，在 care 与 reactivation 里同样该规避——复用 `is_similar_to_disliked`/`add_disliked_reply` 同一黑名单，一处反馈两处生效，且零新存储。
3. **重生成版二次校验**：重生成 reply 不仅再查相似度，也复跑身份泄露 check（避免「换说法又出戏」），仍不合格才 skip。
4. **质量护栏不阻断派发的容错**：metrics 不可用时 `_avoid_disliked` 直接放行原 reply（return reply）而非抛错——质量闭环是增益不是单点故障源。

**已知局限**：黑名单为 in-memory（重启清空，与 reactivation 一致，符合「灰度期人工反馈」定位）；care 样本仅 dry_run 模式产出（真发不留样本，避免与 inbox 重复存储）。

**下一阶段建议**：
- **多平台 deferred 队列**（大阶段）：抽象一个 `DeferredQueue` 协议，让 LINE/WA/TG runner 各实现 enqueue+drain，统一复用 gate/pacing/quiet/kill-switch——是实打实业务扩面，但需专门一轮 + 灰度。
- **O·P 联动质量看板**：把 care/reactivation 的 like/dislike 反馈率、skip 原因分布做成一张统一质量趋势卡（数据驱动调发送阈值）。

## 33. 多平台 deferred 发送队列（非 messenger 主动消息发送闭环）

### 立项重扫与方案决策（关键）
重扫 messenger deferred 实况：队列存于 `messenger_rpa_approvals` 表，drain 走**浏览器池 + 截图 + peer_text 新鲜度**——与 RPA runner 深度耦合，是为「人审/safe_skip 类回复 + 浏览器真发」定制，**不可直接套**到可编程发送的平台。`main.py::_care_send`/`_send_to_messenger` 都 `if channel!="messenger": return 0`（非 messenger 主动消息**被丢弃**）。

发现关键复用点：`account_orchestrator.send(platform, account_id, chat_key, text)` 已是**通用投递**——telegram/whatsapp/line worker 都暴露 `send` 且回写收件箱出站镜像（companion proactive 的 `_send` 已在用）。

→ **决策**：不去改各平台 runner 内建队列（高风险大改），而是建一个**平台无关的轻量 deferred outbox**（独立 SQLite，与 messenger 路径完全解耦），drain 时经编排器统一投递。messenger 保留既有 RPA 路径不动。

### 实施
- [x] `src/integrations/shared/deferred_outbox.py`：
  - `DeferredOutboxStore`（独立表 `deferred_outbox`）：enqueue/drain_due/mark_sent/mark_failed/mark_expired/push_until/count/list_recent。
  - `DeferredDispatcher`（drain loop + 通用护栏，与 care/reactivation 同范式）：**staleness 过期 → kill-switch（复用 `src.ops.kill_switch.is_blocked`）→ quiet_hours 顺延 → per-(platform,account) pacing 最小间隔 → sender 注册表**；任一护栏不过 `push_until` 推后**不丢消息**。
  - `register_sender(platform, fn)` 注册表；`DeferredSenderNotReady` 异常区分「worker 暂未就绪（推后重试）」与「投递失败（mark_failed）」。
- [x] `main.py`：`_ensure_deferred_outbox()` 惰性建队列 + 注册「编排器统一 sender」（owns→orch.send；回落主 TG 客户端；都不可用→抛 NotReady 推后）；`_enqueue_deferred_outbox()` 给 care/reactivation 的非 messenger 分支用；`_maybe_start_deferred_outbox()` 启动 drain loop；shutdown 优雅 stop。两条 send_callback 的 `return 0` 升级为入通用队列。
- [x] `config.example.yaml`：`companion.multiplatform_deferred`（enabled/interval/max_per_tick/min_gap_sec/quiet_hours/platforms，默认关）。
- [x] 单测 +14：enqueue/drain、staleness 过期、kill-switch 推后、quiet 顺延、pacing 推后、无 sender 推后、NotReady 推后、sender False/异常 → failed、max_per_tick 限流、register/has_sender。

**测试**：✅ deferred 14 passed；care/route inventory/config_check 69 passed；`import main` ok；全量 **5609 passed / 31 skipped / 0 fail（238s）**。

**实施中的再优化（在原方案上又改进）**：
1. **零 runner 改动**：没在每平台 runner 重建队列（原朴素设想），而是发现 `orchestrator.send` 已是通用投递层 → 一个 sender 委托编排器路由到任意平台 worker，价值等价、风险趋零（复用已验证发送路径 + 收件箱镜像）。
2. **三态投递语义**：sender 不止 True/False——`DeferredSenderNotReady` 表达「此刻无 worker（暂态）」→ 推后重试而非标失败，避免账号 worker 临时下线导致主动消息被误判丢弃。
3. **护栏顺序即语义**：staleness 在最前（错过时机的消息不该再过后续护栏白耗），kill-switch 次之（急刹优先级最高），pacing 用 last+min_gap 作为推后目标（而非 now+gap，避免堆积时漂移）。
4. **零破坏路由**：非 messenger 分支「队列关/不可用 → 返回 0」与原 `return 0` 语义完全一致，上层 care/reactivation 的 mark_skipped/failed 行为不变；messenger 路径一字未动。

**已知局限**：reactivation_loop 的 `_schedule_one` 目前**硬编码只找 messenger 身份**，故 reactivation 暂不会产出非 messenger 任务（该队列对 reactivation 是「预留接线」）；真正多平台扩面来自 **care**（care_schedule 入库时已存任意 platform/chat_key）。pacing 的 `last_sent` 为内存态（重启后首条不受 min_gap 约束，可接受）。队列暂无运营可视化端点。

**下一阶段建议**：
- **deferred outbox 运营可视化**：`GET /api/deferred-outbox/status`（各 status 计数 + 最近条目 + 各平台 sender 是否就绪），挂到 dashboard——让运营能看到「排了多少、发了多少、卡在哪个护栏」。`store` 已挂 `web_app.state.deferred_outbox_store` 备用。
- **reactivation 多平台化**：把 `_schedule_one` 的「只找 messenger 身份」放宽为按 ChannelIdentity 优先级选平台，让唤醒也能跨平台。
- **O·P 联动质量看板**（承 §32）。

## 34. 多平台 deferred 队列 · 运营可观测性（status 端点 + dashboard 面板）

### 立项确认（确认没开发过）
grep `deferred-outbox|deferred_outbox` 于 `src/web` → 0 命中，确认无现成端点。§33 队列已落地但「盲发」——运营看不到排队/失败/卡护栏情况，是安全灰度前的硬缺口。

### 实施
- [x] `DeferredOutboxStore.stats()`：单次 SQL 聚合 `by_status` + `pending_by_platform` + **`pending_by_reason`**（可观测性核心：直接看出 pending 卡在 quiet_hours/pacing_min_gap/no_sender/kill_switch/sender_not_ready 哪道护栏）。
- [x] `DeferredDispatcher.registered_platforms()`：已注册 sender 平台列表。
- [x] `src/web/routes/deferred_outbox_routes.py`：`GET /api/deferred-outbox/status`（只读）——store 缺→`enabled:false` 不报错；返回 stats + senders + recent N 条。**reply_text 不外泄**（只回 `reply_len`，隐私）。
- [x] `admin.py` 注册 + `main.py` 把 dispatcher 也挂 `web_app.state.deferred_outbox_dispatcher`。
- [x] `dashboard.html`：「多平台 deferred 队列」面板（状态计数 / pending·平台 / pending·卡在护栏 / 已注册 sender / 最近条目按状态色条）。
- [x] 单测 +5：store `stats()` 护栏分组 + `registered_platforms` 排序；端点未启用/stats&senders/reply 不外泄/limit 上限。route inventory 白名单 +1。

**测试**：✅ deferred+routes+inventory 23 passed；`import main` ok；全量 **5614 passed / 31 skipped / 0 fail（222s）**。

**实施中的再优化（在原方案上又改进）**：
1. **`pending_by_reason` 作为一等公民**：原计划只回 status 计数，实施时意识到运营真正要回答的是「为什么没发出去」——pending 的 `reason` 字段恰好记录了被哪道护栏推后，直接 GROUP BY reason 暴露，是这次最有价值的设计点（一眼定位「全卡在 no_sender→worker 没起」还是「卡在 pacing→发太快」）。
2. **单次聚合 vs 多次 count()**：用一条 GROUP BY 出全部分组，而非 `count(status=...)` 调 N 次，热路径友好。
3. **隐私默认**：recent 不回 `reply_text` 全文（主动消息含个性化内容），只给长度——可观测但不泄露话术。
4. **零耦合容错**：store/dispatcher 经 `app.state` 注入，缺任一都软降级（`enabled:false` / `senders:[]`），功能关时端点照常响应不 500。

**已知局限**：面板为手动刷新（未接 15s 轮询，避免常驻拉库）；无「重试失败条/清空 expired」运营动作（当前纯只读，写动作待评估必要性）。

**下一阶段建议**：
- **reactivation 多平台化**：放宽 `_schedule_one` 按 ChannelIdentity 优先级选平台（当前唯一的 messenger 硬编码限制）。
- **deferred outbox 运营动作**（可选）：失败条一键重排 / 清空 expired（需 CSRF + 审计）。
- **O·P 联动质量看板**（承 §32）。

## 35. reactivation 多平台化（按 ChannelIdentity 优先级选渠道）

### 立项确认（确认没开发过）
`reactivation_loop._schedule_one` 实况：`msgr_id = next(i for i in identities if channel=="messenger")`，**硬编码只找 messenger**，非 messenger contact 一律 skip。这是唤醒线唯一的平台限制（care 早已多平台）。单测 `test_skips_when_no_messenger_identity` 锁定了「仅 line→skip」的默认行为。

### 实施
- [x] `_pick_identity(identities)`：按 `platform_priority` 在 contact 的 ChannelIdentity 里选渠道（同渠道多身份取首个，都没命中→skip）。
- [x] `_schedule_one`：用选中的 `channel` 取代 messenger 硬编码——`chat_name=external_id||display_name`、`account_id`、prompt 平台名（`_PLATFORM_LABELS`：messenger→Facebook Messenger / telegram→Telegram / …）、`_send(channel,…)`、dry_run 样本带 `platform`。
- [x] `platform_priority` 构造参数默认 `["messenger"]`（**零破坏**既有单测）；`main.py` 经 config 注入默认 `["messenger","telegram","line","whatsapp"]` 真正启用多平台——非 messenger 渠道由 §33 的 `_send_to_messenger`/`_care_send` 路由到多平台 deferred 队列。
- [x] 单测 +4：priority 选 telegram / messenger 优先于 telegram / 不在 priority 的 zalo→skip / 不传 priority 默认仅 messenger。

**测试**：✅ reactivation_loop 17 passed；`import main` ok；全量 **5618 passed / 31 skipped / 0 fail（207s）**。

**实施中的再优化（在原方案上又改进）**：
1. **优先级选渠道 vs 全平台齐发**：没让一个 contact 在所有平台都发（骚扰 + 重复），而是按优先级**选一个最佳渠道**——messenger 优先（既有主战场、行为不变），无 messenger 才落 telegram/line/…，单 contact 单条主动消息语义不变。
2. **loop 默认 messenger-only，启用在 config**：构造默认不动既有行为（所有旧单测零改动通过），多平台能力由 main.py 的 config 默认开启——「机制默认保守、策略在配置」的一贯分层。
3. **prompt 平台名动态化**：`_PLATFORM_LABELS` 让「正在 {平台} 上私聊」自然贴合实际渠道，不再对 telegram 用户硬说"Facebook Messenger"出戏。
4. **复用 §33 路由零新增发送路径**：非 messenger 直接走已建的多平台 deferred 队列（含 kill-switch/quiet/pacing/staleness 护栏），本阶段只改"选谁发"，不碰"怎么发"。

**已知局限**：同一 contact 即使多平台在线也只发优先级最高的一个渠道（设计如此，避免重复打扰）；非 messenger 真发仍依赖 `multiplatform_deferred.enabled`（关时 enqueue 返 0 → 记 failed 不 mark_sent，下个 tick 重试，不误标已发）。`platform_priority` 暂走代码默认（reactivation loop 无 config.example 块，遵既有约定）。

**下一阶段建议**：
- **deferred outbox 运营动作**（可选）：失败条一键重排 / 清空 expired（需 CSRF + 审计）。
- **O·P 联动质量看板**（承 §32）：care/reactivation 的 like/dislike 率 + skip 原因分布统一趋势卡。
- **拆分提交护盘**（强烈建议）：当前工作区累计大量未提交变更，建议按子系统拆 PR 落盘。

## 36. O·P 联动质量看板（care + reactivation 发送质量统一视图）

### 立项确认（确认没开发过）
实况：reactivation 已在 `bot-metrics` snapshot 里有 skip/feedback 1h 聚合；但 **care 侧无 feedback 计数、无 skip 原因聚合**（care skip 只落 care_schedule 库的 note），且**无 care+reactivation 统一视图**。grep `quality.overview` → 0 命中。

### 实施
- [x] `MetricsStore`：`_care_skipped_recent`/`_care_feedback_recent` 两个 deque + `record_care_skipped(reason)` / `record_care_feedback(verdict)`；新增统一 getter **`companion_quality_overview(window_sec)`**（两线 skip 原因直方图 + like/dislike + 赞率 + dry_run 计数 + 共享黑名单规模），辅以纯函数 `_reason_hist`。
- [x] `care_dispatcher`：抽 `_mark_skipped(sid, reason)` 统一 `store.mark_skipped` + `record_care_skipped`，6 处 skip 全改走它（原因进 metrics）。
- [x] `care_routes` dry-run-feedback：补 `record_care_feedback(verdict)` 计数（原先只 dislike→黑名单不计数）。
- [x] `monitoring_routes`：`GET /api/companion/quality-overview?window_hours=`（默认 24h，上限 720h）。
- [x] `dashboard.html`：「主动消息质量（care + 唤醒）」面板——两线 👍/👎 + 赞率 + dry_run + skip 原因 Top（按量降序）+ 共享黑名单规模。
- [x] 单测 +4：两线聚合 + 赞率/skip 直方图、无反馈赞率 None、非法 verdict 不计、端点契约（`auth_client`）。route inventory 白名单 +1。

**测试**：✅ quality+care+inventory 40 passed；`import main` ok；全量 **5622 passed / 31 skipped / 0 fail（225s）**。

**实施中的再优化（在原方案上又改进）**：
1. **统一 getter 而非各自端点**：没给 care 单独造一套 metrics 端点，而是出一个 `companion_quality_overview` 把两条线放同一结构里对照——运营要的是「care vs 唤醒谁的话术质量更差」，并排才有意义。
2. **skip 路由收口到 `_mark_skipped`**：原 6 处散落 `store.mark_skipped`，抽一个方法同时落库+落 metrics，避免「以后新增 skip 点忘了记 metrics」的漂移（与 §32 dislike 黑名单共享同一治理思路）。
3. **赞率 None 而非 0**：无反馈时 `like_rate_pct=None`（前端显示「—」）而非误导性的 0%——「没人评」与「都点踩」是两回事。
4. **窗口可调 + 上限护栏**：`window_hours` 上限 720h（30 天），避免传超大值扫全 deque 退化；deque 本身 maxlen 兜底。

**已知局限**：feedback/skip 均 in-memory（重启清空，与 reactivation 既有口径一致，灰度期人工反馈定位）；care 的「真发成功率」未并入（care 真发经 deferred 队列，成功率应看 §34 的 deferred-outbox status，两看板互补而非合并）。

**下一阶段建议**：
- **deferred outbox 运营动作**（可选）：失败条一键重排 / 清空 expired（CSRF + 审计）。
- **质量趋势持久化**：把 quality-overview 按小时落 SQLite，画 7 日趋势（当前只有即时窗口）。
- **⚠️ 拆分提交护盘（强烈建议，最高优先）**：工作区未提交变更持续累积，应优先按子系统拆 PR 落盘再继续新功能。

> 注：上述「deferred 运营动作 / 质量趋势持久化 / 拆分提交」三项已在后续一轮全部完成
> （deferred retry/cancel/pause/resume + quality_trend_store 时序 + 10 提交拆分 PR #72 squash 合入 main）。

## 37. Phase K2 · C 端变现闭环（端用户订阅/付费解锁/打赏 + 权益 gate + 营收台账）

### 立项重扫（以代码为准，确认空白）
重扫六竞品后定位最大未做商业差距 = onechat 的「虚拟人陪聊 + **C 端变现闭环**」。grep 验证代码实况：
- `persona_routes.py`/`persona_manager.py` 人设管理**已成熟**（CRUD+四层配置+绑定）→ 排除「人设市场」为伪空白。
- `billing.py` 只有 **B2B 运营计费**（月账单/套餐含量/超额/CSV，对账维度全是 messages/seats）；
  `web_user_store` 是运营/坐席账户；`entitlement|wallet|credit|ledger` grep 在 src 内**零业务命中**。
- → **C 端变现确为整仓空白**。付费主体 = 与 AI 陪伴对话的**端用户**（contact），以 `contact_key`
  （= 收件箱 `conversation_id`，与 care_schedule O4 对齐）为唯一标识。

### 设计原则（复用既有范式，零重造）
- 纯函数仿 `billing.py`、SQLite store 仿 `care_schedule`/`crisis_event_store`、gate 原语仿 `licensing/gate.py`
  （`gate_enabled=False → 恒放行`，零破坏陪伴行为）、路由/注入/懒建单例仿 `care_routes`。
- 与 B2B `pricing` **正交**：本块是「端用户→运营方」内容/会员变现，两套价目互不污染。

### 4.K2-1 · monetization 纯函数 ✅
- [x] `src/utils/monetization.py`：`DEFAULT_CATALOG`（currency + tiers free/vip/svip 各带 grants + items 解锁项 + gifts 礼物）+ `merge_catalog`（config 深合并不污染默认）+ `tier_grants`/`subscription_active`/`effective_tier`（过期降级 free）/`entitlement_allows`（grant 或 unlock 命中）/`feature_allowed`（**gate 原语，关时恒放行**）/`quote`（三类报价）/`revenue_from_txs`（聚合，跳过非 paid）。无副作用、无 DB、无 FastAPI 依赖。

### 4.K2-2 · EntitlementStore（SQLite）✅
- [x] `src/utils/entitlement_store.py`：三表 `subscriptions`（一人一条当前订阅，upsert）/`unlocks`（contact+item 唯一，幂等）/`tx_ledger`（**`ref` 唯一索引 → 支付回调幂等**）。
- [x] 写：`record_tx`（ref 重复跳过）/`grant_subscription`（upsert+入账，ref 幂等不重复发权益）/`record_unlock`（已持有不重复）/`record_gift`（纯入账）。
- [x] 读：`get_entitlement`（有效 tier+grants+已解锁，绝不抛缺则 free）/`revenue_summary`（单次 GROUP BY 聚合）/`top_spenders`/`active_subscription_count`/`active_subscriptions`/`recent_tx`/`expire_subscriptions`。
- [x] 镜像约定：单连接 `check_same_thread=False` + 写锁 + 绝不抛 + `:memory:`/文件双模式 + 进程内单例 `get_entitlement_store`（+ `reset_entitlement_store` 供测）。

### 4.K2-3 · gate 原语 + runtime 单例 ✅
- [x] gate 原语 `feature_allowed`（在 monetization.py）+ runtime 访问器 `get_entitlement_store`（在 entitlement_store.py）已就绪。
- [x] **刻意不强插 send 热路径**——仿 Phase J「seat_exceeded 先定义导出、后在边界 wire」的精神：变现基建+台账+权益+gate 原语先落地可单测，实际功能门控点（语音/主动频次/剧情解锁）待产品定后单独 wire（默认 `monetization.gate.enabled:false` 恒放行，零行为风险）。

### 4.K2-4 · routes + 后台页 + config + 接线 ✅
- [x] `src/web/routes/monetization_routes.py`：`GET /api/monetize/overview`（营收概览+活跃订阅+Top消费+最近流水）/`GET catalog`/`GET entitlement`/`POST grant`（运营手动开通）/`POST webhook`（支付服务商桩：`X-Monetize-Secret` 共享密钥校验 + ref 幂等记账发权益）。store 经 `app.state.entitlement_store` 注入、缺则懒建单例。
- [x] `admin.py` 注册 + `/monetization` 页面（role=monetization）+ `_PATH_TO_ACTIVE`/`_PATH_TO_PAGE` 两 map；`web_user_store.PAGE_PERMISSIONS["monetization"]={master,admin}`（营收数据仅主帐号+管理员）。
- [x] `monetization.html`：营收卡（近N天总额/活跃订阅/订阅·解锁·打赏分项）+ Top 消费榜 + 最近流水表 + 运营手动开通/查权益表单。base.html 桌面+移动导航接入。
- [x] config `monetization.*`（enabled/webhook_secret/expire_on_startup/gate.enabled/catalog）**默认全关**。
- [x] `main.py::_maybe_init_monetization`（gated）：enabled 时按 catalog 建 EntitlementStore 单例→挂 app.state→启动清理过期订阅；关时不建库（路由按需懒建只读）。
- [x] 单测：`tests/test_monetization.py`（16 项：纯函数 8 + store 8）+ `tests/test_monetization_routes.py`（8 项：catalog/overview/grant 三类/webhook 幂等/密钥校验）；route inventory 白名单 +5 端点 +1 页面。

**测试**：✅ K2 栈 24 passed；route inventory + config_check 37 passed；`import main` ok；Jinja 解析 ok；全量 **5668 passed / 31 skipped / 0 fail（273s）**。

**K2 实施中的再优化（在原方案上又改进）**：
1. **付费主体定为 `contact_key`（= conversation_id）而非新造端用户表**：与 care_schedule/inbox 同 ID 空间，变现信号天然可与关怀/健康卡聚合（未来一处 contact 看「关系+变现」全貌），不引入第四套身份。
2. **`tx_ledger.ref` 唯一索引做幂等**：支付服务商回调天然会重投（at-least-once），用 `ref` 唯一约束 + `grant_subscription` 检测「ref 已存在→不重复发权益」彻底防「重投发两次会员」，比应用层查重更可靠。
3. **gate 默认放行 + 不强插热路径**：变现门控 `gate_enabled=False` 恒放行，且本轮**不动陪伴 send 路径**——遵 AGENTS.md「控风险」与 Phase J 范式，基建先行、门控点后定，避免「为变现改了陪伴行为」的回归面。
4. **catalog 深合并不污染默认**：`merge_catalog` deepcopy 默认目录再合并 config 覆盖，单测钉死「改 config 不改 `DEFAULT_CATALOG`」，防多实例/多测试间状态串味。
5. **营收聚合用单次 SQL GROUP BY**（revenue_summary/top_spenders）而非拉全表内存算——量大也稳，与既有 store 性能纪律一致。

**已知局限 / 留待后续**：
- **支付网关未真接**：webhook 为 provider-agnostic 桩（共享密钥校验占位），真接 Stripe/支付宝/微信/Telegram Stars 需各自验签 + 事件映射（按目标市场单独立项）。
- **gate 未 wire 到具体功能**：语音回复/主动关怀频次/剧情解锁等付费门控点待产品定义后接（gate 原语已就绪、默认关）。
- **退款/订阅自动续费状态机简化**：当前 tx 有 refunded 状态但无退款流程；续费靠 grant 覆盖 active_until（无自动扣费循环）。

**下一阶段建议**：
- **K2 延伸·gate 接一个真实门控点**（如「语音陪伴回复仅 VIP+」）+ 端用户付费引导话术（陪伴 prompt 在恰当时机软提示升级），把「能记账」升级为「能转化」。
- **变现×关系健康聚合**：把 `tx_ledger` 的 LTV/付费档接入 Phase P 健康卡（高价值付费用户单列预警），复用 §31 身份桥。
- **支付网关真接**（按目标市场：Telegram Stars / Stripe / 微信支付）单独立项。
- **⚠️ 拆分提交护盘**：本轮 K2 变更（5 新文件 + admin/main/base/config/web_user_store 接线）建议尽快按子系统落盘。

## 38. Phase K2b · 变现真实门控点 + 付费转化引导（feature-check API + 主动关怀配额门控）

### 立项（承 §37 下一阶段建议①）
K2 落地了变现基建+台账+权益+gate **原语**，但 gate 未接任何真实功能 → 「能记账、不能转化」。
本轮把原语接成「陪伴→门控→升级引导」闭环：**一个集成缝 API + 一个真实门控点 + 得体的转化文案**。
门控点选 **主动关怀配额**（我们自己的 `care_dispatcher`，非 RPA 发送路径，低风险），刻意**不碰**
语音/RPA 发送链（散落各 runner，AGENTS.md 红线）。

### 4.K2b-1 · upsell 转化决策 + 配额纯函数 ✅
- [x] `monetization.py` 增：`upsell_offer(entitlement, feature, *, catalog, gate_enabled)`——为缺某功能的端用户算**最便宜**的可解锁 tier（找不到 tier 再看同名 item），gate 关/已拥有 → None；`upsell_pitch_hint(offer, persona_name)`——**得体、贴人设**的软引导文案（"升级 VIP，{她}就能更常陪着你～💕"，非弹窗式硬推销）；`proactive_quota_allowed(entitlement, sent_count, *, free_quota, gate_enabled)`——免费超额拦、`unlimited_proactive` 不限、gate 关恒放行。

### 4.K2b-2 · 变现运行时门控 ✅
- [x] `src/utils/monetization_runtime.py::MonetizationRuntime`（`from_app(app)` 从 app.state 组装 store+config，store 缺→None）：`gate_enabled()`（enabled ∧ gate.enabled）/`feature_check(ck, feature)`（→ allowed+entitlement+upsell+pitch_hint）/`proactive_allowed(ck, sent_count)`。全 best-effort、绝不抛、缺则放行。

### 4.K2b-3 · feature-check 集成缝 API ✅
- [x] `POST /api/monetize/feature-check`（body `{contact_key, feature}`）：任意前端付费功能（语音按钮/剧情解锁/主动等）发送前先查 → `{allowed, upsell?, pitch_hint?}`。gate 关恒 allowed=True（零破坏）；runtime 缺失也安全放行。路由白名单 +1。

### 4.K2b-4 · 真实门控点 wire（主动关怀配额，默认关）✅
- [x] `CareScheduleStore.count_sent_since(contact_key, since)`：窗口内已发主动关怀数（配额计数，复用既有 sent_at，无新状态）。
- [x] `CareDispatcher` 增可选注入回调 `proactive_allowed(contact_key)->bool`（仿 `already_discussed` 范式）：返回 False → `_mark_skipped(sid, "paywall_quota")`，**放在 LLM 之前**超额不白耗 token；**未注入（None）则行为与原来完全一致**。
- [x] `main.py::_build_care_paywall(care_store)`：仅当 `monetization.enabled ∧ gate.enabled` 才返回回调（否则 None=零破坏）；回调**懒读** `MonetizationRuntime.from_app(web_app)` → 近 24h 已发主动数 vs 免费配额。`_maybe_init_monetization` 提前到 proactive_care 之前，保证 gate 开时 store 已就绪。
- [x] config `monetization.upsell.free_proactive_daily`（默认 1）。
- [x] 单测 +17：upsell 选最便宜 tier/item 回退/已拥有或 gate 关→None/文案口吻；proactive_quota 四态；runtime feature_check（gate 关放行/免费拒+upsell/订阅放行）+ proactive_allowed 配额；`count_sent_since`；feature-check 路由 4 项；care_dispatcher paywall（超额跳过+原因/放行/None 零变）3 项。

**测试**：✅ K2b 栈 96 passed（含 monetization 全栈 + care_dispatcher + route inventory + config_check）；`import main` ok；全量 **5685 passed / 31 skipped / 0 fail（260s）**。

**K2b 实施中的再优化（在原方案上又改进）**：
1. **门控点选「主动关怀配额」而非语音回复**：勘察发现语音散落各 RPA runner（发送路径，AGENTS.md 红线），改选 `care_dispatcher`（我们自己的代码、已有注入回调+skip-with-reason 基建）——价值等价（"她主动找你"是陪伴付费点）、风险低一个数量级，且零碰 RPA。
2. **转化不污染陪伴对话**：付费引导走 **feature-check API + UI/坐席展示**（`pitch_hint`），**不**把销售话术自动注入端用户收到的关怀消息——守北极星「陪伴深度」，避免「到点打卡式推销」毁体验；门控只调**频次**（免费少、VIP 不限），不塞广告文案。
3. **回调懒读 runtime**：`proactive_allowed` 闭包在 dispatch 时才 `from_app` 取 store/config，而非构造期绑定——即使 store 晚于 dispatcher 构造、或运行中热更 config 都能跟上；构造期只判「要不要注入」（gate 关→None 彻底零开销）。
4. **paywall 检查置于 LLM 之前**：超额用户直接 skip，不白烧生成 token；skip 原因 `paywall_quota` 进 §36 质量看板（与其它 skip 原因同口径可观测）。

**已知局限 / 留待后续**：
- 仅 wire 了「主动关怀配额」一个门控点；语音/剧情解锁等需各自在**可编程**层接 feature-check（RPA 语音不在本轮）。
- `free_proactive_daily` 配额按「自然 24h 滚动窗 + sent 计数」近似，未做按日历日重置（够灰度用）。
- 转化文案 `pitch_hint` 已生成但前端尚未消费（feature-check 返回，等具体付费 UI 接入）；真支付仍是 webhook 桩。

**下一阶段建议**：
- **K2c·变现×关系健康聚合**（承 §37 建议②）：把 `tx_ledger` 的 LTV/付费档接入 Phase P 健康卡（高价值付费用户单列），复用 §31 身份桥——让运营一眼看到「谁在付费、谁该挽留」。
- **付费 UI 消费 pitch_hint**：前端/坐席台在 feature-check 拒绝时渲染升级卡片（把已备的 `pitch_hint`/`upsell` 用起来）。
- **支付网关真接**（Telegram Stars / Stripe / 微信）单独立项。
- **⚠️ 拆分提交护盘（最高优先）**：K2 + K2b 已累计 7 新文件 + 多处接线，强烈建议尽快按子系统拆 PR 落盘。

## 39. Phase K2c · 变现×关系健康聚合（LTV/会员档接入预警榜 + 付费流失单列）

**目标**（承 §38 建议①）：把 §37 建好的 `tx_ledger`/订阅数据，复用 §31 身份桥反推的 conversation_id，聚合进 Phase P 单人健康卡 + 流失预警榜——让运营一眼看到「谁在付费、谁是付费用户正在流失（最该挽留）」。**零新表、零新路由、纯叠加既有两条线**。

### 4.K2c-1 · EntitlementStore 批量聚合方法 ✅
- `spend_by_contacts(keys)`：分块 IN 查询 `tx_ledger`（仅 `status='paid'`，`refunded` 不计）→ `{contact_key: LTV}`，避免预警榜 N+1。
- `tiers_by_contacts(keys, now)`：批量取**当前有效**会员档（`status='active'` 且 `tier!='free'` 且 `active_until>now`，过期自动排除）。
- 两者都 500 一批、绝不抛（变现不可用不拖垮健康榜）。

### 4.K2c-2 · best_tier / tier_rank 纯函数 ✅
- `tier_rank(tier, catalog)` 按 catalog 月费给档位排序权重；`best_tier(tiers, catalog)` 从单人多会话的若干 tier 里取**最高档**（一个人可能在多平台会话各有订阅，合并取最高）。

### 4.K2c-3 · contacts_routes 聚合缝（复用身份桥）✅
- `_monetization_for_keys(conv_ids)`（单卡）+ `_monetization_batch(jids,…)`（榜单，复用榜单已算好的 `convkeys_by_contact`，一次 SQL 取全量 key）。
- 单卡 `GET /api/relations/health/{jid}` 新增 `monetization` 块；榜单 `GET /api/relations/health-board` 上榜行带 `monetization` + 顶层 `payer_count` 汇总。
- **减噪**：非付费用户（LTV=0 且无有效会员）→ 该块为 None，不占视觉。变现未启用（无 `app.state.entitlement_store`）→ 全 None，零行为变化（自然门控，**不**懒建库）。

### 4.K2c-4 · 前端预警榜「付费」列 + 付费流失高亮 ✅
- 新增「付费」列：付费用户显示 `币种 LTV` 绿徽 + 会员档紫徽；汇总条加「付费用户 / 付费流失」两枚 pill。
- **付费用户正流失**（付费/会员 ∧ at_risk/critical）→ 行紫色高亮 + 「付费流失」红标，盖过普通「高价值流失」——把最该挽留的人推到最显眼。
- 重逢草稿弹窗：发送前展示 `💎 付费用户 · LTV · 档位`，坐席挽留时知道对方价值。

**测试**：✅ K2c 新增 9 测试（best_tier/tier_rank + 批量 spend/tiers + 单卡 payer/free/无 store + 榜单列 payer_count）；全量 **5691 passed / 31 skipped / 0 fail（271s）**。

**K2c 实施中的再优化（在原方案上又改进）**：
1. **批量复用榜单已算的 `convkeys_by_contact`**：不另做一次 CI 反查，变现聚合搭 care/inbox 的便车——榜单一次身份解析喂三个域（care pending + inbox 语境 + 变现 LTV）。
2. **聚合点放「上榜行」而非「全扫描行」**：镜像 §26 R2——SQL 随 `limit`（默认 30）增长而非 `scan`（默认 400），上量也不放大变现库压力。
3. **多会话 LTV 合并 + 取最高档**：一个端用户可能在 Telegram/Messenger 多会话分别付费，身份桥合并后 LTV 求和、会员档取 `best_tier`——避免「同一人被算成多个小付费」。
4. **展示而非改排序**（本轮克制）：变现信号目前只**单列展示 + 高亮**，不改健康分排序模型（避免 §27 R3 那样的排序复杂度蔓延）；「付费权重并入 value_at_risk 排序」留作可选下一步（见下）。

**已知局限 / 留待后续**：
- 变现信号不参与榜单**排序**（仅展示/高亮）；若要「付费流失绝对置顶」，需扩 `_board_sort_key` 加 payer 维度（建议跟 §27 一样走 config flag，默认关）。
- LTV 取全时段累计，未分「近 30 天活跃付费 vs 历史」；运营若要「近期掉付费」预警需再加时间窗聚合（`spend_by_contacts` 已支持 `since`，仅前端未透出）。

**下一阶段建议**：
- **①付费流失排序加权（config flag）**：把 `payer_at_risk` 并入 `_board_sort_key`，让付费正流失绝对置顶；低风险（沿用 R3 flag 模式）。
- **②付费 UI 消费 pitch_hint**（仍未做）：feature-check 拒绝时前端渲染升级卡片。
- **③支付网关真接**（Telegram Stars / Stripe）单独立项。
- **⚠️ 拆分提交护盘（最高优先升级）**：K2 + K2b + K2c 已累计 7 新文件 + 多模块接线（monetization 全栈 / care_dispatcher / contacts_routes / 前端 / 配置），**强烈建议本轮后立即按子系统拆 PR 落盘**，避免大改积压。

## 40. 落盘 + Phase K2c① · 付费流失排序加权（config flag，默认关）

**落盘止血**：变现栈按子系统拆 4 提交（cumulative-green，依赖前向流动）落 `main`：
1. `feat(care)` 主动关怀 proactive 门控钩子 + `count_sent_since`（无变现依赖，独立可绿）。
2. `feat(monetize)` K2/K2b 变现核心 + runtime + routes + UI + 接线（依赖 1）。
3. `feat(monetize)` K2c 变现×关系健康聚合接缝（依赖 2）。
4. `docs(devlog)` §37/§38/§39 回写。
（`config.yaml.bak_preBeatrice` 备份故意不纳入版本控制。）

**K2c①**（承 §39 建议①）：`health_board_sort_key` 加 `payer_priority` 维度（纯函数），
config `companion.relations_health.health_board.payer_sort_priority`（默认关）。开启后
**付费/会员用户正流失（at_risk/critical）绝对置顶**——盖过普通「高价值流失」，运营最该挽留的人一眼可见。
- 镜像 R3 模式：开关开时对**全部候选**批量富集变现信号再 sort（仍仅多 1 次批量 SQL）；
  关时维持 K2c 默认（仅富集上榜行，SQL 随 limit 不随 scan）。
- 榜单响应加 `payer_sort_priority` 标志；前端排序说明动态拼「付费流失置顶 + …」。
- 抽出 `is_payer_at_risk()` 纯函数（前端高亮口径与排序口径一致）。

**测试**：✅ 新增 3 测试（纯函数 payer_priority 序 + 路由开关置顶 + 默认关）；全量 **5694 passed / 31 skipped / 0 fail（263s）**。

**下一阶段建议**：②付费 UI 消费 `pitch_hint`（feature-check 拒绝时渲染升级卡片）；
③支付网关真接（Telegram Stars / Stripe）；④LTV 近 30 天时间窗聚合（`spend_by_contacts` 已支持 `since`，仅前端未透出）。

## 41. Phase K2②③ · 升级卡片消费 pitch_hint + 支付网关真接（Stripe / Telegram Stars）

承 §40 建议②③，一并落地「转化引导前端」+「真支付通路」。**provider 默认全关**，未配密钥时
对应 checkout/webhook 直接拒绝；陪伴主流程零影响。

### 4.K2③-1 · payment_gateway.py 纯函数核心（可测、无网络）✅
- **Stripe**：`stripe_verify_signature`（`t=,v1=` HMAC-SHA256 + `compare_digest` 防时序 + 时间容差防重放）、
  `parse_stripe_event`（`checkout.session.completed` → grant 字典，金额从 `amount_total` 分还原，
  ref=事件 id）、`build_stripe_checkout_params`（Checkout Session form 参数，grant 进 metadata）。
- **Telegram Stars**：`telegram_verify_secret`（`X-Telegram-Bot-Api-Secret-Token`，空密钥拒绝）、
  `parse_telegram_successful_payment`（XTR 整数星 / 法币分两种口径）、`extract_telegram_pre_checkout`、
  `build_telegram_invoice_params`（`createInvoiceLink`，grant 编码进 payload 回传）。

### 4.K2③-2 · 路由：checkout + provider 专用 webhook ✅
- `POST /api/monetize/checkout`：按 catalog 报价 → 构建参数 → best-effort 调 provider API 拿 `pay_url`；
  provider 未启用/未配密钥 → `provider_disabled`，未知项 → `unknown_item`。
- `POST /api/monetize/webhook/stripe`：验 `Stripe-Signature` → 解析 → 幂等发权益。
- `POST /api/monetize/webhook/telegram`：验 secret 头 → 应答 `pre_checkout`（10s 内）/ 解析 `successful_payment` 发权益。
- 抽出 `_apply_grant(store, grant, source)` 归一三种回调（generic/stripe/telegram）统一落库，
  全走 `ref` 幂等（支付回调 at-least-once 必须幂等）。
- 网络调用经 `_provider_request`（aiohttp，已声明依赖）best-effort 包裹，失败返回 `(0, {})` 不抛。

### 4.K2②-1 · 升级卡片面板消费 feature-check（monetization.html）✅
- 新增「功能门控 / 升级卡片预览」面板：输入 contact_key + 功能位 → 调 `/api/monetize/feature-check`：
  - allowed → 绿条「已拥有」；denied → 渲染**升级卡片**（pitch_hint 文案 + 价格 + 含的功能位 +
    Stripe / Telegram Stars 两个「去支付」CTA）。
  - CTA 调 `/api/monetize/checkout` 拿 `pay_url` 内联展示。
- 顺手给 `upsell_offer`（subscribe 分支）补 `grants` 字段，卡片可列出「升级后获得哪些功能位」。

**测试**：✅ 新增 ~20 测试（payment_gateway 验签/解析/参数构建全覆盖 + checkout 禁用/未知项/未知 provider +
stripe webhook 验签失败/成功幂等 + telegram 未授权/pre_checkout/successful_payment）；
路由白名单补 3 条；全量 **5712 passed / 31 skipped / 0 fail（260s）**。

**实施中的再优化**：
1. **纯函数与网络解耦**：验签/解析/参数构建全做成无网络纯函数单测覆盖；真网络调用隔离在 `_provider_request`
   best-effort 层（不进单测、不 flaky），既「真接」又可测。
2. **三回调归一 `_apply_grant`**：generic webhook + stripe + telegram 落库逻辑收敛到一处，
   全部 `ref` 幂等，避免三套发权益逻辑漂移。
3. **本仓只有后台前端**：端用户 MiniApp 不在本仓，故②落成**坐席/运营**可用的升级卡片预览
   （也正是端用户触达付费墙时该看到的卡片），即时可用且零端依赖。

**已知局限 / 留待后续**：
- checkout 的 provider 网络调用未进单测（避免真打 Stripe/Telegram）；建议接真密钥后手测一次回路。
- Telegram Stars 计价：catalog 暂无 `stars` 字段，用 `round(amount)` 兜底；接入前应在目录显式标星价。
- Stripe 订阅用 `mode=payment`（一次性）+ days 授予，非 Stripe 原生 recurring subscription；
  续费目前靠运营/再次 checkout（原生续费 + `invoice.paid` 周期发权益可后续接）。

**下一阶段建议**：④LTV 近 30 天时间窗聚合透出前端；⑤Stripe 原生 recurring 订阅 + 自动续费发权益；
⑥端用户 MiniApp 接 checkout（本仓 API 已就绪，跨仓前端单独立项）。

## 42. Phase K2④⑤ · 近 30 天 LTV 透出 + Stripe 原生 recurring 订阅

承 §41 建议④⑤，一并落地「掉付费识别」+「订阅自动续费」。

### 4.K2④ · 近 30 天活跃付费窗 + 掉付费预警 ✅
- 聚合层（`_monetization_for_keys`/`_monetization_batch`）复用 `spend_by_contacts(since=)` 加算
  `ltv_recent`（近 `_LTV_RECENT_DAYS=30` 天）、`recent_days`、`lapsed`（历史付费但近 30 天 0 付费）。
- 前端预警榜「付费」列：LTV 旁加 `+近期额` 绿徽 / **掉付费** 红徽；汇总条加「掉付费」pill；
  重逢草稿弹窗付费行展示「近 30 天 X · ⚠️掉付费」——运营一眼识别「曾付费正在流失」的高挽留价值用户。
- 仍只多 1 次批量 SQL（同一批 key 加一个 `since` 查询），口径与全时段 LTV 一致。

### 4.K2⑤ · Stripe 原生 recurring 订阅 + invoice.paid 周期发权益 ✅
- `build_stripe_checkout_params(recurring=True)`：`mode=subscription` + `price_data[recurring][interval]`，
  并把 grant 写进 `subscription_data[metadata]`——让**续费发票**也能溯源端用户。
- `parse_stripe_event`：
  - `checkout.session.completed` 且 `mode=subscription` → **None**（让位 `invoice.paid`，防首付双发）；
  - `invoice.paid`（首付 + 每期续费）→ metadata 依次取 `subscription_details`/`lines`/顶层，
    金额从 `amount_paid`，**ref=发票 id**（每期一张 → 续费天然按期发权益且幂等）。
- checkout 路由：订阅默认走 recurring（`providers.stripe.recurring` 默认开，body 可覆盖）+ `interval` 可配。

**测试**：✅ 新增 6 测试（recurring 参数构建 + 订阅 checkout 让位 + invoice.paid 首付/续费/lines 兜底 +
路由 invoice.paid 发权益 + 健康卡近 30 天窗/掉付费）；全量 **5718 passed / 31 skipped / 0 fail（225s）**。

**实施中的判断**：
1. **防双发**：订阅首付 Stripe 会同时发 `checkout.session.completed` + `invoice.paid`；统一只认 `invoice.paid`
   （ref=发票 id），session 让位——首付与续费走同一条路径，零特例。
2. **续费溯源**：session metadata 不传播到续费发票，故把 grant 冗余写到 `subscription_data[metadata]`，
   解析时多源兜底（`subscription_details`→`lines`→顶层），兼容不同 API 版本。
3. **掉付费 = 高价值挽留信号**：`lapsed`（有 LTV 但近 30 天断付）比单纯 LTV 更可执行，直接进预警榜视觉。

**已知局限**：`grant_subscription` 续费按 `now+days` 绝对设置（非在原有效期上叠加）；月度账单到期续费时
`now≈旧到期`，误差可忽略；若需严格不丢天，可后续改 additive。退订（`customer.subscription.deleted`）
暂未接，靠 `active_until` 自然过期 + `expire_on_startup` 收敛。

**下一阶段建议**：⑥端用户 MiniApp 接 checkout（本仓 API 就绪，跨仓前端单独立项）；
⑦退订事件接入（`customer.subscription.deleted` → 立即标 expired）；⑧掉付费用户自动触发挽留关怀（接 Phase O）。

## 43. Phase K2⑦⑧ · Stripe 退订即时收敛 + 流失付费挽回榜

承 §42 建议⑦⑧。⑧刻意**不**走「自动往陪伴对话注入续费话术」（违背北极星 + 需产品确认），
改做**运营驱动的挽回工作台**：变现库全量识别流失付费用户，运营一眼看「该挽回谁」。

### 4.K2⑦ · Stripe 退订即时作废 ✅
- `parse_stripe_cancellation`（`customer.subscription.deleted` → contact_key，依赖 ⑤ 写入的
  `subscription_data[metadata]`）+ `EntitlementStore.cancel_subscription`（立即 status=expired、
  active_until=now）。`/webhook/stripe` 先判退订再判发权益。
- 价值：用户主动退订 / 账单失败到期，Stripe 发 deleted → 立即收敛，不再等 `active_until` 自然过期，
  让⑧的「流失」判定更准。

### 4.K2⑧ · 流失付费挽回榜 ✅
- `EntitlementStore.lapsed_payers(recent_days=30)`：单条 SQL（GROUP BY + HAVING total>0 AND recent<=0）
  找「有历史已付但近 N 天 0 付费」，按累计 LTV 降序；附 `days_since_paid` + **最后已知会员档**
  （含已过期/退订，挽回时知道对方原是什么档）。
- `GET /api/monetize/retention` + 后台 monetization 页「流失付费挽回榜」面板（LTV / 距上次付费 / 原档 +
  「挽回」按钮一键回填 contact 查权益 / 手动开通）。
- 与 §40④ 健康榜「掉付费」互补：那里是**按 journey** 在健康榜扫描范围内标记，这里是**变现库全量**
  按 LTV 排序的挽回清单，运营视角不同、互不替代。

**测试**：✅ 新增 5 测试（cancel_subscription + lapsed_payers + parse_stripe_cancellation +
退订 webhook 收敛 + retention 端点）；全量 **5723 passed / 31 skipped / 0 fail（191s）**。

**实施中的判断**：
1. **⑧不碰陪伴对话**：把「掉付费」变可执行靠**运营清单**而非自动话术——守北极星、零品牌风险、
   无脆弱启动接线；自动挽回关怀（接 Phase O）作为需产品确认的后续项保留。
2. **「原档」读 raw tier**：lapsed 用户订阅多已过期，`tiers_by_contacts`（仅 active）会全返 free；
   故 `lapsed_payers` 单独查 subscriptions 原始 tier，挽回时能看到对方原来是 VIP/SVIP。
3. **退订先于发权益判定**：webhook 里 `customer.subscription.deleted` 优先短路，语义清晰不误入发权益分支。

**下一阶段建议**：⑥端用户 MiniApp 接 checkout（跨仓单独立项）；
⑧-ext 掉付费自动挽回关怀（接 Phase O，**需产品确认文案策略**——保持温暖陪伴口吻而非推销）；
⑨退订/流失漏斗分析（订阅时长分布、退订率、挽回成功率）。

---

## 44. 回到竞品对标主线 · 立项「记忆→成长→剧情」需求链（2026-06-21 重扫）

> 用户纠偏：变现栈（K2 全家桶）已过度打磨，**退回竞品分析对比主节奏**。
> 以 grep 核实代码实况（不信 DEVLOG 自述），多维 GAP 重扫，剔除「需外部推理服务」
> （语音/换脸）与「重·偏战略」（UGC 角色市场），收敛出**全自治 + 北极星对齐 + 与已建
> 系统咬合**的一条需求链：**记忆（记得住）→ 成长（看得见关系变深）→ 剧情（深到一定
> 程度解锁新体验）**——正好把已建的 episodic 记忆、intimacy_engine、变现目录三个半成品串通。
> 用户拍板：①→②→③ 顺序都做，逐阶段实现+回归+报告。

### Phase ① · 长期记忆深化激活 ✅

**勘探结论（关键）**：episodic 记忆引擎**早已全量写好且全链接线**——
`EpisodicMemoryStore`（R2–R17：salience/分层 stable/复发 hits/矛盾消解/新证据推翻 stable/
来源分级/近义去重/向量融合/画像）+ `skill_manager` 注入·抽取·巩固·补嵌入·后台 CRUD +
`proactive_topic.select_proactive_topic`（回访高置信记忆开场）+ `main._maybe_start_companion_proactive`
全部就绪。**「深化」不是重建，而是激活 + 修配置漂移 + 防再漂移**。

**发现的真实缺陷（配置在、代码读不到）**：
1. **salience 键名漂移**：`companion.yaml` 写 `memory.salience.enabled`，但代码读
   `memory.salience_rerank.enabled` → 情绪显著性重排这条护城河特性被**静默关掉**。
2. **记忆驱动主动开场未激活**：预设未设 `companion.proactive_topic.enabled` →
   「主动惦记你说过的事」（记忆护城河最直观体现）在旗舰预设里休眠。
3. **语义向量召回**：预设开了 `semantic_dedup`（行内已生成 embedding）却没开
   `memory.vector.enabled` → query 侧不嵌入、存好的向量从不用于检索（且需 embed-capable
   provider，deepseek-chat 无嵌入端点）→ 留为带说明的注释 opt-in，不强开（守自治）。

**改动**：
- `skill_manager.resolve_salience_rerank_cfg(memory_cfg)`：单一事实源解析器，**容忍
  `salience_rerank` / `salience` 两种键名**（规范键优先），inject 路径改用之 → 修复静默失效，
  且对历史误写向后兼容。纯本地启发式重排，**零 API 成本、零行为风险**（默认仍关）。
- `config/presets/companion.yaml`：键改回规范 `salience_rerank`；新增
  `companion.proactive_topic.enabled: true`（min_silent_hours 24 / scan_limit 200）激活记忆
  驱动主动开场；vector 语义召回留注释 opt-in + 降级说明。
- **防漂移契约测试** `test_companion_preset_memory_contract.py`：把「预设激活意图」与
  「代码真实读取口径」绑定（salience 经 `resolve_salience_rerank_cfg` 判定 + proactive_topic
  键名与 main 一致 + 巩固三件套 resolve/supersede/source_aware），任一侧再漂移即红——
  直击 AGENTS.md「文档/配置落后于代码」教训。

**测试**：✅ 新增 5 测试；全量 **5728 passed / 31 skipped / 0 fail（186s）**。

**实施中的判断**：
1. **不强开 vector**：语义召回需嵌入模型=外部资源，强开会让无嵌入 provider 白付 embed 成本；
   留 opt-in 注释守「全自治」边界，与北极星「翻译/多模态只补到不被否决」一致。
2. **代码侧做别名而非只改预设**：别名解析器同时修复任何历史 config.yaml 的同款误写，
   而不仅修一个预设文件——根治而非补丁。
3. **契约测试绑定代码口径**：直接 import `resolve_salience_rerank_cfg` 喂预设，避免测试里
   复刻解析逻辑造成二次漂移。

**下一阶段**：Phase ② 端用户可见关系成长系统（亲密度等级/里程碑 + 咬合变现解锁；
后端 journey_fsm/relationship_stager/intimacy_engine 已 50%，缺端用户可见进阶面 + 解锁联动）。

---

## 45. Phase ② · 关系成长系统（Bond Level）——让用户「看见关系在变深」

竞品对标（星野/Replika/Talkie）：陪伴留存的核心是**可见的成长**（等级 + 进度条 + 里程碑 +
按级解锁）。本仓已有 `intimacy_score`（0-100）与规范阶段（`companion_relationship`：
initial/warming/intimate/steady，阈值 25/55/80），但只是**运营侧信号**；缺端用户可见的成长机制。

**勘探结论**：阶段/阈值/中文标签已有**单一事实源** `companion_relationship`（含
`derive_stage_from_intimacy` / `STAGE_ORDER` / `STAGE_LABEL_ZH` / `INTIMACY_BAND_DEFAULTS`）。
另有 `relationship_stager._intim_band` 是平行的第二套 band 命名——为**不再增第三套**，本期
全部 import 自 `companion_relationship`，等级即「阶段序号 + 段内进度」。

**改动**：
- 新增纯函数模块 `src/contacts/relationship_level.py`：
  - `compute_bond_level(score)` → `{level 1..4, stage, name, progress, score_to_next, next_name, is_max}`，
    阶段判定**直接调** `derive_stage_from_intimacy`（零阈值漂移）。
  - `bond_milestones(intimacy/days_known/turn_count_in)` → 已达成里程碑（相识时长/交心句数/升级）。
  - `level_unlocks(level, unlock_map)` → 按等级**累计解锁预览**（键支持 1..N 或阶段 code）；
    **不做付费判定**，只回答「关系深度配解锁什么」，绝不作绕过付费后门。
  - `build_bond_level_block(...)` → 克制的【关系进展】prompt 块：仅 intimate/steady 或刚达成
    里程碑时给一句背景，让 AI 自然流露关系厚度而非游戏化播报；initial/warming 无里程碑 → 空。
- **透出（看得见）**：`/api/relations/health/{jid}` 单卡新增 `bond`（含 days_known + milestones）；
  `/api/relations/health-board` 各行带 `bond_level/bond_name/bond_progress`；`relations_health.html`
  榜单亲密度列追加等级名、单卡 modal 顶部加「💞 阶段 LvN（进度%）· 最近里程碑」。
- **接 AI 回路**：`skill_manager` 注入 `_bond_level_block`（默认关 `companion.bond_level.enabled`），
  `ai_client` 在 companion 域消费，`context_store` 加入 per-request 非持久键白名单。
- **配置**：`config.example.yaml` 加 `companion.bond_level`（默认关 + unlocks 注释示例）；
  `companion.yaml` 预设默认开 + 给 intimate→exclusive_album / steady→all_story 解锁预览。
- **测试**：新增 `test_relationship_level.py`（16）+ 健康卡/榜单透出 2 测 + 预设契约 bond 断言；
  全量 **5747 passed / 31 skipped / 0 fail（222s）**。

**实施中的判断**：
1. **复用规范阶段而非造第三套 taxonomy**：阈值/标签全 import `companion_relationship`，单测断言
   每级 stage 与 `derive_stage_from_intimacy` 完全一致——杜绝「又一套 0-25-55-80」漂移。
2. **解锁是预览非门控**：`level_unlocks` 明确不替代 monetization tier gating，防把「关系深度」
   误用成绕过付费的后门（守住已建变现的收口）。
3. **prompt 块极度克制**：陌生/升温期不谈深度（免越界油腻），只在深关系或纪念点给一句，
   且不让 AI 像 NPC 宣告等级——陪伴口吻优先于游戏化。
4. **days_known 来自 journey.created_at**：测试环境 created_at=now 故时长里程碑不触发（已在测试中
   规避断言），生产环境首见即建档、随时间自然累积。

**下一阶段**：Phase ③ 剧情/场景 roleplay 引擎——填补变现目录里 `story_ch1`/`all_story` 空占位，
与本期 bond_level 的 steady→all_story 解锁天然咬合，形成「记忆→成长→剧情解锁」完整闭环。

---

## 46. Phase ③ · 剧情/场景 roleplay 引擎——填补变现空占位，闭合「记忆→成长→剧情」链

竞品对标星野/Talkie/筑梦岛：场景化剧情是陪伴高粘性玩法 + 天然付费解锁点。本仓变现目录
早埋 `story_ch1`/`all_story` 占位却**无引擎驱动**（Glob 全仓无 story/scenario 模块）。本期补齐。

**改动**：
- 新增纯函数引擎 `src/skills/story_engine.py`（确定性、零 IO/LLM/网络）：
  - 场景剧本声明在 `config.companion.story.scenarios`（title + beats[directive] + 双 gate）。
  - `scenario_locked_reason/available` **双 gate**：`min_bond_level`（关系等级，咬合 Phase ②）
    + `require_unlock`（付费 feature，走 `monetization.entitlement_allows`——**不另造、不绕过**收费）。
  - `start_scenario` / `advance_state`（按用户轮次确定性推进 beat，剧终自动收场）/
    `build_story_prompt_block`（每 beat 一行【剧情场景】导演指令）。
  - state 是可序列化 dict，由调用方持久化（user_context["story_state"]，与 companion_relationship 同范式）。
- **skill_manager 端到端接线**（默认关 `companion.story.enabled`）：
  - `_handle_story_command`：用户发「剧情列表 / 开始剧情 X / 结束剧情」→ 列表带准入状态、
    开始过双 gate 置 state、结束清 state；锁定时给温暖话术（区分需升级/需解锁）。
  - 活动剧情时注入 `_story_block`（per-request），`ai_client` 在 companion 域消费；
    `_update_after_reply` 按轮次推进 beat，`context_store` 加 per-request 白名单（state 仍持久）。
- **配置**：`config.example.yaml` 加 `companion.story`（默认关 + coffee_date 样例）；
  `companion.yaml` 预设默认开 + coffee_date（免费·需 bond≥2）+ starry_night（付费·需 all_story + bond≥3）。
- **测试**：`test_story_engine.py`（12，引擎）+ `test_story_command_wiring.py`（9，接线/双 gate）
  + 预设契约 story 断言；全量 **5769 passed / 31 skipped / 0 fail（206s）**。

**实施中的判断**：
1. **付费 gate 复用 entitlement_allows 而非自造**：剧情解锁直接查既有权益（unlock/grant），
   守住 K2 变现收口；关系 gate 复用 Phase ② 的 bond_level——三期真正咬成一条链。
2. **指令触发 + 状态自管，先不依赖跨仓 MiniApp**：用对话内「开始剧情」命令即可端到端跑通
   （仿 episodic forget 命令），entitlement 缺省按免费处理→付费场景不会被白嫖。
3. **导演指令交回复层演绎、不报幕**：prompt 块明确「沉浸演绎、跟随对方节奏、别旁白报章节」，
   保陪伴口吻而非游戏化播报；推进确定性（轮次驱动）便于单测与可控成本。

**三期链路达成**：记忆（记得住，Phase ①激活）→ 成长（看得见关系变深，Phase ② bond_level）→
剧情（深到一定程度解锁场景，Phase ③ story_engine，steady+all_story→starry_night）已串通闭环。

## 47. Phase ④ · 把「记忆→成长→剧情」单向链**做成正循环**（分支多结局 + 完成回写共享记忆）

**立项判断**（重扫确认未开发：Glob/grep `story_engine.py` 仅线性 beat、2-tuple advance、无 branch/ending/回写）：
Phase ③ 把三件事串成一条**直线**。本期「做深」的关键 insight——**把直线做成正循环**：
剧情完成 → 把「共享经历」写回情景记忆 → 被 consolidate 晋升 stable + 被 `proactive_topic`
日后主动回访（"还记得那次星空下的约定吗？"）→ 关系更深 → 解锁更深剧情……记忆/成长/剧情
互相喂养。**且回写到 episodic 后，proactive_topic 既有逻辑零改动即可消费**（它本就读 episodic facts）。

**改动**：
- `story_engine.py` 演进（仍纯函数、向后兼容 list beats）：
  - **分支多结局**：beat 可设 `branch`（关键词→`ending` 路由）+ `default_ending`；`endings` 字典
    （id→{directive, memory}）。`_match_branch` 按用户回应确定性选路（无命中走 default）。
  - **完成回写**：`advance_state` 改返回 `(new_state, finished, memory)`——剧终返回该结局
    （或 `on_complete`）声明的「共享经历」文本；引擎只**返回**、由调用方落库（保持零 IO）。
  - state 加 `ending_id`（非空=已进入结局段）；`current_directive`/`build_story_prompt_block`
    结局段取 endings 并打「·结局」标。
- **skill_manager 接线**：
  - `_update_after_reply(... user_msg=text)`：把用户当轮原话传入 → 驱动 `branch` 路由。
  - 新增 `_writeback_story_memory`：剧终非空 memory → `add_fact(key, mem, "story", source="user_stated")`
    （共享经历在虚构里真实发生过=高置信，可晋升 stable / 被主动回访；add_fact 内容哈希去重，
    重复收场不灌水；任何失败不打断回复管线）。
- **配置**：`companion.yaml` 预设 coffee_date 末 beat 加 `ask` 选择点 + warm/cool 双结局（各带不同回写记忆）、
  starry_night 加 `on_complete.memory`；`config.example.yaml` 同步注释 branch/endings/on_complete schema。
- **测试**：`test_story_engine.py` 升级到 3-tuple + 加分支路由(warm/cool/default)/结局段/回写文本断言；
  `test_story_command_wiring.py` 加 `_writeback_story_memory` 写库（user_stated/label=story）与空保护断言。
  story 相关 34 passed；**全量 5775 passed / 31 skipped / 0 fail（191s）**。

**实施中的再优化**：
1. **不另造「故事→记忆」回写格式，直接落 episodic 既有 add_fact + source=user_stated**：
   于是 consolidation（晋升 stable）与 proactive_topic（主动回访）**全部零改动复用**——
   这正是把链做成循环、却几乎不增接线面的关键。曾考虑独立「共享经历表」，否决（割裂记忆栈）。
2. **branch 只路由到 ending（不路由到任意 beat）**：覆盖「我的选择改变结局」核心体验，
   又不引入图遍历复杂度/死循环风险；list beats 向后兼容（不配 branch 即纯线性，旧场景不受影响）。
3. **回写走 user_stated 而非 ai_inferred**：共享经历在剧情里真实发生，按高置信入库才能被晋升与
   主动回访；但仍受 add_fact 去重与 prune 上限约束，不会污染/灌水记忆。

**做成循环后的链路**：记忆①→成长②→剧情③→（剧终回写）→记忆① → proactive_topic 主动回访 →
关系更深② → 解锁更深剧情③……四期把单向链闭成自我强化的飞轮。

## 48. Phase ④续 · 把飞轮真正转起来——剧情→成长加成 + 主动回访偏好共享经历

**立项判断**（勘探确认两条边都未接实）：
- `intimacy_score` 由 IntimacyEngine 拥有、写在 `journeys` 表（contacts/gateway 路径），
  **skill_manager 无直写口**——上期遗留的「剧情→成长」是悬空的。
- `proactive_topic._fact_score` 只按 stable/hits/recency 排序，**不认 category**，
  共享经历回写后并不会被优先回访。

**改动**：
- **剧情→成长（autonomous，不动事实源）**：engine `advance_state` 收场 payload 升级为
  `{"memory", "intimacy_bonus"}`（endings/on_complete 可声明 `intimacy_bonus`）。skill_manager
  新增 `_apply_story_intimacy_bonus`：把加成累加进 `rel_state.story_bonus`（随 user_context
  持久、按 chat 维度、`max_intimacy_bonus` 封顶防刷）；`_effective_intimacy` = 基础
  intimacy + story_bonus（封顶 100，无基础信号则返回 None 不臆造）。`_bond_level_from_context`
  与 bond_level prompt 块改用 effective —— **完成深度剧情真实推动 bond 等级 → 解锁更深剧情**。
- **记忆→主动回访（pure，opt-in）**：`proactive_topic` 加 `prefer_category` 参数，命中类目
  （`story`）的事实领先一档；`build_proactive_opener` 从 `companion.story.proactive_prefer_category`
  （默认 `story`）传入。共享经历 `category="story"` 由 Phase ④ 回写时写入 → 沉默回归时
  AI 优先「还记得我们一起看过的星空吗」开场。默认 `""` 行为等同旧版（零回归）。
- **配置**：预设/example 给 endings 补 `intimacy_bonus`、story 段补 `max_intimacy_bonus` +
  `proactive_prefer_category`。
- **测试**：engine 3-tuple→payload 化 + 加成断言；wiring 加加成累加/封顶/按 chat 隔离/无基础
  信号保持 None；proactive 加 prefer_category 抬升/无命中回退/默认不变三测。story 相关 66 passed；
  **全量 5782 passed / 31 skipped / 0 fail（222s）**。

**实施中的再优化**：
1. **认清 intimacy_score 不可直写 → 改用「独立累加项」而非 hack 事实源**：曾想在
   user_context 直接改 intimacy_score，但 runner 每轮会用 IntimacyEngine 值覆盖它 → 不持久。
   改存 `rel_state.story_bonus` 独立项、在 `_effective_intimacy` 处叠加：既不篡改 IntimacyEngine
   事实源、又持久生效，且后续 RPA/journey 侧若要接真加成也不冲突。
2. **`_effective_intimacy` 无基础信号返回 None（不臆造关系）**：纯命令/无亲密度追踪的路径
   不会凭空冒出 bond 块，保 Phase ② 在该路径的休眠现状不变。
3. **prefer_category 做成参数、策略留在 wiring 层**：纯函数保持中立可测，是否偏好 story 由
   config 决定，默认空=零回归——这条「记忆→主动回访」边可随时关。

**飞轮闭环验收**：完成 starry_night（+5）/coffee warm（+4）→ story_bonus 累加 → effective
intimacy 抬升 → bond 升级 → 解锁更深剧情；同时共享经历入 episodic（story 类目）→ 沉默回归
被 proactive_topic 优先回访。记忆/成长/剧情三系统互相喂养的正循环已端到端跑通且全程可测。

## 49. Phase ④续² · 飞轮质量与情感硬化（防刷一次性加成 + 首次完成关系纪念点）

**立项判断 / 主动改方向**：上轮建议「健康卡叠加 effective intimacy（#1）」，本轮勘探确认
**`story_bonus` 仅存 user_context，而健康卡 bond 取自 journey 表（IntimacyEngine）——
skill_manager 无 contacts/journey 句柄**，要在 ops 卡片叠加 effective 必须跨库桥接
（contact↔user_context 或写 journey 事件），属上轮已明确延后的跨模块工程；半吊子桥接脆弱、
价值低（ops 视图美观问题、非用户侧护城河）。故**透明改做**两项全自治、且直接强化刚建好飞轮
质量的改进；同时确认 proactive 循环本就有 72h/会话冷却，回访重复天然受限，「新鲜度」不是缺口。

**改动**：
- **防刷·一次性加成**：新增 `_record_story_completion`——`rel_state.story_done` 记已完成场景；
  首次完成才结算 `intimacy_bonus`，重复完成归零（记忆仍照常回写、复发自然累积，但关系深度只认
  「真实的新经历」，杜绝刷同一剧情冲等级）。`advance_state` 收场改走此口（不再裸调
  `_apply_story_intimacy_bonus`）。
- **情感闭环·完成纪念点**：首次完成 → 置一次性 `user_context["bond_fresh_milestone"]
  = "story:一起经历了《<剧情名>》"`；bond 块注入处**一次性消费并清除**（pop），下一轮 AI 自然
  致意（"我们刚一起经历了那次约会，感觉离你更近了"）——把「剧情→成长」从数字变成可感知的真情
  流露。`relationship_level._milestone_label` 认 `story:<人话标签>` 形态（标签随码透传，不耦合剧情表）。
- **测试**：wiring 加首次加成+置纪念点 / 重复完成不刷分不置点 / `story:` 码标签解析三测；
  全量 **5785 passed / 31 skipped / 0 fail（245s）**。

**实施中的再优化**：
1. **认清 #1 是跨模块、果断改道而非硬桥**：勘探 `grep` 实锤 skill_manager 无 journey 句柄后，
   不做脆弱的 contact↔user_context 反查，转做两项全自治改进——符合「有更好替代就优化」。
2. **防刷不牺牲记忆复发**：重复完成仍回写记忆（dedup 累加 hits→利好 consolidation 晋升），
   只掐 intimacy 加成——既挡刷分、又让「重温喜欢的剧情」依然强化共享记忆，体验不打折。
3. **纪念点用一次性 pop 而非常驻标志**：致意只出现在剧情收场后的下一轮，自然不啰嗦；
   `_milestone_label` 走「码携带人话标签」而非在纯函数里塞剧情字典，守住模块零耦合。

**遗留（明确下一步候选）**：健康卡/看板 effective intimacy 统一 = 需把 story_bonus 经
gateway/IntimacyEngine 写成 journey 事件（跨模块，单独立项）；端用户 MiniApp 接 bond/story/checkout
（跨仓前端）。

## 50. Phase ④续³ · 关系/成长面板（对话内一屏看见整条链的成长）

**立项判断 / 主动改方向**：上轮建议下一步做「真·journey 加成」统一 ops 数据面。本轮勘探
（读 `intimacy_engine.py` + `rpa_hooks.py`）确认：IntimacyEngine 是 **event-stream-is-truth**
（journey_events 聚合 0-100），contacts hooks **挂在各 runner、不在 skill_manager**；要把 story
加成沉到 journey 须新增 event 类型 + 改 IntimacyEngine 打分 + 经**多个 runner** 发事件 + store
迁移，且动的是喂 reactivation/funnel/handoff 的核心分数——**高面积高回归风险、却只换来 ops 视觉
统一（非用户侧护城河）**。按「有更好替代就优化」纪律，**透明延后** journey 统一，转做用户侧
价值更高且全自治的一环：把整条「记忆→成长→剧情」链的进度做成**对话内可查的成长面板**——
这正是当初建链要给端用户的回报，也是 MiniApp 面板的「无前端」先行版。

**改动**（纯新增指令，零改既有逻辑）：
- `_handle_growth_command`（接在 `_handle_story_command` 后短路链）：触发词
  「我们的关系 / 关系进度 / 成长 / 我的等级 / 我们的故事 / /status …」→ 一屏返回：
  - 💞 当前 bond 等级 + 名称 +（距下一级）；进度条 `▮▮▮▯▯…` + 百分比（`compute_bond_level`）；
  - 🌱 已达成里程碑（`bond_milestones`：相识时长 + 升级）；
  - 📖 剧情足迹：一起经历过（`rel_state.story_done`）/ ✨ 还能一起经历 / 🔒 待解锁
    （`list_scenarios` 复用双 gate，不绕付费）；
  - 🎁 当前等级解锁预览（`level_unlocks`，配 bond_level.unlocks 才出）。
- 用 `_effective_intimacy` 取数 → 与对话面 bond 完全一致（含剧情加成）；非陪伴域/未启用 → 返回
  None 不劫持；空亲密度 → 温和兜底「刚认识不久」。
- **测试**：新建 `test_growth_command_wiring.py` 8 测（触发/非域不劫持/等级进度/剧情足迹
  经历过·可玩·待解锁/解锁预览/低分兜底/进度条边界）；全量 **5793 passed / 31 skipped / 0 fail（278s）**。

**实施中的再优化**：
1. **代码实锤后果断改道**：读 IntimacyEngine + hooks 确认 journey 统一是跨多 runner 改核心分数，
   不硬上；改做用户侧 ROI 更高的对话面板——且它顺带给了「成长可见」这件事的真正落点（用户而非 ops）。
2. **面板全程复用既有纯函数**（compute_bond_level/bond_milestones/list_scenarios/level_unlocks），
   零新增业务逻辑、零写库——把链的「读」侧收成一个口，drift 风险最低。
3. **取数走 `_effective_intimacy`**：面板显示的等级与 AI 对话感知、与剧情解锁门槛三者同源一致，
   不会出现「面板说 Lv2、AI 当 Lv3」的新割裂。

**遗留**：journey 侧 effective 统一（ops 看板，跨模块）；端 MiniApp（跨仓前端）——面板已先用
对话内形态交付了其用户价值。

## 51. Phase ④续³ · 剧情跨场景因果（requires_story）——把孤立剧情连成有因果的故事线

**立项判断**（勘探确认未开发：`story_engine` 仅 min_bond + require_unlock 两道 gate，无任何「前置
剧情/结局」依赖；`_record_story_completion` 只记 `story_done` 列表、不存结局）。竞品（恋与/筑梦岛）
的章节制叙事靠「前一章的选择决定后一章」吸住用户。本期补这条「剧情→剧情」的因果边。

**改动**：
- `story_engine`：新增第三道 gate `requires_story`（AND 语义）：
  - `{scenario: X}` = 完成过 X 即可；`{scenario: X, ending: warm}` = 须以 warm 结局完成。
  - `_story_prereq_unmet(scn, completed)` 纯判定；`scenario_locked_reason` 判定顺序
    **关系 → 前置剧情 → 付费**（越友好/可行动者优先：缺前传提示「先经历《X》」而非直接报付费）。
  - `completed` = `{scenario_id: ending}` 经 `scenario_available/list_scenarios/start_scenario` 全程透传。
- `skill_manager`：
  - `_record_story_completion` 加 `ending` 形参，落 `rel_state.story_outcomes[sid]=ending`
    （首次/重复都刷新最近结局，供因果 gate）；advance 收场处捕获 `_sstate.ending_id` 传入。
  - `_story_outcomes` 取结局足迹；`_handle_story_command`（列表/开始）与 `_handle_growth_command`
    面板全程传 `completed`，锁定时给「经历过《前传》后解锁」温暖话术；`_scenario_title` 解析前传名。
- **配置**：预设 `starry_night` 升级为续作——`requires_story: [{scenario: coffee_date, ending: warm}]`
  （须以 warm 结局走过咖啡约会）+ 原 bond3 + all_story；example 同步 schema 注释。
- **测试**：engine 加 缺前置/错结局/前置先于付费判定/start 拦截 4 测；wiring 加 结局落库/续作前
  锁后解/错结局仍锁/列表前传提示 4 测；契约测随之喂满前置。全量 **5801 passed / 31 skipped / 0 fail**。

**实施中的再优化**：
1. **gate 判定顺序「关系→前置→付费」**：缺前传时优先提示「先经历《前传》」（可行动），而非
   一上来报「需付费」劝退——叙事引导优先于变现话术，且 require_unlock 仍最后兜底不被绕过。
2. **结局足迹与「共享记忆」双轨**：因果 gate 用结构化 `story_outcomes`（机器判定），而剧情连续感
   仍由既有「warm 结局回写的共享记忆」经 episodic 自然带出（AI 在续作里能提起前传）——结构解锁 +
   情感延续两条线并行，不互相耦合。
3. **首次/重复都刷新 outcome**：重温前传改走另一结局后，续作 gate 立即按最新结局重判，符合直觉。

**叙事链成形**：coffee_date（warm 结局）→ 解锁 starry_night；记忆/成长/剧情飞轮之上再叠一条
「剧情→剧情」的有向因果，章节式留存钩子成型。**遗留**：分支结局的「多分支树」（A 结局解锁剧情 X、
B 结局解锁剧情 Y）现已天然支持（按 ending 配不同续作即可），可在内容侧扩展。

## 52. Phase ④续⁴ · 真·journey 加成统一（剧情加成镜像进 journey → 运营健康卡 effective bond 对齐）

**立项判断**（兑现 ## 50 遗留的「journey 侧 effective 统一」）。先做有界勘探，结论改写了原方案：
- **割裂确为真**：主平台 Telegram(A 线) 经 `companion_context.resolve_intimacy_score`/`record_relationship_message`
  接入 contacts journey；但剧情加成只活在会话侧 `rel_state.story_bonus`（`_effective_intimacy` 叠加）→
  运营关系健康卡读 `journey.intimacy_score`（基础分）算 bond，**比用户对话内体感低**。
- **不碰核心评分**：`IntimacyEngine` 喂 reactivation/funnel，把加成烤进 `intimacy_score` 既有回归风险、
  又会与会话侧 shim **双算**。故定方案 C：加成**不进** intimacy 事实源，改走 journey 事件流镜像 +
  健康卡用**同一公式**（base + 封顶 story bonus）派生 effective bond。IntimacyEngine / reactivation /
  funnel / 会话路径**全部零改动**，无 ALTER TABLE。

**改动**：
- `contacts/gateway.py`：`record_story_completion(...)` → `on_peer_seen` + `append_event("story_complete", {scenario/ending/bonus/title})`；
  **刻意不重算/不写 intimacy_score**（保持事实源纯净）。
- `contacts/rpa_hooks.py`：`GatewayContactHooks.on_story_complete`（委托 gateway，吞异常）+ 协议声明 + Noop 占位。
- `utils/companion_context.py`：新增进程级 `story` provider + `record_story_completion(...)`（与既有
  intimacy/funnel/record 同一惰性注册座；未注册 → no-op 返回 False，零行为变化）。
- `main.py`：`story_recorder=hooks.on_story_complete` 与 `message_recorder` 同闸——**仅 contacts_recording 开**时注册。
- `client/telegram_client.py`：`_sm_context` 注入 `account_id`（与 resolve_intimacy 同一寻址，供镜像定位 journey）。
- `skills/skill_manager.py`：`_record_story_completion` 首次收场分支调 `_mirror_story_completion_to_journey`
  （best-effort：缺 account_id/provider → 跳过；会话侧加成已独立权威生效，不依赖镜像）。**仅首次**镜像 ⇒
  每场景至多一条 story_complete 事件 ⇒ 健康卡 sum 恰等于会话侧累计 story_bonus（同源同量、互不叠加）。
- `web/routes/contacts_routes.py`：`_story_bonus_from_events`（聚合封顶，cap 读 `companion.story.max_intimacy_bonus`
  同源）+ `_bond_for(..., events=)` 用 effective 分派生等级/进度/里程碑，单卡 + 预警榜均透出 `story_bonus`/`effective_intimacy`。
- **测试**：companion_context 5 测（注册/参数/None/空输入短路/吞异常）；gateway 3 测（落事件/不动 intimacy/负值钳零）；
  路由 4 测（健康卡反映/封顶/无事件零变化/榜单透出）；wiring 3 测（首次镜像参数/重复不镜像/缺 account_id 跳过且会话侧仍生效）。
  全量 **5816 passed / 31 skipped / 0 fail（215s，+15 测）**。

**实施中的再优化**：
1. **从「烤进 intimacy_score」改道「事件镜像 + 健康卡同公式」**：勘探发现前者既动 reactivation/funnel
   评分（用户明确担心的回归面）、又与会话侧 shim 双算。改道后**风险面归零**——事实源不动、会话路径不动，
   只是健康卡多读一类既有事件，新行为仅「story-active 用户 bond 显示更高」（正是统一目标）。
2. **复用既有 provider 座 + contacts_recording 同闸**：不新造 bootstrap 接线，沿用 N 线已验证的惰性
   provider；默认关 → 零影响，遵循「新子系统默认关」。
3. **首次镜像 = 防刷与对账双赢**：与会话侧 `story_done` gate 同点触发，天然保证「健康卡聚合 == 会话累计」，
   无需第二套封顶/去重逻辑，cap 也从同一 config key 取，杜绝两侧漂移。
4. **best-effort 且非阻断**：镜像失败/未寐线（B 线/RPA 未注入 account_id）一律跳过，会话侧加成始终生效——
   统一是「锦上添花的 ops 可见性」，绝不成为回复链路的新依赖。

**统一闭环**：用户在对话内看到的 bond（含剧情加成）= 运营在健康卡/预警榜看到的 effective bond，同源同公式同封顶。
**遗留**：① B 线/各 RPA runner 若要同享镜像，仅需在其 context 注入 account_id（一行级，按需）；② 端 MiniApp
（跨仓前端）仍待，但「成长可见」的用户侧（## 50 面板）与 ops 侧（本期）已双双落地。

## 53. Phase ④续⁵ · 主动剧情邀约——把「剧情解锁」接进沉默期 re-engagement 闭环

**立项判断**（勘探确认未开发：`build_proactive_opener` 仅回访 episodic 记忆，从不感知剧情；
主动循环 `_conversations()` 的 `intimacy/stage` 还**硬编码 0/""** → 开场连真实关系等级都没有）。
竞品（星野/恋与）最吃用户时长的是「章节解锁后主动召回」。本期把已落地的剧情系统接进沉默期主动开场：
**剧情解锁 → 主动邀约 → 回流 → 更多剧情**，让前面所有剧情投入转化为可量化的留存动作。

**改动**：
- `story_engine.py`：新增纯函数 `select_story_invite(scenarios, *, bond_level, completed, active_id, entitlement=None)`
  ——挑「关系/前置已满足 + 免费（`entitlement=None` 天然只放行 `require_unlock` 为空者）+ 未完成 +
  非进行中 + 有 beat」的场景，按声明序取第一个（内容侧可借声明序表达推荐优先级）。
- `skills/skill_manager.py`：
  - `_story_progress_from_context`（静态）：从持久化 user_context 的 `companion_relationship` **并集**所有
    rel_state 桶的 `story_done`/`story_outcomes`/`story_bonus`——绕开「proactive 用 memory_key 取 context、
    但 rel_state 按 chat_storage_key 分桶、键不必相等」的脆弱性，私聊一桶时等价、键错时也不误判。
  - `_proactive_story_invite(memory_key, intimacy)`：载 context → 汇总完成足迹 → effective intimacy
    （base + 封顶 story_bonus，与对话面/健康卡同源）→ `compute_bond_level` → `select_story_invite`
    → 温暖邀约 directive（`mode="story_invite"`）。story 未启/关 invite/无 context/无可邀约 → None。
  - `build_proactive_opener`：**优先**试剧情邀约（新内容钩子更强），无则无缝回落记忆话题；任何失败不打断。
- `main.py` `_conversations()`：用 N 线已就绪的进程级 `resolve_intimacy_score/funnel`（best-effort）
  把**真实 intimacy/stage** 注入快照——既修了记忆开场沉默阈值缩放失准的老问题，也让邀约能按真实等级判断。
- **配置**：`companion.story.proactive_invite`（默认 true；example 注释说明仅邀约免费已解锁场景、付费留店内引导）。
- 发送侧零改动：`mode` 一直只是采样标签，发文案由 `directive` 驱动（已验证）→ 邀约即「好 directive」。
- **测试**：engine 6 测（首选/bond 跳过/付费排除/前置解锁续作/完成+进行中跳过/空配置）；
  wiring 6 测（可邀约/完成回落记忆/bond 不足回落/加成顶过门槛解锁/invite 关/story 关）。
  全量 **5828 passed / 31 skipped / 0 fail（240s，+12 测）**。

**实施中的再优化**：
1. **顺手修了主动循环的硬编码 intimacy=0**：这是个潜伏 bug——记忆开场的沉默阈值/克制修饰本应随关系
   缩放，却一直拿 0。复用现成 `resolve_*` provider 一并补上，邀约与记忆开场双双受益（一次改动两处增值）。
2. **rel_state 跨桶并集 > 精确键寻址**：与其赌 memory_key==chat_storage_key（群聊/历史键会错），不如
   并集所有桶——私聊等价、错键也安全，鲁棒性换极小开销，杜绝「误邀已完成剧情」这个最伤体验的坑。
3. **`entitlement=None` 即「只邀免费」**：不在沉默期隔空 teasing 锁住的付费内容（劝退/掉好感），付费解锁
   交给店内高意图场景；一个默认参数把策略表达干净，无需额外分支。
4. **优先邀约、无缝回落**：邀约是更强的新内容钩子，但自限——开始/完成后即不再可邀约 + 72h 冷却仍生效，
   不会变成骚扰；无可邀约时静默回落记忆话题，绝不空转。

**飞轮再加一圈**：记忆→成长→剧情→（完成）记忆/成长 的内循环之外，再接出「剧情解锁→沉默期主动邀约→回流」
的**留存外循环**。**遗留**：① 邀约话术可做 A/B（few-shot 已有 mode 分桶基建，加 `story_invite` 桶即可）；
② 续作/分支结局解锁的「专属召回」（带上前传结局的个性化邀约）；③ 付费场景的「解锁预告」式召回（区别于免费邀约）。

## 54. Phase ④续⁶ · 个性化召回——续作邀约织入前传共同经历（把因果链接进召回话术）

**立项判断**（兑现 ## 53 遗留①②）。通用邀约「要不要一起经历《X》」对续作偏空泛；竞品的高黏召回都带
「上次我们…」的回忆钩子。本期把已落地的因果链（`requires_story` + `story_outcomes` 结局足迹）织进邀约：
续作召回自然提起**前传标题 + 那次结局回写的共享经历**，让召回有据可依、像老朋友而非系统推送。

**改动**：
- `story_engine.py` 两个纯函数：
  - `satisfied_prerequisite(scn, completed)` → 该场景**已满足**的首个前置 `(scenario_id, ending_taken)`；
    复用 `requires_story` 的 AND 解析，仅在 `select_story_invite` 判可邀约后调用（命中即满足）。
  - `ending_memory(scn, ending_id)` → 取某结局回写的「共享经历」文本（无结局→`on_complete` 兜底→`""`）。
    续作引用前传那次的真实共同经历（"我们约好下次再一起喝咖啡…"），不空泛。
- `skills/skill_manager._proactive_story_invite`：选中场景若是续作且用户走过前传 →
  `callback = 《前传标题》（前传结局共享记忆）`，directive 改为「先自然提起上次那段经历、再顺势邀续作」；
  非续作仍用通用温暖邀约。复用既有 `_scenario_title`。
- `integrations/companion_sample_store._MODE_HINTS`：补 `story_invite` 调参建议
  （邀约太硬/续作召回是否提前传/打扰感强时上调 cooldown）——few-shot 与看板已**按 mode 自动分桶**，
  无需改采样/分桶代码，`story_invite` 桶随 ## 53 emit 即已生效，本条只补人审提示文案。
- **测试**：engine 6 测（满足/无 requires/未完成 → prereq；ending/on_complete 兜底/缺失 → memory）；
  wiring 1 测（续作邀约 directive 同时含前传标题 + 该结局共享记忆 +「续作」）。
  全量 **5835 passed / 31 skipped / 0 fail（247s，+7 测）**。

**实施中的再优化**：
1. **召回钩子复用结局回写的共享记忆文本**：不另写一套"前传摘要"，直接取 `endings[ending].memory`——
   与剧情完成时回写情景记忆、与 proactive 记忆回访三处**同一句文本**，口径统一、零维护漂移。
2. **个性化是叠加而非分叉**：续作有回忆钩子、普通剧情走通用邀约，同一函数一个 if 分流，
   不为个性化新开路径；失败（缺 title/memory）自然回落通用话术，鲁棒。
3. **few-shot/看板零改动复用 mode 分桶**：确认 `build_few_shot_block(mode=)` 与 `stats.by_mode` 已按 mode
   切桶 → `story_invite` 桶自动成立；只补 `_MODE_HINTS` 文案，避免重复造分桶轮子。

**召回质量闭环成形**：邀约（## 53）→ 个性化因果钩子（本期）→ 采样 `mode=story_invite` 落库 →
运营 👍/👎 → few-shot 反哺 `story_invite` 桶口吻 → 邀约更自然。**遗留**：① 付费场景「解锁预告」式召回
（区别免费邀约，接变现）；② 情绪自适应召回时机/语气（接 `wellbeing_guard`/危机信号，避免情绪低谷期推邀约——
规模化前的「主动打扰」安全护栏）；③ 邀约文案多模板/分支结局差异化召回。

## 55. Phase ④续⁷ · 情绪自适应召回护栏——情绪低谷/危机期不主动推「播放性」内容

**立项判断**（兑现 ## 54 遗留②，规模化主动召回前的**安全护栏**）。最高危场景：用户刚倾诉完痛苦/
流露危机信号，系统隔天却推个约会剧情邀约——这是会出大事的伤害性体验。本期在所有主动开场前加情绪闸门。

**改动**：
- `utils/wellbeing_guard.py` 新增纯函数 `proactive_emotion_gate(crisis_latest, *, now, window_days, last_emotion)`：
  依「最近危机事件 + 末条情绪」返回抑制档位——
  - `"block"`：窗口内 **severe**（自伤/轻生）→ 完全不主动打扰（交人工/关怀，AI 不发起）。
  - `"soft"`：窗口内 **elevated**（深度绝望）**或**末条明确负面情绪 → 抑制剧情邀约、仅留温和问候。
  - `""`：无抑制。保守：危机仅在 `window_days` 内计；异常一律按「不抑制」（护栏失效不应反向阻断关怀）。
- `skills/skill_manager.py`：
  - `_proactive_emotion_gate(memory_key)`：以 memory_key 反查 `crisis_event_store`（已就绪才查，复用
    既有 `crisis_summary_for_user` 的 user_id 前缀/chat_id 双匹配）→ 调纯函数判档；任何失败 → `""`。
  - `_proactive_crisis_window_days`：读 `companion.proactive_topic.crisis_guard_days`（默认 14）。
  - `build_proactive_opener` **顶部**先过闸：`block` → 直接返回空（连记忆问候都不发）；`soft` → 跳过
    剧情邀约、回落温和记忆话题；`""` → 原行为。护栏对**剧情邀约 + 记忆回访**两类主动开场统一生效。
- **配置**：`companion.proactive_topic.crisis_guard_days`（默认 14）；example 注释说明 severe/elevated 分档。
- **测试**：纯函数 8 测（block/soft/窗口内外/窗口可配/负面情绪/中性/危机优先/脏输入安全）；
  wiring 3 测（severe 全屏蔽/elevated 抑邀约留记忆/危机过期不抑制）。
  全量 **5846 passed / 31 skipped / 0 fail（244s，+11 测）**。

**实施中的再优化**：
1. **闸门置于 `build_proactive_opener` 顶部、护栏覆盖全部主动开场**：不只挡剧情邀约——severe 期连
   记忆问候也静默（此刻任何主动打扰都可能是冒犯）。安全护栏就该是「最外层、最先判」。
2. **纯函数 + last_emotion 形参预留**：危机信号（高精度、authoritative）本期接通；末条情绪（更广的
   「低谷」但更噪）以形参预留——pure fn 已支持并测，待 snapshot 接通 inbox 情绪即可零改逻辑启用。
3. **「异常 → 不抑制」而非「异常 → 抑制」**：护栏失效时宁可漏挡也不要误伤——一个崩溃的护栏不该把
   正常关怀也一起掐断（fail-open 关怀优先），与回复链路其它 best-effort 一致。
4. **复用 crisis_summary 的双键匹配**：不自造寻址，私聊（user_id 前缀）/群聊（chat_id）一个 key 通吃，
   与坐席工作台侧栏同源。

**安全边界成形**：主动召回链（## 53/54）外面套上情绪护栏——「该静默时静默」。**遗留**：① 末条情绪
接通 inbox（snapshot 注入 → soft 覆盖非危机低谷）；② 付费「解锁预告」式召回（接变现）；
③ severe 期可选「转人工/关怀升级」而非单纯静默（接 care 队列主动排程）。

## 56. Phase ④续⁸ · 危机关怀升级——severe 期把「静默」变「接住」（拦下的沉默用户排进 care 队列）

**立项判断**（兑现 ## 55 遗留③，安全闭环的最后一块）。## 55 让 severe 危机期的沉默用户「完全不主动」，
但**纯静默有盲区**：刚倾诉完痛苦、之后再没回来的用户，恰恰是最需要被「接住」的人——对他们静默 = 放任。
本期把「静默」升级为「转关怀」：被情绪护栏 `block` 拦下的会话不只跳过，而是排一条**高优先 care 待办**，
交关怀派发/坐席兜底主动回访。安全护栏从「不伤害」进到「主动接住」。

**改动**（零回归、IO 留在回调、纯函数仍纯）：
- `skills/skill_manager.py::build_proactive_opener`：`block` 档不再裸返回空，而是带可识别信号
  `{"mode": "", ..., "blocked": "crisis_severe"}`——`mode` 仍空（绝不会被当普通主动文案发出），
  `blocked` 字段供派发层识别「这是被危机护栏拦下的，需升级关怀」。
- `integrations/companion_proactive.py`：
  - `plan_proactive_sends` 新增可注入回调 `on_crisis_block(conv) -> None`：opener 返回
    `blocked == "crisis_severe"` 时调用（再正常跳过，不进发送计划）。**IO 留在回调里**（同
    `has_pending_care` 范式），纯函数本身不落库、可单测；回调抛错吞掉、不影响其余会话计划。
  - `CompanionProactiveLoop` 透传 `on_crisis_block` 进 `run_once`。
- `main.py`：装 `_on_crisis_block(conv)` 回调——经 `care_schedule.add_commitment` 排一条
  `topic=情绪关怀 / sentiment=negative / confidence=1.0 / due_at=now`（立即到期 → 下个派发 tick 即被
  关怀/坐席接住）的待办，`contact_key=conversation_id`（与 `_has_pending_care` 同键）。
- **幂等**：排进后 `has_pending_care(cid)→True`，下个 tick 该会话在计划**最前段整段让路**（早于 opener），
  天然不重排；care 项发出/过期后若危机窗仍在才会再排（续接关怀，合理）。
- **可观测预览**（`_proactive_preview`，dry-run）**不传** `on_crisis_block` → 预览绝不写 care 队列。
- **配置**：`companion.proactive_topic.crisis_care_escalation`（默认 `true`；整条主动循环本就被
  `enabled` 总开关 opt-in，故内部安全增强默认开）；需 care 子系统就绪（store 已挂 `web_app.state`）才生效。
- **测试**：plan 层 4 测（升级且不发/无回调仅跳过/普通会话不升级/回调抛错不阻断其余）+ loop 透传 1 测；
  ## 55 的 severe block 测加断言 `blocked == "crisis_severe"`。全量 **5851 passed / 31 skipped / 0 fail（252s，+5 测）**。

**实施中的再优化**：
1. **复用 `has_pending_care` 前置过滤做幂等去重**，不另造去重表：该谓词在计划循环**最前段**执行
   （早于沉默/冷却/opener），所以一旦排进 care，下个 tick 整段让路——零额外状态即得「排一次、不重排」。
2. **`blocked` 哨兵而非新增 `mode`**：保持 `mode == ""`（不发文案）这一既有契约不变，新信息走旁路字段，
   plan 层只多一处「先升级、再照常跳过」的判断，对所有现存 opener / 调用方零影响。
3. **升级落到 care 队列而非自造「人工 lane」**：care 子系统已有派发器（AI 关怀文案，**非**剧情播放）、
   运营看板、坐席接管、paywall——severe 用户进的是被监控、可人工接管的成熟通道，而非新孤岛。
4. **`due_at=now + confidence=1.0`**：危机关怀是最高优先，立即到期让它排在 care 队列最前，
   `confidence=1.0` 越过 `min_confidence=0.6` 门槛不被低质过滤误挡。

**安全闭环成形**：主动召回（## 53/54）→ 情绪护栏（## 55）→ **危机升级（本期）**，三段连成
「正常召回 / 该静默时静默 / 静默不够时主动接住」。**遗留**：① 末条情绪接通 inbox snapshot
（soft 覆盖非危机低谷，## 55 已预留形参，仅差 snapshot 注入）；② care 派发文案对 severe 来源做更
克制的语气模板（区别普通约定回访）；③ 付费「解锁预告」式召回（接变现）。

## 57. Phase ④续⁹ · 末条情绪接通 snapshot——情绪护栏从「只看危机事件」扩到「也看低谷」

**立项判断**（兑现 ## 55/56 遗留①，把护栏盲区补齐）。## 55 的情绪护栏 `proactive_emotion_gate`
早已预留 `last_emotion` 形参并测过，但 snapshot 一直没注入真实情绪——只有 `crisis_event_store`
里被记为 severe/elevated 的**危机事件**会触发护栏。盲区：用户没到「危机」线、但最近一条消息明显
负面（愤怒/不满/焦虑），系统仍可能推剧情邀约。本期把 inbox 已分析的末条情绪接进 snapshot，
让 `soft` 档覆盖「非危机但明显低谷」。**纯接线、护栏逻辑零改**（仅扩负面标签词典）。

**关键发现 + 修正**：inbox `conversation_meta.last_emotion` 存的是**中文**标签
（`愤怒/不满/催促/焦虑/平稳/满意/感谢`，见 `inbox/store.py::_EMOTION_ORDER`），而 ## 55 的
`_NEGATIVE_EMOTIONS` 只有**英文**标签——直接接通将**永不命中**。故同步扩 `_NEGATIVE_EMOTIONS`
纳入中文负面：`愤怒/不满/焦虑`（`催促`=不耐烦非低谷、`平稳/满意/感谢`=中性正面，均不计入，避免过抑制）。

**改动**（数据流：snapshot → plan → opener_fn → build_proactive_opener → 护栏）：
- `utils/wellbeing_guard.py::_NEGATIVE_EMOTIONS`：新增中文负面标签 `愤怒/不满/焦虑`（与 inbox 对齐）。
- `skills/skill_manager.py::build_proactive_opener`：新增 `last_emotion=""` 形参 → 透传给
  `_proactive_emotion_gate(memory_key, last_emotion)`（该函数 ## 55 已支持）。
- `integrations/companion_proactive.py::plan_proactive_sends`：`opener_fn` 调用补 `last_emotion=`
  （取自快照 `c["last_emotion"]`）；opener_fn 契约文档同步更新。
- `main.py::_conversations`：批量 `get_conv_meta_for_ids(cids)` 取末条情绪，写入快照
  `last_emotion` 字段；`_opener` 形参补 `last_emotion=""` 并透传。
- **测试**：纯函数 2 测（中文负面→soft / 中文中性正面+催促→不抑制）；plan 透传 2 测
  （快照 last_emotion 进 opener / 无字段默认空）；wiring 2 测（无危机仅中文负面→soft 抑邀约留记忆 /
  正面→仍可邀约）。全量 **5857 passed / 31 skipped / 0 fail（247s，+6 测）**。

**实施中的再优化**：
1. **发现并修复中英标签不匹配**（最关键）：若只接线不扩词典，功能形同虚设（中文情绪永不命中）——
   勘探 inbox 真实存储而非想当然，是「以代码实况为准」的直接兑现。
2. **`**_kw` 容错 opener 桩**：plan 给 opener 多传一个 kwarg，旧桩签名会 `TypeError` 被吞→全员
   误判空计划。把测试 opener 桩统一加 `**_kw` 既修当下、也为未来契约扩展免维护（向前兼容）。
3. **保守纳入负面标签**：只收明确低谷（愤怒/不满/焦虑），`催促`（不耐烦）刻意不收——主动召回宁可
   漏抑制也不要把「只是催我回复」误判为情绪低谷而长期静默该用户。
4. **复用既有批量取数**（`get_conv_meta_for_ids`，health-board 同款）：不为本功能新造查询，一次批量
   取全部 cids 的情绪，零额外逐行 IO。

**护栏盲区收口**：情绪护栏现同时看「危机事件（authoritative，window 内）」+「末条情绪（更广低谷）」。
**遗留**：① `last_emotion` 无 recency（取最新分析值，对沉默用户即「沉默前最后情绪」，语义正确，但若需
「仅近 N 天情绪才计」可后续接 `updated_at` 窗口）；② care 派发文案对 severe 来源克制语气模板（## 56 遗留②）；
③ 付费「解锁预告」式召回（接变现）。

## 58. Phase ④续¹⁰ · 危机关怀「克制陪伴」语气专线——把「接住」时说什么的最后一公里收口

**立项判断**（兑现 ## 56/57 遗留②，安全闭环的语义最后一公里）。## 56 把 severe 危机期的沉默用户
「接住」排进 care 队列，但 care 派发器对所有待办用的是**同一套**日常约定回访模板
（"你之前说的{topic}…怎么样啦？😊"）。对一个刚流露危机信号的人，套进 `topic=情绪关怀` 会生成
"你之前说的情绪关怀怎么样啦？"——既荒谬又冷漠，是**二次伤害**。本期给危机来源关怀切一条专用语气线。

**改动**（纯模板/分支层，零 schema 变更）：
- `contacts/care_schedule.py`：导出共享常量 `CRISIS_CARE_TOPIC = "情绪关怀"`——危机升级排队（main.py）
  与派发识别（care_dispatcher）两端共享同一保留 topic，不散落魔法字符串。
- `contacts/care_dispatcher.py`：
  - 新增 `_CRISIS_CARE_PROMPT`「克制陪伴」模板：不寒暄、不追问发生了什么、不让对方"汇报近况"、
    不提具体某件事、不催回复；只传"我在""不用急着回我""你不是一个人"的安静陪伴感；不评判、
    不给医疗/心理建议。
  - `_dispatch_one` 按 `topic == CRISIS_CARE_TOPIC` 分流危机关怀，并对其三处**豁免**：
    ① **跳过变现配额门控**（伦理优先于变现——危机期一句陪伴不该被计费掐断）；
    ② **跳过 no_context skip**（陪伴本身即目的，不因"没聊过具体事"而不发）；
    ③ **不调用 context_provider、不注入对话要点**（避免把对方低谷内容回放进话术）。
  - send `reason` 标 `care:crisis`、extra 带 `crisis_care=True`（运营看板可识别来源）。
  - 保留 identity-leak / dislike 黑名单防重 / quiet_hours 顺延等既有护栏。
- `main.py::_on_crisis_block`：`topic` 由字面量改用 `CRISIS_CARE_TOPIC` 常量。
- **测试**：危机专线 5 测（用克制模板且不含 topic 字样 / 无上下文也发 / 越过 paywall /
  不取对话上下文 / 普通关怀仍走通用模板不受影响）。全量 **5862 passed / 31 skipped / 0 fail（218s，+5 测）**。

**实施中的再优化**：
1. **危机关怀豁免 paywall + no_context（关键伦理判断）**：最初只想换 prompt，深入后意识到通用路径上的
   两道「跳过」会把危机陪伴也挡掉——免费超额用户/无近期话题的用户恰恰可能最需要被接住。安全/伦理
   场景下，计费与"非空话才发"的工程护栏都应让位。
2. **不注入对话上下文进危机话术**：通用关怀靠引用具体事显真诚，但危机场景反过来——回放对方刚说的
   痛苦内容是冒犯。危机模板刻意"少即是多"：不取 context、不提 topic、只给陪伴。
3. **共享常量而非魔法字符串**：`CRISIS_CARE_TOPIC` 放 care_schedule（两端都 import 的模块），排队侧与
   派发侧单点对齐，避免一端改字符串另一端静默失配（与 ## 57 的中英标签教训同源）。
4. **零 schema 变更**：用保留 topic 做来源标识，不为单一来源加列做 migration——`情绪关怀` 不在可抽取
   topic 词典内，碰撞风险近零，且在 care 看板里本就是有意义的展示名。

**安全闭环语义收口**：主动召回 → 情绪护栏 → 危机升级 → **危机陪伴语气（本期）**，"接住"时的话术也校准到位。
**遗留**：① care 来源标识若未来需更细分（如 elevated vs severe 不同语气）可升级为枚举列；② 付费
「解锁预告」式召回（接变现）；③ 危机关怀送达后的人工接管闭环（坐席工作台高亮 `care:crisis` 来源）。

## 59. 回竞品变现主线 · Stage 1 · 把真实付费权益接进对话剧情闸（让付费门「通电」）

**立项判断 + 关键发现**（用户问"下一阶段 vs 继续开发哪个价值高"，核实代码后回竞品变现主线）。
核实到一个**比新功能更根本的缺口**：剧情付费闸（`story_engine.scenario_locked_reason` 的
`require_unlock` → `entitlement_allows`）读 `user_context["entitlement"]`，但**全仓库无任何地方往
该键写值**——对话里 `entitlement` 恒 `None` → **付费场景对所有人锁死，连真付费用户也进不去**，
付费剧情是死内容。`MonetizationRuntime` / `EntitlementStore.get_entitlement` 都已就绪，**只差接线**。
故先补这块地基（不补则任何"解锁预告召回"都是把人导向打不开的门）。

**改动**（provider 范式，复用 N 线既有 `companion_context` 进程级 provider 架构）：
- `utils/companion_context.py`：`_REL_PROVIDERS` 加 `entitlement`；`set_relationship_providers`
  加 `entitlement_resolver` 形参；新增 `resolve_entitlement(contact_key) -> Optional[dict]`
  （未注册/空 key/异常/非 dict → None）。
- `main.py`：变现就绪时（`monetization.enabled`）注册 resolver = `lambda ck: store.get_entitlement(ck)`。
  未启用 → 不注册 → `resolve_entitlement` 恒 None → 付费场景仍锁（零回归）。
- `skills/skill_manager.py`：新增 `_ensure_entitlement(user_id, user_context)`——剧情/成长指令前
  懒解析真实权益进 `user_context["entitlement"]`；**仅 story 启用时查**（普通消息零开销）、
  **5 分钟 TTL 缓存**（权益变动罕见，避免每条消息查库）、resolver 缺失则不动 ctx（零回归）。
  在 `_handle_message_guarded` 调用一次（覆盖列表/开始/成长所有命令，零命令签名改动）。
- **约定**：权益 `contact_key` == `process_message` 的 `user_id`（端用户身份）；支付流水须按同一
  key 写权益，闸才查得到。config.example 的 `monetization` 段注明此接线约定。
- **测试**：resolver 5 测（未注册→None / 返回 dict 且以 ck 调用 / 空 key / 异常吞 / 非 dict→None）；
  skill_manager 4 测（注入并解锁付费场景 / 无 resolver 不动 ctx 仍锁 / story 关时不查 / TTL 缓存不重查）。
  全量 **5871 passed / 31 skipped / 0 fail（224s，+9 测）**。

**实施中的再优化**：
1. **发现"付费闸没通电"这一根因，而非直接做预告召回**（最关键）：用户问的是"价值高低"，核实代码
   发现表层需求（预告召回）建立在一个失效地基上——先通电再谈转化，否则把用户导向打不开的门。
   以代码实况为准的又一次直接兑现。
2. **懒解析 + TTL 缓存 + 仅 story 启用时查**：付费闸只在剧情命令时才需要权益，故不在每条消息查库；
   5 分钟缓存挡住高频命令重复查询。性能与正确性兼顾。
3. **provider 范式而非散落注入**：不在 telegram_client/protocol_autoreply/web/RPA 各调用方分别注入
   entitlement（会散落、易漂移），而是 skill_manager 单点懒解析——一处接线，所有平台对话路径生效。
4. **contact_key 单点对齐 + 显式文档约定**（## 57 中英标签教训的延续）：权益 key 锚定 `user_id`，
   并在 config 注明支付侧须同 key 写入，避免"装了但 key 对不上、付费用户照样进不去"的静默失配。

**变现主线 Stage 1 收口**：付费剧情闸端到端通电（付费用户进得去、免费看到锁、未启用零回归）。
**下一步**：Stage 2 主动「付费解锁预告」召回（选 `need_unlock`-only 场景沉默期发预告 + `pitch_hint`
引导，情绪护栏 soft/block 一律抑制）；Stage 3 转化漏斗可观测（预告→点击→解锁）。

## 60. 变现主线 · Stage 2 · 主动「付费解锁预告」召回（让"够格但没买"的用户被温柔勾起向往）

**立项**：Stage 1 给付费闸通了电后，自然的转化驱动——沉默期里若用户**关系/前置都满足、只差付费**
（`need_unlock`-only），主动发一句温暖预告勾起向往、引导去解锁。免费内容用尽时的"再钩一次"，把
re-engagement 闭环从「免费剧情邀约」延伸到「付费剧情预告」。

**改动**（纯函数 + 复用 ## 53/59 既有装配，零新 schema、零命令签名改动）：
- `skills/story_engine.py`：新增纯函数 `select_paid_teaser(scenarios, *, bond_level, completed,
  active_id, entitlement)`——**只选 `scenario_locked_reason` 恰为 `need_unlock:*` 的场景**（reason
  优先级 bond→prereq→unlock，故 `need_unlock` 意味关系/前置都已满足）。返回 `{scenario_id, title,
  feature}`。被关系/前置锁的不选（够不着的不推）；用真实 entitlement 判 → 已解锁者 reason 为空
  自然落选（不骚扰付费用户）；跳过已完成/进行中/无 beat。
- `skills/skill_manager.py`：新增 `_proactive_story_teaser(memory_key, intimacy, contact_key)`——
  需 story 启用 + `paid_teaser` 开 + 有 context + **解析到真实权益**（经 Stage 1 `resolve_entitlement`，
  以 `contact_key` 锚定端用户）。无 contact_key / 无权益源（变现未就绪）→ None（不对未知状态空推）。
  关系等级用 effective intimacy（base+封顶剧情加成）算，与对话面/健康卡同源。话术只勾期待与靠近感、
  **不报价格、不像广告**（真正付费交回消息时的店内 paywall → 先对话后商业，转化更顺、不惹反感）。
- `build_proactive_opener`：召回优先级 **免费邀约 → 付费预告 → 记忆话题**（新内容钩子最强、其次付费
  钩子、最后记忆兜底）。付费预告与免费邀约同处 `_gate != "soft"` 块内 → **情绪 soft/block 一律抑制**
  （绝不在用户低谷/危机期推销，付费预告比免费邀约更敏感）。
- `companion_proactive.py` / `main.py`：把 `contact_key`（= 会话 `conversation_id`，即 Stage 1 约定的
  权益 key）沿 `plan_proactive_sends → opener_fn → build_proactive_opener` 串下去（与 ## 57 `last_emotion`
  同范式）；`main.py::_opener` 透传。
- `companion_sample_store.py`：`_MODE_HINTS` 加 `story_teaser` 调优建议（删价格暗示/只对 need_unlock-only
  发/付费预告更敏感→上调 cooldown）。
- `config.example.yaml`：`companion.story.paid_teaser: false`（**默认关**——转化敏感、需运营确认 +
  需 `monetization.enabled` 接真实权益排除已解锁者）。
- **测试**：`select_paid_teaser` 9 测（选 paywall-only / 排除已解锁 / 跳过前置锁 / 前置满足后选续作 /
  跳过 bond 锁 / 跳过完成与进行中 / 无付费场景 / 空与脏入参）；teaser 接线 7 测（免费用尽→预告 /
  免费邀约优先于预告 / 已解锁→不推回落记忆 / flag 关→回落 / 无 contact_key→回落 / 无 resolver→回落 /
  soft 情绪→抑制）。全量 **5886 passed / 31 skipped / 0 fail（192s，+15 测）**。

**实施中的再优化**：
1. **软预告而非硬推销（关键 UX/转化判断）**：原计划接 `upsell_pitch_hint` 在预告里带价格/报价。深入后
   改为**只勾期待、不报价**——预告的职责是"创造向往 + 把人唤回对话"，真正的 paywall/报价交给用户回消息
   时的店内流程（先对话后商业）。既更不惹反感，转化链路也更顺（带价格的冷启动召回易被当广告忽略）。
   `pitch_hint` 接入降级为 Stage 2.1 可选优化。
2. **"只差付费"的精确判定（不推够不着的）**：复用 `scenario_locked_reason` 的 reason 优先级，只认
   `need_unlock:*` → 自动排除关系/前置没到位的场景（推那些是误导："解锁了你也玩不了"）。一个纯函数收口。
3. **无权益源则不预告（fail-safe）**：resolver 未注册（变现未就绪）→ `resolve_entitlement` 返回 None →
   直接不预告。避免在无法判断"是否已购"时对可能已付费用户误推，也避免空 key 乱推。
4. **召回优先级与情绪护栏的统一收口**：免费 > 付费 > 记忆三级回落 + 付费预告纳入 soft/block 抑制 →
   既保证"先给免费价值再谈付费"，又保证"低谷期绝不推销"，与 ## 57 的情绪护栏一脉相承。

**变现主线 Stage 2 收口**：免费用尽 → 付费预告（够格且未购才推、低谷抑制、软预告不报价）→ 记忆兜底。
**下一步**：① Stage 2.1 预告话术接 `pitch_hint`（可选，软引导一句"可解锁"）；② Stage 3 转化漏斗
可观测（预告发出→回流→解锁的归因埋点，看预告真实转化率）；③ 付费预告专属 cooldown（比免费邀约更长，
当前共用会话级 72h cooldown，已不算 spammy，但精细化可独立配）。

## 61. 变现主线 · Stage 3 · 付费预告「转化漏斗」可观测（把"凭感觉推销"变"看数据调参"）

**立项**：Stage 2 把付费预告发出去了，但**无归因**——不知道预告是否真的把人推向付费，`paid_teaser`
该不该常开、话术好不好全凭感觉。本期补埋点 + 归因，让预告产生可衡量的商业价值。

**关键设计判断**：变现库 `tx_ledger` 已逐笔时间戳记录所有已付事件（unlock/subscribe）——**它本身
就是转化真相源**，无需在解锁路径另插埋点（少改一处、零回归风险）。故只需记「预告发出」事件，查询期
与 `tx_ledger` 做时间窗归因即可。

**改动**（新增独立埋点库 + 复用既有派发/变现栈，零 schema 改既有表）：
- `utils/companion_funnel_store.py`（**新**）：`CompanionFunnelStore`（SQLite，镜像 entitlement_store
  约定：单连接 + Lock + 绝不抛 + :memory:/文件双模）。表 `teaser_events`(contact_key, scenario_id,
  feature, ts)。`funnel_stats(*, paid_lookup, window_days, attribution_days)` 在查询期归因：端用户
  **最早预告后** attribution 窗内有已付事件 → 记转化；已付 item_id 命中预告 feature → 记**精确转化**
  （更强信号）。`paid_lookup` 注入式（不耦合变现内部 → 可纯单测）；缺省→转化恒 0 仅看触达。
- `utils/entitlement_store.py`：加 `paid_events_for(contact_keys, *, since)`——批量取多人已付事件流
  （`{ck: [{item_id, kind, ts}]}`，仅 status='paid'，IN 分块），作漏斗真实 `paid_lookup` 底座。
- `companion_proactive.py`：`plan_proactive_sends` 计划携带 `scenario_id/feature`（归因元数据，普通
  opener 为空串）；`CompanionProactiveLoop` 加 `on_sent(plan)` 钩子——发送**成功后**触发（best-effort，
  抛错不影响派发计数）。
- `main.py`：变现就绪时建 `companion_funnel.db` 挂 app.state；proactive loop 注入 `_on_teaser_sent`
  回调——仅 `mode == story_teaser` 时记一条 teaser 事件（contact_key=conversation_id，与 Stage 1/2 同 key）。
- `web/routes/monetization_routes.py`：`GET /api/monetize/teaser-funnel?window_days=&attribution_days=`
  → 发出数/触达人数/转化数/精确转化数/转化率/按场景分布 + 最近 20 条；漏斗库未挂（变现/预告未开）→
  `enabled:false` 空漏斗（不报错）。
- config.example：`monetization` 段注明 Stage 3 漏斗库与查询接口（无配置项，纯埋点 + 查询期归因）。
- **测试**：funnel store 14 测（记录/空 key 跳过/recent 序/无 lookup 仅触达/窗内归因+精确命中/窗外不计/
  预告前付费不计/订阅算转化但非精确/window 排除旧预告/lookup 异常吞/空库/单例复用重置/paid_events_for
  批量+仅 paid+since 过滤+空 key）；proactive 5 测（计划携带 scenario/feature/普通为空、on_sent 成功触发/
  失败不触发/抛错不破派发）；route 2 测（无库 enabled:false / 端到端归因转化率）；路由清单 +1。
  全量 **5907 passed / 31 skipped / 0 fail（212s，+21 测）**。

**实施中的再优化**：
1. **复用 tx_ledger 作转化真相源，不在解锁路径插埋点**（最关键）：解锁/订阅已逐笔带 ts，查询期时间窗
   归因即可——避免在 record_unlock/grant_subscription/webhook 多处插钩子（散落、易漏、有回归风险）。
   一处只读 `paid_events_for` + 注入式 `paid_lookup` 收口。
2. **注入式 paid_lookup 让漏斗库与变现解耦**：store 不 import entitlement_store，转化逻辑可用假 lookup
   纯单测；main/route 才注入真实底座。同 N 线 provider 范式（改一处不牵连）。
3. **精确转化 vs 泛转化双指标**：item_id 命中预告 feature=精确转化（强归因）；预告后任意付费=泛转化
   （含订阅顺带解锁、被预告勾起后买了别的）。两个率都给运营，避免单一口径误判。
4. **on_sent 通用钩子而非 teaser 专用**：循环只暴露"发送成功"通用事件，main 侧按 mode 过滤记 teaser——
   循环不耦合变现概念，未来别的埋点（邀约点击率等）可复用同一钩子。
5. **归因窗口/统计窗口都做查询参数而非配置**：运营可在看板即时切 7/14/30 天看不同口径，无需改配置重启。

**变现主线 Stage 3 收口**：预告发出即埋点 → tx_ledger 时间窗归因 → 转化率/精确转化率/按场景分布上 API。
"凭感觉推销"变"看数据调参"的闭环建立（未启用变现/预告→空漏斗零回归）。
**下一步**：① **回流(reply)中间指标**——本期做了"发出→解锁"两端，中间的"用户被预告后是否回了消息"需在
入站消息路径插一个轻量归因钩子（预告后该会话首条 inbound → 记 return 事件），补全三段漏斗、定位是
"没人理"还是"理了不买"；② 漏斗卡片接进 workspace_usage 模板可视化（当前是 JSON API）；③ 转化数据回哺
Stage 2 选择：低转化场景自动降权/停推（用 funnel_stats 反向调 `select_paid_teaser` 候选）。

## 62. 竞品对标 · Stage A · 陪伴「形象照/自拍」生成（把 exclusive_album 真正通电）

**立项判断 + 关键发现**（用户问"竞品对标还有哪些要做"，逐竞品盘点代码实况后定）。对标星野/Talkie/
Replika 的招牌情感钩子=「她发来一张照片」，本仓**完全空白**。更关键：变现目录 `monetization.items`
里早已定义付费项 `exclusive_album`（专属相册 ¥4.99），但**全仓库零交付代码**——又一处"装了付费项
却没通电"（同 Stage 1 entitlement 病灶）。故本期把自拍能力补上并直接咬合 exclusive_album 付费闸。

**改动**（新增纯逻辑引擎 + 软失败 provider 骨架，复用 K2 变现 gate / 编排器媒体通道，默认全关）：
- `ai/companion_selfie.py`（**新**，镜像 `tts_pipeline` 范式）：
  - `detect_selfie_request(text)`：多语保守意图识别（自拍/你长什么样/what do you look like/send a pic…），
    刻意避开"用户自述照片"误命中、超长叙述不命中。
  - `build_selfie_prompt(persona, *, scene_hint, style, default_appearance, sfw)`：出图提示词纯函数，
    外貌优先级 persona 真实外貌→config default_appearance→按 name 通用→中性兜底；**强制 SFW 安全约束**。
  - `decide_selfie(*, entitlement, gate_enabled, free_used, free_daily, bond_level, min_bond_level)`：准入纯
    函数——关系浅→`too_soon`；拥有相册/gate 关→`allow` 不限；免费额度内→`allow(used_free)`；用尽→`locked`。
  - `SelfieProvider`（enabled/backend=disabled|openai|command，软失败、绝不抛）+ `SelfieResult` + 单例。
- `skills/skill_manager.py`：`_handle_selfie_request`（async）接入 `_handle_message_guarded`（growth 指令后、
  冷却前）。`too_soon`→温柔搪塞；`locked`→复用 `monetization.upsell_*` 出 exclusive_album 软付费引导
  （把死目录项变活 paywall 触点）；`allow`→`SelfieProvider` 出图，有受管媒体 worker 则经
  `account_orchestrator.send_media` 发出（返回 ""=已发不再补文字），否则优雅退回文字陪伴。免费额度按天
  在 user_context 计数（仅 gate 开+未拥有时消耗）。`_monetization_gate_enabled` 复用变现总闸判定。
- `config.example.yaml`：`companion.selfie` 段（enabled/free_daily/min_bond_level/appearance/style/scene_hint/
  caption/provider）默认全关 + provider.backend=disabled（不接真模型零行为）。
- **测试**：引擎 16 测（意图正/负样本、提示词外貌优先级+SFW、决策 too_soon/gate 关不限/拥有相册不限/
  免费额度→locked、provider disabled/未知 backend/空 prompt 软失败/command 后端真出图/单例）；接线 7 测
  （未开→None/非请求→None/关系浅搪塞/未解锁付费引导/免费额度兜底并计数/拥有相册不计数/gate 关不引导）。
  全量 **5938 passed / 31 skipped / 0 fail（180s，+23 测）**。

**实施中的再优化**：
1. **发现 exclusive_album"付费项没通电"为切入点**（最关键）：不是凭空加自拍，而是补上一个已售卖却无
   交付的付费项——和 Stage 1 同源的"以代码实况为准"判断，先让已有商品可交付再谈扩展。
2. **软付费引导（locked 路径）零依赖即生效**：locked 分支是纯文字 upsell，不依赖任何图像 provider——
   即使不接真模型，也立刻把 exclusive_album 从死目录变成对话内活 paywall 触点（直接续 Stage 1-3 变现链）。
3. **provider 默认 disabled 仍优雅**：未接模型时 `allow` 路径退回文字陪伴而非报错/沉默，付费用户体验不破。
4. **外貌描述四级回落 + SFW 硬约束在 prompt 层**：persona 无 appearance 字段也能出图（config 兜底），
   安全约束不依赖模型自觉。
5. **媒体发送走既有编排器通道、best-effort**：不新造每平台发图栈——有受管媒体 worker 就发、没有就退文字，
   `owns_media` 守卫 + try/except，零破坏既有发送链。

**Stage A 收口**：自拍意图 → 关系/付费双 gate → 出图(默认文字兜底) → exclusive_album 付费闸通电。
**下一步**：① **真接图像 provider**（openai images 已写、本地 ComfyUI/SD command 模板已留口，接真模型即出图）；
② **每平台发图栈补全**（当前依赖编排器受管 media worker；A 线主客户端 send_photo 直发可补，扩大覆盖）；
③ **自拍转化接 Stage 3 漏斗**（locked→付费引导也记 funnel 事件，scenario_id="selfie"/feature="exclusive_album"，
与剧情预告同口径看相册转化率）；④ **持久化免费额度**（当前在 user_context，可落库防重启绕过）。

---

## 63. Stage B：自拍「转化可观测」——exclusive_album 付费墙是否真把人推向付费（续 Stage A③）

**背景/动机**：Stage A 让 `exclusive_album` 通了电（locked 软付费引导 + 准入出图），但**有没有真转化无数据**。
本期补上观测闭环——精确回答运营最关心的：**自拍付费墙(locked)真的把人推向买 exclusive_album 了吗？**

**实施前的代码实况核对**（遵循"以代码为准"）：`rg selfie` 确认 `_handle_selfie_request` 仅在 user_context
内存里 `_selfie_used` 计数、**零持久化埋点**；Stage 3 的 `CompanionFunnelStore` 只记剧情预告 `teaser_events`，
不含自拍。结论：本功能此前未开发，且 Stage 3 漏斗库的归因基建（单连接+锁+`:memory:`/文件+软失败+
`paid_events_for` 注入式归因）正可**复用扩展**而非另起炉灶。

**改动**（**扩展** `CompanionFunnelStore`，剧情 teaser 路径零改动 → 对刚提交的 Stage 3 零回归风险）：
- `utils/companion_funnel_store.py`：新增 `selfie_events` 表 + `record_selfie(ck, kind)`（kind∈too_soon/locked/
  delivered，与 `decide_selfie` 三态一一镜像）+ `selfie_recent/selfie_count` + `selfie_funnel_stats(...)`。
  归因核心指标 `conversion_rate`=**付费墙转化率**（分母=触墙端用户数）：触墙(locked)群体在 attribution 窗内买了
  `exclusive_album`(item_id 命中)即记一次转化。新增 `peek_companion_funnel_store()`（**只取已存在单例、绝不创建**）。
- `skills/skill_manager.py`：`_record_selfie_event(ck, kind)` best-effort 埋点——只 `peek` 已就绪单例
  （monetization 接了才有）、未初始化静默 no-op、绝不抛；`contact_key` 取 `user_id`（与 entitlement/tx_ledger
  同一身份键，保证 exclusive_album 付费可归因）。在 `_handle_selfie_request` 三个准入分支各记一条。
- `web/routes/monetization_routes.py`：`GET /api/monetize/selfie-funnel?window_days=30&attribution_days=14`
  → 需求(requests)/触墙(locked)/送达(delivered)数 + 付费墙转化率（镜像 teaser-funnel，复用 `paid_events_for`）。
- `config.example.yaml`：变现段补 Stage B 漏斗说明（无新配置项，纯埋点+查询期归因，与 Stage 3 同库）。
- **测试**：store 13 测（kind 计数/非法 kind+空 key 拒写/recent 倒序/无 paid_lookup 仅分桶/locked→album 归因/
  只 album item 算转化/只 locked 群体能转化/窗外不算/付费早于 locked 不算/旧事件出窗/paid_lookup 异常吞掉/空库）；
  接线 3 测（locked/delivered/too_soon 各自落正确事件 + 单例未就绪时 no-op 不破坏主流程）；路由 2 测（无库空漏斗/
  端到端 album 归因）。**全量 5955 passed / 31 skipped / 0 fail（单进程 703s，+~18 测）**。

**实施中的再优化**：
1. **复用扩展而非另起新库**（最关键）：本可新造 `SelfieFunnelStore`，但 Stage 3 已有同一套归因基建——
   扩 `selfie_events` 表 + 专用方法，**teaser 路径一行未碰**，既零回归又不重复造轮子（同一 companion_funnel.db）。
2. **新增 `peek`（只取不建）解决埋点污染**：`get_companion_funnel_store(None)` 会误建 `:memory:` 抛弃式 store；
   skill_manager 改用 `peek` → 仅 monetization 用真 db_path 初始化后才记录，避免对话路径里悄悄建脏库。
3. **kind 三态 == decide_selfie 三 action**：埋点语义与准入决策 1:1 对齐（too_soon/locked/delivered），
   可测、无映射歧义；转化只认 **locked 群体**（真触墙才是付费机会，免费送达/关系浅不混入分母）。
4. **store 零耦合 ai 层**：归因目标项 `exclusive_album` 在 store 内硬编码常量（不 import companion_selfie），
   保持纯单测、与变现内部解耦（同 Stage 3 设计哲学）。

**能否再优化**：可在 `selfie_events` 加 `used_free` 维度细分免费/owned 送达，或把 requests 也按 persona/account
分桶看哪个人设最招自拍需求——属下一阶段看板细化，不阻塞本期闭环。

**踩坑/环境**：全量 `-n auto` 在本机反复"卡死"（数十分钟不结束），经诊断**非本期代码、非测试失败**：
collection 7s 正常、单进程 `-p no:xdist` 11m43s 全绿（5955 passed/0 fail）→ 确认是 **pytest-xdist worker
关停期**在本 Windows 环境的挂起（与外部常驻进程无关，killed 后仍复现）。**本机回归改用单进程兜底**；CI（Linux）
不受影响仍走 `-n auto`。

**Stage B 收口**：自拍准入三态全埋点 → 触墙群体 exclusive_album 付费归因 → `/api/monetize/selfie-funnel` 看板。
**下一步**：① **真接图像 provider**（openai images/本地 SD command 已留口，接真模型即出图，让 delivered 真出片）；
② **A 线主客户端 send_photo 直发**（当前依赖编排器受管 media worker，补主客户端直发扩大覆盖）；
③ **看板 UI**（teaser-funnel + selfie-funnel 合并进变现总览页，运营一眼看两条转化链）；
④ **持久化免费额度**（自拍免费额度当前在 user_context，落库防重启绕过）。

---

## 64. Stage C：真接 openai images 出图 provider——让 delivered 从"文字兜底"变"真出片"（续 Stage B①）

**背景/动机**：Stage A 写了 `SelfieProvider` 骨架，但 `_generate_openai` 只是"看起来能跑"的占位——实测有真实缺陷，
接真模型必翻车。本期把它**做成生产可用 + 可单测（无网络）**，让整条 exclusive_album 变现链真正交付图片。

**实施前的代码实况核对**：复核 `_generate_openai` 发现三处真 bug/隐患：
1. **只取 `resp.data[0].b64_json`**——`dall-e-2/3` 默认返回 **url**（不带 `response_format=b64_json` 时 b64 为空）→
   接 dall-e 直接 `empty b64` 报错；
2. **不传 client timeout**——HTTP 请求不会自中断，全靠外层 `wait_for`，但外层默认 60s == 无独立请求超时；
3. **外层 `wait_for(timeout_sec=60)` 与请求超时同值**——会在合法出图（gpt-image-1 high 常 30~90s）中途误砍，
   掩盖底层精确错误。另：`gpt-image-1` **不接受** `response_format` 参数（传了真实 API 报错），不能一刀切传。

**改动**（`ai/companion_selfie.py`，沿用 `tts_pipeline` 的"provider 自带 key/base_url"约定——全局 `ai` 是
DeepSeek/openai_compatible 不支持图像，**绝不可共用那把 key**）：
- `_generate_openai` 拆为可测三段：`_make_openai_client()`（测试缝，可 monkeypatch 注入假 client）+
  `_openai_generate_bytes(client, prompt)`（model 感知请求 + b64/url 双回退）+ `_download_image(url)`（stdlib urllib，
  不引依赖）。**model 感知**：`dall-e*` 才加 `response_format=b64_json`；`gpt-image-1` 不传（避免报错）。
- 新增 config：`quality`（gpt-image-1 low/medium/high/auto 透传）、`request_timeout_sec`（client 单请求超时，默认 60，
  真正能自中断挂起请求）。client 构造带 `timeout`。
- **外层 wait_for 改兜底语义**：`generate(timeout_sec=None)` 时 `eff = inner(请求/命令超时) + 15s 余量`——严格大于
  底层超时，只在底层完全失控时才兜底触发，平时让底层吐精确错误（而非笼统 outer timeout）。
- **测试 +10**：openai 路径全用**注入假 client**（无网络）——b64 解码/dall-e 自动加 response_format/quality 透传/
  url 回退/无 b64+无 url 报错/空 data 报错/缺 key 报错/端到端写文件/client 异常软失败不抛/显式超时兜底。
  **全量 5965 passed / 31 skipped / 0 fail（单进程 711s）**。

**实施中的再优化**：
1. **拆出 `_make_openai_client` 测试缝**（最关键）：原 `_generate_openai` 把"建 client + 发请求 + 写文件"焊死，
   无法不联网测。拆出注入点后，openai 出图逻辑首次拥有**确定性单测**（覆盖 b64/url/各错误分支），不再是黑盒占位。
2. **model 感知请求**：发现 gpt-image-1 与 dall-e 的 response_format 行为相反——一刀切必有一方报错；按 model 前缀分流。
3. **b64/url 双回退**：兼容官方 + 自建/代理 images 网关（base_url 场景）的返回差异，提升真实环境鲁棒性。
4. **两层超时分工明确**：client timeout 管请求自中断、外层 wait_for 管线程失控兜底（+15s 余量），消除互相误砍。
5. **坚持 provider 独立 key**：核对后确认不能复用全局 ai（DeepSeek）key——技术上不支持图像，强行复用会误导运营。

**能否再优化**：可加 `n>1` 多图候选 + 选优、或本地缓存"同 persona 提示词→图"避免重复计费——属成本/质量调优，
非交付闭环必需。出图后**发送通道**仍依赖编排器受管 media worker（A 线主客户端 send_photo 直发是下一步②）。

**Stage C 收口**：openai images（gpt-image-1/dall-e）生产可用、可单测、双回退、双超时分工；接真 key 即出真图。
**下一步**：① **A 线主客户端 `send_photo` 直发**（脱离对编排器 media worker 的依赖，让主平台 Telegram 直接发图，
扩大 delivered 实际触达面）；② **变现看板 UI 合并**（teaser-funnel + selfie-funnel 进总览页）；
③ **持久化自拍免费额度**（落库防重启绕过）；④ **出图缓存/多候选**（控成本、提质量）。

---

## 65. Stage D：A 线主客户端 send_photo 直发——出图能力的"最后一公里"（续 Stage C①）

**背景/动机**：Stage A-C 让自拍能"判准入→出真图"，但 `_try_send_selfie_media` **只走编排器受管媒体 worker**
（B 线/受管账号）。主平台 Telegram 的 A 线主客户端（`TelegramClient`）**不注册为编排器 worker**，故
`owns_media` 恒 False → 出了图也发不出、永远退回文字。本期补"直发"兜底：A 线直接 `send_photo` 把图送达。

**实施前的代码实况核对**：`rg send_photo/send_media/owns_media` 确认——`_try_send_selfie_media` 仅编排器单路；
A 线是 **Pyrogram**（非 Telethon），`send_message` 在 `TelegramSenderMixin`（`client/sender.py`），且 A 线已把
`'_send_to_chat': self.send_message` 注入 user_context（既有"回调注入"范式）。结论：直发能力此前未开发，
且可**复用同一注入范式**加一个 `_send_photo_to_chat`，不必让 skill_manager 持有客户端引用（解耦）。

**改动**（沿用既有"回调注入"缝，零侵入 skill_manager 与客户端耦合）：
- `client/sender.py`：`TelegramSenderMixin` 新增 `async send_photo(chat_id, photo_path, caption)`——镜像
  `send_message`：无 client/空路径→False，命中风控走 G2 封号信号分级处置，**绝不抛、失败返 False**。
- `client/telegram_client.py`：A 线 `_sm_context` 注入 `'_send_photo_to_chat': self.send_photo`（与 `_send_to_chat`
  并列），让 skill_manager 在不持有客户端引用的前提下能回调直发。
- `skills/skill_manager.py`：`_try_send_selfie_media` 改**双路兜底**——①编排器受管 worker（owns_media 时）；
  ②A 线 `_send_photo_to_chat` 回调直发。任一成功即 True，两路皆不可用才退文字。
- **测试 +8**：`_try_send_selfie_media` 直发回调成功/无通道 False/空图 False/回调异常软兜底；`send_photo`
  成功/RPC 失败/无 client/空路径；`_handle_selfie_request` 准入出图→直发→返回 ""（媒体已发不补文字）端到端。
  **全量 5971 passed / 31 skipped / 0 fail（单进程 754s）**。

**实施中的再优化**：
1. **复用"回调注入"范式而非给 skill_manager 塞客户端引用**（最关键）：A 线早有 `_send_to_chat` 注入先例，
   加 `_send_photo_to_chat` 与之对齐——skill_manager 不耦合具体客户端、可纯单测（stub 回调即可），架构一致。
2. **双路兜底顺序：编排器优先、直发兜底**：受管账号（B 线/统一收件箱）走编排器保持原行为不变；只有主平台
   A 线（无受管 worker）才落直发——既不改既有受管路径（零回归），又补齐主平台触达。
3. **send_photo 与 send_message 同构 + G2 封号信号**：直发照片同样吃风控分级急停，不让发图绕过安全护栏。
4. **空路径/无 client 早退**：直发回调对脏输入鲁棒，绝不抛进对话主链路。

**能否再优化**：可把 `send_photo` 也纳入 A 线 `min_interval` 全局发送节流（当前仅 send_message 计时）避免图文
混发触发风控——属节流策略细化，可下一阶段统一；当前直发已走 G2 异常处置兜底。

**Stage D 收口**：A 线主客户端 `send_photo` 直发兜底接通——自拍出图能力"最后一公里"打通，主平台 Telegram
准入用户真能收到形象照（编排器受管账号仍走原 worker，零回归）。
**下一步**：① **变现看板 UI 合并**（teaser-funnel + selfie-funnel 进变现总览页，运营一眼看两条转化链，纯前端零风险）；
② **持久化自拍免费额度**（当前在 user_context，落库防重启绕过）；③ **发送节流统一**（send_photo 纳入 min_interval）；
④ **出图缓存/多候选**（控成本、提质量）。

---

## 66. Stage E：变现看板 UI 合并——两条转化漏斗进总览页（续 Stage D①，让 Stage B/C/D 可观测真正被用起来）

**背景/动机**：Stage B/3 已建好 `teaser-funnel` / `selfie-funnel` 两个 API，但运营**没有入口看**——数据躺在
接口里没人用。本期把两条转化链渲染进既有变现看板 `/monetization`，让"预告→剧情付费""自拍触墙→相册付费"
一眼可见。纯前端（模板 + 原生 JS），零后端改动、零风险。

**实施前的代码实况核对**：`rg monetization.html` 确认看板是 `templates/monetization.html`（Jinja 继承 base.html +
原生 JS fetch `/api/monetize/*` 渲染），已有 `mzLoad/mzRetention` 范式 + `.mz-card` 卡片样式可复用；
两漏斗 API 已存在且已测。结论：UI 此前未做，且可**纯复用既有渲染范式**，不引任何新依赖/组件。

**改动**（`web/templates/monetization.html`，纯前端）：
- 新增「转化漏斗」card：①付费预告漏斗（发出预告/触达人数/转化/转化率 4 卡 + 按场景分布表，精确转化=已付项
  命中预告 feature）；②自拍/形象照漏斗（自拍请求/触墙/送达/相册转化/付费墙转化率 5 卡）。
- JS `mzFunnels()` → `mzTeaserFunnel()` + `mzSelfieFunnel()`：复用上方统计窗口（window_days）+ 固定归因窗 14 天，
  fetch 两端点渲染；`enabled=false`（功能未开）优雅显示"未启用"而非报错；请求失败显示降级提示。
- 接入初始加载（DOMContentLoaded）与「刷新」按钮 → 与营收/挽回榜同步刷新。
- **测试 +1**：模板接线 smoke（断言引用两端点 + 渲染容器 id + mzFunnels 触发，防接线被误删）。
  **全量 5972 passed / 31 skipped / 0 fail（单进程）**；另跑 jinja parse 校验模板语法无误。

**实施中的再优化**：
1. **纯复用既有渲染范式 + 卡片样式**（最关键）：不引图表库/前端框架——`.mz-card`/`.mz-table` 既有样式 +
   `fCard()` 小工厂函数即可，保持看板视觉一致、零新依赖、加载零额外体积。
2. **`enabled=false` 优雅降级**：功能未开时显示"未启用 + 需开哪个 flag"提示而非空白/报错，运营自助可诊断。
3. **复用统一统计窗口**：漏斗 window_days 跟随页面顶部「统计窗口」输入，刷新按钮一键同步营收+漏斗，交互一致。
4. **smoke 测守接线而非脆断言渲染**：只断言端点/容器/触发函数存在（稳定），不耦合具体 DOM 文案（易变）。

**能否再优化**：可加归因窗（attribution_days）独立输入、或把 by_scenario 做成可点击下钻到具体端用户列表——
属看板交互深化；当前已满足"一眼看两条转化链"的核心诉求。

**Stage E 收口**：`/monetization` 看板新增两条转化漏斗可视化——Stage B/C/D 的数据/能力首次有统一运营入口，
变现闭环（意图→双闸→出图→送达→**可观测**）在 UI 层收口。
**下一步**：① **持久化自拍免费额度**（当前在 user_context，重启清零可绕过，落库防滥用 + 跨进程一致）；
② **发送节流统一**（send_photo 纳入 A 线 min_interval，图文混发不触风控）；③ **漏斗下钻**（by_scenario→端用户列表）；
④ **出图缓存/多候选**（控成本、提质量）。

---

## 67. Stage F：全局每日出图预算 cap——护住出图 API 账单（修正 Stage E① 的过期假设 + 补真缺口）

**实施前的代码实况核对（重要修正，遵循"以代码实况为准"）**：原计划"持久化自拍免费额度"基于一个**错误假设**
——以为 `_selfie_used`/`_selfie_date` 在 user_context 是内存态、重启清零。核对 `_get_user_context` 实况：
它返回 `self._context_store.get(user_id)`（**SQLite 持久化** bot.db），且 `_handle_selfie_request` 调用方在返回前
`mark_dirty(user_id)+flush(user_id)`——**按端用户的免费额度本就已持久化、重启不清零**。原计划是伪需求。

**真正的缺口**（核对后定位）：per-user `free_daily` 只限**单个**端用户；但 **N 个不同新用户各刷免费图**会让
出图 API（OpenAI images 等，按张计费）总开销**无上限**——这才是上线前真金白银的风险。无任何全局/账号级
当日出图总量护栏（`rg daily_cap/global_cap/budget` 确认：RPA 有发送 cap，出图无）。故 Stage F 转做这个真护栏。

**改动**（复用既有 `DailyCapTracker` 范式，不造新轮子；默认 0=不限 → 行为零变化）：
- `config.companion.selfie.daily_global_cap`（int，0=不限）：跨所有端用户的**当日出图总次数**硬上限。
- `skills/skill_manager.py`：`_get_selfie_cap(cap)` 惰性持有进程级 `DailyCapTracker`（tz 0 点自动归零、线程安全、
  运行时 `set_cap` 跟随 config）。`_handle_selfie_request` 的 allow 路径：仅当 provider **真出图**
  （enabled 且 backend≠disabled，即真有 API 成本）时才计数/拦截——`would_exceed`→优雅兜底文案、
  **不消耗用户免费额度、不记 delivered、记 capped 事件**；放行则 `record_sent(1)` 后再 generate。
  provider disabled（无成本）时 cap 完全不介入（行为不变）。
- `utils/companion_funnel_store.py`：`SELFIE_KINDS` 增 `capped`；`selfie_funnel_stats` 输出 `capped` 计数
  （运营可在自拍漏斗看「多少需求因预算被拦」——预算调参的依据）。
- **测试 +5**：全局 cap 拦第二次且不消耗用户免费额度+记 capped、cap=0 不限、provider disabled 时 cap 不介入、
  store capped kind 计数。**全量 5976 passed / 31 skipped / 0 fail（单进程，~11min）**。

**实施中的再优化**：
1. **核对推翻伪需求、改做真缺口**（最关键）：没有盲目"持久化"一个本已持久化的东西，而是按代码实况重定位到
   真正的烧钱风险（全局出图无上限），把工时投在有效护栏上——正是"以代码实况为准"避免 tasklist drift。
2. **只对真出图计数**：cap 仅在 provider 真会产生 API 成本时介入；文字兜底（provider disabled）零拦截，
   不误伤"功能开但模型没接"的常态体验。
3. **per-user 持久额度 × 全局当日 cap 双层防滥用**：前者限单人（已持久化）、后者限全局爆发面（本期补），互补。
4. **capped 入漏斗可观测**：预算拦截不是黑箱——运营能看到被拦量，据此调 cap/加预算（数据驱动）。
5. **复用 DailyCapTracker**：tz 归零/线程安全/set_cap 热调全有，零新代码风险。

**能否再优化**：全局 cap 为进程内存态——多进程部署需共享计数（可后续落 Redis/DB）；当前单进程足够，
且 per-user 额度已持久（重启只重置全局上限，攻击者无法重启我方服务，风险可接受）。可加 `/api/monetize`
暴露 cap 快照（已用/剩余/归零时刻）供看板显示——属运维可视化增强。

**Stage F 收口**：全局每日出图预算护栏接通——出图 API 账单有硬上限（默认关、开即生效），叠加已持久化的
per-user 免费额度，自拍变现链上线前的烧钱风险收敛。同时**修正了一个过期任务假设**（per-user 额度本已持久化）。
**下一步**：① **发送节流统一**（send_photo 纳入 A 线 min_interval，图文混发不触 Telegram 风控）；
② **cap 快照上看板**（已用/剩余/归零时刻，运营可视）；③ **漏斗下钻**（by_scenario/capped→端用户列表）；
④ **出图缓存/多候选**（控成本、提质量）。

---

## 68. Stage G：send_photo 纳入统一发送护栏/节流/记账——图不再绕过风控

**实施前的代码实况核对**：A 线文本回复 `_send_reply` 有完整发送安全栈：**G1 Kill-Switch → N 线反封号闸门
→ min_interval 节流 → 发后刷墙钟 + 记入共用发送计数器**。但 Stage D 加的 `send_photo`（形象照「直发」缝，
已接进 skill_manager 的 `_send_photo_to_chat`）是**裸 Pyrogram 调用**——以上四项**一个都没有**：
- 账号被 Kill-Switch 冻结 / 被反封号闸门拦时，**文字不发但照片照发**（风控绕过，真安全洞）；
- 不读 `min_interval`：自拍「配文 + 照片」可瞬时双发 → 触 Telegram 反垃圾；
- 不刷 `_last_send_wallclock`：照片后紧跟的文字也不按间隔排队；
- 不记共用计数器：照片不计入今日外发量 → 反封号/健康灯**漏算**。

故 Stage G 范围比原计划「只补 min_interval」更宽更有价值：**把 send_photo 接入与文本回复同一套发送安全栈**。

**改动**（抽公共方法，文本与照片**单一实现来源**，真·统一而非各写一份）：
- `client/sender.py` 抽三个共用方法：`_presend_blocked()`（Kill-Switch+反封号闸门，异常静默放行）、
  `_presend_pace()`（按 `reply.split_send.min_interval_seconds` 相对**共用**墙钟补足间隔）、
  `_postsend_record_count()`（刷墙钟 + 记入共用发送计数器）。
- `_send_reply` 重构为调用这三个方法（逐字抽取、行为不变、日志措辞统一为「跳过 A 线外发」）。
- `send_photo` 接入同三方法：发前过护栏（拦则不发、返 False 让调用方退文字）、节流共用墙钟、发后记账
  （照片**计入**今日外发量，反封号不漏算）。
- **测试 +3**：护栏拦截则照片不真发、距上次<interval 则补足节流、interval=0 不节流（行为不变）。
  **全量 5979 passed / 31 skipped / 0 fail（单进程，~11min）**。

**实施中的再优化**：
1. **范围升级（核对驱动）**：原计划只补 min_interval，核对发现真缺口是**整条安全栈**缺失（含 Kill-Switch
   绕过这个安全洞）——按代码实况把范围扩到「统一全栈」，堵住「冻结期照片照发」的真风险。
2. **单一实现来源**：把文本回复里的安全栈逐字抽成共用方法，文本/照片走同一份——杜绝两线各写一份后**漂移**
   （日后改节流/闸门只改一处）。这也是「统一」一词的真正含义。
3. **共用墙钟**：照片与文字共用 `_last_send_wallclock`——图文混发整体排队，而非各算各的（真防瞬时双发）。
4. **零行为变更默认**：min_interval=0 / 闸门关 / 无 Kill-Switch 时行为与改前完全一致（全量回归佐证）。
5. **护栏自身异常一律放行**：护栏/节流出错绝不阻断发送（沿用既有 best-effort 原则）。

**能否再优化**：照片发送目前**未**做出站镜像（`_emit_inbox`）/contacts 记账（`record_relationship_message`）——
文本回复有；后续可让坐席台/亲密度引擎也看到「AI 发了张照片」（属可观测增强，非安全项）。另外 voice note
（`_maybe_send_voice_reply` 内 `send_telegram_voice`）同样走裸路径，下一步可一并纳入本统一栈。

**Stage G 收口**：A 线**所有富媒体外发**（文本已有、照片本期接入）共用同一套 Kill-Switch+反封号闸门+节流+记账——
形象照不再是风控盲区，图文混发也排队。变现自拍链（出图→直发）上线前的**账号安全**缺口收敛。
**下一步**：① **voice/照片纳入出站镜像 + contacts 记账**（坐席台/亲密度看到富媒体外发）；
② **voice note 纳入统一发送栈**（与照片同构）；③ **cap 快照上看板**；④ **漏斗下钻**（capped→端用户）。

---

## 69. Stage H：富媒体外发的出站镜像 + contacts 记账——坐席台/亲密度「看见」图与语音

**实施前的代码实况核对**：文本回复 `_send_reply` 发后做两件可观测/记账：**N4b 出站镜像**（`_emit_inbox`
→ 坐席台看到 AI 自动回复内容）+ **Q3 contacts 记账**（`record_relationship_message(..., "out", ...)`
→ IntimacyEngine 计入一次外发互动，mutuality 不偏低）。但 Stage D/G 的 `send_photo` 与 voice
（`_maybe_send_voice_reply` 在 `send_text_summary=false` 时）**两步都没做**——形成数据空洞：
- 坐席台**看不到** AI 给端用户发过照片/语音（人工接管时信息缺失）；
- IntimacyEngine **只见入站、不见这些富媒体外发** → 收发失衡、亲密度分数偏低（误判关系冷淡）。

**改动**（抽公共方法，文本/照片/语音**单一来源**）：
- `client/sender.py` 抽 `_postsend_mirror_and_record(chat_id, preview)`：出站镜像 + contacts 记账两步
  （各自 best-effort）。富媒体传带标记 preview：照片「`[图片] {配文}`」（无配文则「`[图片]`」）、语音「`[语音]`」。
- `_send_reply` 重构为调用该方法（逐字抽取、行为不变）。
- `send_photo` 发后接入（先 `_postsend_record_count` 计数，再镜像/记账）。
- `_maybe_send_voice_reply`：仅发语音（无文本摘要）时也镜像/记账；`send_text_summary=true` 时走 `_send_reply`
  已自带、不重复记。
- **测试 +3**：照片镜像带 `[图片] 配文` 前缀 + contacts 记 out、空配文 → `[图片]`、无 `_emit_inbox` 属性时
  镜像优雅跳过但 contacts 照记。**全量 5982 passed / 31 skipped / 0 fail（单进程，~10min）**。

**实施中的再优化**：
1. **范围含 voice**：不止照片——voice note 同样有此空洞，一并纳入（与用户「voice/照片」建议一致）。
2. **单一实现来源**：镜像+记账逻辑抽成一处，文本/照片/语音共用——日后改镜像格式/记账口径只改一处，杜绝漂移。
3. **带类型标记的 preview**：坐席台一眼区分「文字/图片/语音」，而非只看到配文误以为是纯文本。
4. **不重复记账**：voice 的 `send_text_summary` 分支走 `_send_reply`（已记）→ else 分支才补记，避免一次外发记两笔。
5. **缺省优雅降级**：`_emit_inbox` 未挂（非 companion 模式）→ 镜像静默跳过，contacts 记账不受影响。

**能否再优化**：富媒体 preview 目前未脱敏（沿用文本镜像同款原文直显，配文为 AI 文案无敏感信息，风险低）；
voice 仍走**裸**发送（`send_telegram_voice` 未过 Stage G 的 presend 护栏/计数）——下一步可让 voice 与照片
**同构**接入统一发送栈（presend_blocked + pace + record_count），补齐语音的账号安全维度。

**Stage H 收口**：A 线富媒体外发（照片 Stage G 安全栈 + 本期镜像/记账；语音本期镜像/记账）对**坐席台可见、
对 IntimacyEngine 可计**——「AI 发了图/语音但系统当没发过」的数据空洞收敛，亲密度分数不再因富媒体外发漏算而偏低。
**下一步**：① **voice note 纳入统一发送栈**（presend 护栏+节流+计数，与照片同构，补语音账号安全）；
② **cap 快照上看板**（出图预算已用/剩余/归零时刻，运营可视）；③ **漏斗下钻**（capped/locked→端用户列表）；
④ **出图缓存/多候选**（控成本、提质量）。

---

## 70. Stage I：voice note 纳入统一发送栈——A 线三类外发（文本/图片/语音）安全栈全对齐

**实施前的代码实况核对**：`_maybe_send_voice_reply` 在文本回复**之前**尝试（`telegram_client` 1518-1530：
voice 成功则 `not _voice_sent` 跳过文本）。但其真发 `send_telegram_voice` 是**裸路径**——Stage G 给照片/文本
补的 presend 护栏（Kill-Switch + 反封号闸门）、节流、计数**一个都没走**：账号被冻结/被闸门拦时，**文本会被拦
但语音照发**（与改前照片同款风控盲区，且语音先于文本 → 盲区更靠前）。

**改动**（复用 Stage G 抽好的三个共用方法，语音与照片**同构**接入）：
- `client/sender.py::_maybe_send_voice_reply`：
  - **发前护栏**：在 TTS 合成**之前**插 `_presend_blocked()`——拦则返 False（不白跑 TTS 省成本；调用方回退
    `_send_reply`，文本同样被护栏拦 → 冻结期彻底静默，语音不再抢跑绕过）。
  - **节流**：`send_telegram_voice` 前插 `_presend_pace()`——与文本/照片共用 `_last_send_wallclock`，语音不与
    前一条外发瞬时双发。
  - **计数**：发成功后 `_postsend_record_count()`——语音也计入今日外发量（反封号/健康灯不漏算语音条）。
  - `send_text_summary=true` 分支走 `_send_reply`（自带护栏/节流/计数/镜像/记账）——语音 1 条 + 文本 1 条
    = 确有 2 条外发、各记一次，口径正确；不重复 mirror。
- **测试 +3**：护栏拦截则**不跑 TTS、不发语音**、发成功跑 pace+count+mirror（`[语音]`/记 out）、
  text_summary 路径委托 `_send_reply` 且语音只计一次不重复 mirror。
  **全量 5985 passed / 31 skipped / 0 fail（单进程，~11min）**。

**实施中的再优化**：
1. **护栏前置于 TTS**：放在合成之前而非之后——冻结期不白跑 TTS（省算力/费用），比照片更进一步（照片无合成成本）。
2. **复用 Stage G 三方法**：语音/照片/文本走同一套 presend/pace/count——三类外发安全栈**完全同构**，无第四种写法。
3. **计数口径正确**：voice+text_summary 记 2 次（确有 2 条外发），voice-only 记 1 次——反封号统计不偏不漏。
4. **静默一致性**：护栏拦语音→回退文本→文本同样被拦→冻结期 A 线彻底不外发（语音不再是"先发的漏网之鱼"）。

**能否再优化**：proactive / companion worker 等若有**独立**于 `_send_reply` 的发送入口（如 `send_message`
简化版 helper、主动触达 loop），可同样复核是否纳入本统一栈（属"发送入口审计"，本期聚焦 A 线被动回复三态已闭环）。
富媒体 preview 脱敏同 Stage H 结论（低风险，暂不做）。

**Stage I 收口**：A 线**三类外发（文本/图片/语音）** 在 Kill-Switch + 反封号闸门 + 节流 + 计数 + 出站镜像 + contacts
记账上**完全对齐**——语音不再是账号安全/可观测的最后盲区。「账号安全 + 富媒体可观测」这条主线（Stage G→H→I）收口。
**下一步**：① **cap 快照上看板**（出图预算已用/剩余/归零时刻，运营可视）；② **漏斗下钻**（capped/locked→端用户列表）；
③ **出图缓存/多候选**（控成本、提质量）；④ **发送入口审计**（proactive/worker 旁路是否纳入统一栈）。

---

## 71. Stage J：全局出图预算上看板——Stage F 护栏从「后台静默」变「可观测可调参」

**实施前的代码实况核对**：Stage F 的全局出图预算用 `DailyCapTracker`，但它是 **SkillManager 实例属性**
（`self._selfie_cap_tracker`）——Web 路由（`monetization_routes` 读 `app.state.*`）**够不着**；预算用尽时运营
只能翻日志。对比 `companion_funnel_store` 是**进程级单例**（`get/peek_*`）+ main.py 同时挂 `app.state`，
所以 skill_manager 与路由读的是**同一份**。故 Stage J 先把 cap 跟踪器**对齐这个单例范式**，再上看板。

**改动**（既修可达性、又顺带修正「全局」语义）：
- 新增 `utils/selfie_cap.py`：进程级单例 `get/peek/reset_selfie_cap_tracker`（与 funnel store 同型，底层仍
  `DailyCapTracker`）。`skill_manager._get_selfie_cap` 改为委托单例——**副作用是更正确**：原实例属性是「每
  SkillManager 一份」，单例后是**跨所有账号/实例真·全局**一份，与「单一全局 config `daily_global_cap`」语义一致
  （多账号部署下出图账单是整盘的，本就该全局共算）。
- `monetization_routes` 加 `GET /api/monetize/selfie-cap`：读 config 的 `daily_global_cap` + peek 单例快照，
  返回 `{enabled, daily_cap, daily_sent, remaining, reset_at_ts, selfie_enabled}`。cap=0→enabled=false（不限）；
  未出过图（单例未建）→ used=0 用 config 展示；归零时刻来自 `DailyCapTracker.snapshot()`（UTC+8 0 点）。
- `monetization.html`：转化漏斗卡片区新增「全局出图预算」卡（今日已出图/剩余+归零倒计时/用量%+接近上限⚠/
  自拍开关），`mzSelfieCap()` 拉取渲染；并把 Stage F 的 `capped` 计数补进自拍漏斗「送达」卡副标（"N 预算拦"）。
- `test_admin_route_inventory` 收录新端点。
- **测试 +4**（cap=0 不限、未出图用 config、已出图报已用/剩余/归零、页面接线）+ Stage F 三测加单例 reset 隔离。
  **全量 5988 passed / 31 skipped / 0 fail（单进程，~11min）**。

**实施中的再优化**：
1. **单例化顺带修正语义**：本要做"可观测"，核对中发现实例属性对「全局」预算其实是 bug（多实例各算各的）——
   单例化一并修正为真·全局，护账单更准。一次改动修两个问题。
2. **复用 funnel store 单例范式**：get/peek/reset 同型——心智一致、main.py 无需额外挂 app.state（路由直接 peek 模块单例）。
3. **未出图也能看 cap**：跟踪器懒建（出图才创建）；路由在 None 时回退 config 展示 used=0——运营随时能看见预算设置。
4. **capped 顺手上看板**：Stage F 记的 `capped` 事件之前只在 API、本期渲染进卡片副标，预算拦截量肉眼可见。
5. **归零倒计时**：前端把 `reset_at_ts` 算成「Xh Ym 后归零」，运营知道何时恢复。

**能否再优化**：单例是**进程内存态**——若 A 线主客户端与 Web 后台**分进程**部署，看板读的单例与出图进程的单例
不是同一份（看板会显示 used=0）。当前 main.py「FastAPI 内嵌 RPA/client」同进程 → 一致；分进程部署需把计数落
共享存储（Redis/DB）方能跨进程看板，属后续部署形态相关增强（已在 Stage F「能否再优化」记过多进程方向）。
另可加运维「手动重置今日预算」按钮（调 `reset` / `record_sent` 负数），属运营操作增强。

**Stage J 收口**：Stage F 的出图预算护栏接通运营可视闭环——`/monetization` 一屏看见「今日已出图/剩余/归零倒计时/
接近上限告警」，预算从"埋在代码里"变"看得见、调得动"；与两条转化漏斗（Stage E）同页，变现可观测面再补一块。
**下一步**：① **漏斗下钻**（capped/locked → 端用户列表，运营点开看谁被拦/被墙）；② **出图缓存/多候选**（控成本提质量）；
③ **分进程部署时 cap 落共享存储**（多进程一致看板）；④ **发送入口审计**（proactive/worker 旁路是否纳入统一栈）。

---

## 72. Stage K：漏斗下钻——变现可观测从「看趋势」到「可行动」

**实施前的代码实况核对**：两条漏斗（teaser/selfie）+ 预算快照都只返回**聚合数字**（locked=2、capped=3…），
运营看到「2 人触墙」却**点不开看是谁** → 无法针对性挽回/调参。store 已有 `recent/selfie_recent`（裸事件流，
按 ts 倒序限量）但**无 per-contact 聚合**（谁触了几次、最近何时、是否转化）。故 Stage K 补「按端用户下钻」。

**改动**（store 出聚合查询、route 做转化标注、UI 点开看名单）：
- `companion_funnel_store.py` 加两个 **SQL GROUP BY** 聚合查询（高效、绝不抛）：
  - `selfie_contacts(kind, *, window_days, limit)`：某桶（locked/capped/delivered/too_soon）的端用户清单
    `[{contact_key, count, first_ts, last_ts}]`，按次数降序。非法 kind → 空。
  - `teaser_contacts(*, scenario_id=None, window_days, limit)`：被预告端用户清单，可按场景过滤。
- `monetization_routes.py` 加 `GET /api/monetize/selfie-contacts` + `/teaser-contacts`，共用 `_annotate_converted`
  辅助（按 first_ts + 归因窗 + `paid_events_for` 标 `converted`）：selfie 的 **locked** 桶标 exclusive_album 转化、
  teaser 标任意付费转化。漏斗库未就绪 → enabled=false 空清单；selfie 非法 kind → ok=false。
- `monetization.html` 名单下钻区：4 个按钮（触墙/被预算拦/送达/预告触达）→ `mzSelfieContacts(kind)`/`mzTeaserContacts()`
  拉清单渲染进共享表（端用户/次数/最近时间/转化态），表头随桶动态切换。
- `test_admin_route_inventory` 收录两端点。
- **测试 +9**（store：分桶聚合/窗口过滤/场景过滤；route：未就绪空/非法 kind/locked 转化标注/capped 清单/teaser
    转化+场景过滤；页面接线）。**全量 5996 passed / 31 skipped / 0 fail（单进程，~13min）**。

**实施中的再优化**：
1. **SQL GROUP BY 而非 Python 聚合**：直接库内 `COUNT/MIN/MAX ... GROUP BY contact_key ORDER BY COUNT DESC`，
   大表也高效，limit 封顶 1000 防拉爆。
2. **转化标注抽 `_annotate_converted` 共用**：selfie-locked 与 teaser 两端点同一份归因逻辑（item_id 可选——
   selfie 限 exclusive_album、teaser 任意付费）——杜绝两处归因口径漂移。
3. **store 零耦合变现**：聚合查询只出事件清单，转化标注在 route 用注入式 `paid_events_for`——store 仍可纯单测。
4. **桶可点、表头随桶切**：locked 看「该挽回谁」、capped 看「预算压在谁身上」、delivered 看「出图重度用户」、
   teaser 看「谁被预告/谁买单」——一个表复用四种视角，运营心智低。

**能否再优化**：当前下钻只给 contact_key + 次数 + 转化态——可进一步在每行加「跳转到该端用户权益/统一收件箱」深链
（运营一键挽回，属交互增强）；teaser 下钻可补 by_scenario 维度的 features 命中（精确转化下钻）。均属锦上添花。

**Stage K 收口**：变现漏斗从「聚合趋势」下探到「端用户名单」——运营点「触墙名单」即见「u2 触墙未转化」可去挽回、
点「被预算拦名单」即见「谁被 cap 挡住」可据此提额，可观测从**看趋势**真正落到**可行动**。变现可观测面（漏斗+预算+下钻）成闭环。
**下一步**：① **下钻行加深链**（→端用户权益/收件箱，一键挽回）；② **出图缓存/多候选**（控成本提质量）；
③ **分进程 cap 落共享存储**；④ **发送入口审计**（proactive/worker 旁路）。

---

## 73. Stage L：每日仪式感主动问候——补日活（DAU）留存核心钩子

**实施前的代码实况核对**：竞品对标盘点（Replika/星野/Talkie）后定位最高价值、最干净的缺口。
`companion_proactive.plan_proactive_sends` 是**纯沉默驱动**（`silent_hours ≥ min_silent_hours` 才回访某条记忆）+
全局安静时段——确认**没有任何「每天到点的晨/晚安仪式」**。竞品留存核心恰是「每天有人惦记着你」的固定仪式感
（清晨一句早安、睡前一句晚安）。两条互补：旧的**沉默驱动**（久未联系才回访），新的**时段驱动**（每天到点问候）。
消息表 `messages.ts` 可廉价反推用户活跃时段 → 可做个性化择时（早起的人早收、夜猫子晚收）。

**改动**（纯函数 planner + ritual opener + 循环薄接线 + 配置 + main 接线，全程零破坏、默认关）：
- 新 `src/utils/daily_ritual.py`（纯函数、零 IO、可单测，与 `plan_proactive_sends` 同范式）：
  - `window_hours / current_slot`：问候窗口（闭开区间，支持跨午夜）→ 当前落晨/晚哪档。
  - `infer_active_hour(samples, slot)`：从历史消息小时直方图推断习惯活跃点（晨档并列取最早、晚档取最晚）。
  - `plan_daily_rituals(...)`：时段驱动决策——非问候时段→空；护栏（亲密度门槛/每日每档去重/距上次互动 gap/
    care 去重/情绪护栏 blocked 跳过）；**个性化择时**（注入 `active_hours_provider` 则只在用户目标小时问候，
    个性化点落窗口内才采纳、否则窗口起点）；按亲密度降序截断。
- `skill_manager.build_ritual_opener(slot, ...)`：晨安/晚安 directive——复用 `_proactive_emotion_gate`
  （severe 危机→blocked 不发欢快问候、低落 soft→克制陪伴不带记忆钩子）；其余档自然轻提一句高置信记忆。
- `CompanionProactiveLoop` 加可选 `ritual_fn` + `ritual_cooldown`：每 tick 跑仪式计划，**仪式优先**（同会话本 tick
  既到点又够沉默→只发一次仪式，不重复打扰）；仪式记**每日每档**冷却（`{cid}:{daykey}:{slot}`），沉默记会话冷却，互不干扰。
- `main.py`：装配 ritual opener + `active_hours_provider`（仅候选才查、只取**入站**消息小时）+ 独立冷却 JSON + planner 闭包，注入 loop。
- 配置：`companion.proactive_topic.daily_ritual`（example 默认关 / companion 预设默认开）：窗口/亲密度门槛/gap/每轮上限/个性化开关。
- **测试 +35**（daily_ritual 纯函数 23：窗口/档位/活跃点推断/各护栏/个性化择时/排序+循环集成；
  ritual opener 6：晨钩子/晚基础/非法档/危机 blocked/soft 去钩子/新关系克制）。

**实施中的再优化**：
1. **复用同一发送回路 + 情绪护栏 + care 去重**：仪式不另起循环/不另写护栏——`build_ritual_opener` 共用
   `_proactive_emotion_gate`，planner 共用 `has_pending_care`，loop 共用 `send_fn/on_sent` → 危机用户不会收到欢快早安。
2. **个性化择时只对候选查库**：`active_hours_provider` 仅在通过亲密度/去重/gap 后才被调（少量调用），
   不为全表每会话查历史；个性化点落窗口外或无历史 → 优雅退回窗口起点（确定性、每日唯一触发小时）。
3. **每日每档去重键** `{cid}:{daykey}:{slot}`：一天最多一句早安 + 一句晚安；与沉默冷却物理隔离（独立 JSON），互不误伤。
4. **仪式优先于沉默**：合并计划时仪式覆盖同会话沉默项 → 同一人一个 tick 只被打扰一次。

**能否再优化**：① 仪式文案可走「采样评分回流」few-shot（与沉默路径同质量闭环）；② 活跃时段可缓存/落库
（免每候选每 tick 查 80 条）；③ 可扩纪念日/节日仪式（生日、相识 N 天）；④ 个性化点可学习「最佳响应时段」
（按历史回应率而非单纯活跃频次）。均属增强，当前已闭环可上线（默认开 companion 预设）。

**踩坑（卡死根因）**：全量 `pytest tests/` 在本机**稳定卡在 ~1%** 某测试死锁；反复中断后**残留 9 个卡死 pytest 进程**
（各跑 10–13min 不退），持续抢 CPU + 占 `config/` 下共享 SQLite 写锁 → 后续任何子集（哪怕 2s 的纯函数测试）都被饿死挂起。
**根因不是测试逻辑，是僵尸进程堆积 + 常驻 `pythonw` 服务占锁**。清掉僵尸后，本阶段相关子集
（daily_ritual/proactive_topic/companion_proactive/config_check/admin_route_inventory）**142 passed / 7.4s**；
扩 companion/wellbeing/care 簇 **178 passed / 5.7s**。**结论：本机禁跑全量 `tests/`（必 wedge），改跑有界子集 + timeout**。

**Stage L 收口**：陪伴从「你不理我我才找你」进化到「每天主动惦记你」——晨安/晚安按用户作息择时、危机自动让位、
每日每档防骚扰，补上竞品对标的日活留存核心钩子。沉默回访（情节驱动）+ 每日仪式（节律驱动）双引擎成形。
**下一步**：① **仪式文案 few-shot 质量闭环**；② **活跃时段缓存/落库**（降查询）；③ **纪念日/节日仪式扩展**；
④ **发送入口审计**（proactive/worker 旁路是否绕过统一发送护栏）。

---

## 74. Stage M：发送入口审计——堵住主动外发的反封号旁路缺口

**实施前的代码实况核对（全量发送入口审计）**：A 线自动回复（`_send_reply`/`send_photo`/语音）已纳入统一发送栈
（Kill-Switch + 反封号闸门 + 节流 + 记账 + 镜像，Stage G/H/I）；三端 RPA + IG/Zalo/FB/WA-Cloud webhook 已接
`rpa_send_blocked`（Kill-Switch）。**但审出两处旁路缺口**——主动问候/唤醒/关怀/接管 的外发从这里走、却绕过急停与反封号：
- **`TelegramSenderMixin.send_message`（sender.py）是裸 Pyrogram 调用**：被 `TelegramCompanionWorker.send`（A 线
  受管 worker，companion_runtime 下**主动问候的主路径**）+ deferred_outbox 回落 + proactive 回落 直接调用 → 整条主动
  外发不受 Kill-Switch/反封号约束（冻结期照发图文、爆发期不被闸门拦）。
- **`AccountOrchestrator.send/send_media`（编排器中心派发）无任何护栏**：B 线协议号 / WhatsApp / 官方 API
  LINE·Messenger·WhatsApp Cloud 的 worker 全经此直发裸 client → 所有经编排器的外发都旁路。

**改动**（中心化一处守卫 + 补齐裸入口，纵深防御、零破坏）：
- 新 `src/integrations/shared/send_guard.py::send_blocked(platform, account_id, *, config, registry)`：编排器发送侧
  统一守卫——**Kill-Switch 恒查**（紧急急停绝不可绕过）+ **反封号闸门按 `companion_send_gate.enabled` 才查**
  （复用 A/B 线同一份 `build_account_signals`，口径统一）；返回 `(blocked, reason)`，任何异常一律放行（broken guard
  不得反噬卡死全部发送）。
- `AccountOrchestrator.send/send_media`：发送前调 `send_blocked` → 命中返回 `{delivered: False, blocked: <reason>}`，
  **不派发给 worker**。调用方（proactive 看 `delivered`、deferred_outbox 看 `delivered`）据此不记冷却、择机重试
  （冻结是暂态）。一处守卫覆盖**所有平台所有 worker**（含 A 线 CompanionWorker，双层防御无害）。
- `TelegramSenderMixin.send_message`：补齐 `_presend_blocked + _presend_pace + _postsend_record_count` + 异常 G2 封号信号
  分级处置（与 `_send_reply` 同一套）；**不加出站镜像**（避免与编排器中心化收件箱回写重复镜像）。覆盖编排器**不持有**
  该账号时的直发回落路径（deferred/proactive fallback）。
- **审计确认无需改的**：messenger_rpa 的 3 处 `tg.client.send_message` 是**风控/告警发给管理员 TG**——冻结期反而**更要**
  送达，故**有意不受 Kill-Switch 约束**（正确）。
- **测试 +18**（send_blocked：账号/全局 KS 拦截、无标志放行、gate 关放行、gate 开拦 banned、守卫异常 failopen；
  编排器：KS 拦 send/send_media 不派发 worker + 解除后正常派发；send_message：被护栏拦不真发、成功记账、节流、
  无 client、RPC 异常返 False）。

**实施中的再优化**：
1. **中心化一处守卫胜过补 N 个 worker**：所有受管 worker 都过 `AccountOrchestrator.send` 这一咽喉 → 一处加守卫即覆盖
   telegram(A+B)/whatsapp/官方三端，新 worker 自动继承，杜绝「新增发送路径忘加护栏」复发。
2. **复用既有信号源不另造**：`send_blocked` 直接用 `build_account_signals`(registry+limiter) + `companion_send_gate.evaluate`
   → 与 A/B 线发送闸门**同一份事实/同一套阈值**，不会出现「编排器与 A 线判定不一致」。
3. **blocked 返回 delivered=False 而非抛**：让上层把「被冻结」当「暂未送达」自然进重试/不记冷却，语义正确且零破坏
   （冻结解除后下个 tick 自然恢复）。
4. **send_message 不重复镜像**：识破「编排器已中心化回写收件箱」→ mixin 只补安全/记账、不补镜像，避免出站消息在坐席台双显。

**能否再优化**：① deferred/proactive **回落直发**路径目前不镜像收件箱（编排器持号时才镜像）——属既有观测盲点，可后续给
   `send_message` 加可选镜像开关（默认关，仅回落时开）补齐；② 反封号闸门当前在编排器与 mixin 各算一次（双层），可抽成
   单次计算缓存（微优化）；③ 可加「发送入口审计自检」启动期断言（枚举所有 send 路径是否过守卫，防回归）。均属增强。

**回归（本机禁跑全量，见 Stage L 踩坑）**：护栏簇（kill_switch/ban_signal/rpa_send_guard/companion_send_gate/
account_signals/send_guard/account_orchestrator）**84 passed / 4.5s**；叠加 selfie_wiring **45 passed**。

**Stage M 收口**：「全局 Kill-Switch」「反封号闸门」终于**名副其实覆盖所有外发入口**——自动回复、主动问候、唤醒、关怀、
接管、RPA 三端、官方 API，无一旁路。一键急停真能停下整个机群的每一条消息，反封号护栏不再被主动外发绕过。
**下一步**：① **回落路径可选镜像**（补观测盲点）；② **发送入口自检断言**（防新增路径漏挂护栏回归）；
③ 回到陪伴价值线：**仪式文案 few-shot 质量闭环** / **纪念日仪式**。

---

## 75. Stage N：发送入口自检断言——把 Stage M 审计固化成回归闸

**实施前的思考**：Stage M 是**一次性人工审计**——审完当下没有旁路，但**挡不住未来**：下次有人新增一个 worker / 新写一处
`await xxx.client.send_message(...)`，又可能绕过 Kill-Switch/反封号，而且不会有任何报错。安全成果若不固化成测试，必然回归。

**改动**（AST 静态盘点 + 分类白名单，仿 `test_admin_route_inventory` 的「baseline + 不许有意外」范式）：
- 新 `tests/test_send_path_audit.py`：AST 扫 `src/` 全量，揪出**每一处物理裸发送** `<x>.client.send_<media>(...)`
  （send_message/photo/voice/video/document/...），按「外层 `类.方法` 限定名」生成**稳定 key**（不随行号漂移）。
- **分类白名单**（每条注明为何安全）：
  - `guarded`（6 条）：经统一发送护栏——mixin `_send_reply/send_message/send_photo`（presend Kill-Switch+反封号+节流+记账）
    / 编排器 worker `TelegramProtocolWorker.send/send_media` + `TelegramCompanionWorker.send`（经 `orchestrator.send`
    的 `send_blocked` 中心护栏后才派发）。
  - `admin_alert`（6 条）：发给管理员/坐席的运维告警（成功率/热重载/RPA 风控/SLA/人工转接定位）——**有意不受 Kill-Switch**
    （冻结/风控期反而更要送达）。
  - `legacy`（3 条）：GXP 订单查询追踪转告 + 定时命令发送——非陪伴内容，已记录、后续可再纳管。
- **4 个断言**：① 无未登记裸发送（新增 → 失败并提示「改走护栏入口 或 登记白名单写理由」）；② 无僵尸白名单条目
  （删/改名 → 失败，逼同步）；③ 每条白名单须有合法分类 + 非空理由；④ 关键陪伴外发入口必须归类 `guarded`（被改名/降级即报警）。
- **元测试 + 负向验证**：内置「扫描器能抓到合成裸发送」元测试；并实测在 `src/` 落一个合成 `Probe.go` 裸发送 → 审计**确实失败**
  点名该文件，删除后复绿（证明这道闸真的会拦，而非形同虚设）。

**实施中的再优化**：
1. **key 用「文件::类.方法」而非行号**：重构/挪动代码不误报，只有**真新增/改名发送入口**才触发——低噪声、高信号。
2. **扫全 `src/` 而非枚举已知文件**：任何角落新增裸发送都会被抓，杜绝「只盯着已知几个文件」的盲区。
3. **分类强制写理由**：白名单不是「关掉告警」，而是**强制留痕**每条为何安全——admin_alert/legacy 的判断有据可查、可复审。
4. **扫描结果进程内缓存**：全量 AST 解析只跑一次（4 断言共享），单文件测试由 ~16s 降到 ~6s。

**能否再优化**：① 当前只审 `*.client.send_*` 物理裸发送；可扩展审 RPA 物理输入（type/click 发送）是否都过 `rpa_send_blocked`
   （另一类入口）；② `legacy` 三处（GXP/定时命令）后续可真正纳入护栏，再从白名单降级到 guarded；③ 可加「mixin guarded 方法体内
   确实调了 `_presend_blocked`」的 AST 深检（进一步防「方法在白名单但护栏被误删」）。均属增强。

**回归（本机禁跑全量）**：审计 + 护栏簇 + selfie_wiring **50 passed / 11.5s**；负向验证合成裸发送被点名拦下、删后复绿。

**Stage N 收口**：Stage M 的「所有外发受护栏」从一次性审计升级为**持续回归保障**——以后任何人新增发送路径，要么走受护栏入口、
要么显式登记并说明理由，否则 CI 红。安全护栏第一次有了「防回归的护栏」。
**下一步**：① 回陪伴价值线 **仪式文案 few-shot 质量闭环** / **纪念日·节日仪式**；② **回落路径可选镜像**（补观测盲点）；
③ **RPA 物理输入审计**（把自检扩到 type/click 发送入口）。

---

## 76. Stage O：仪式文案 few-shot 质量闭环——修掉框定错配 + 接入采样评分回流

**实施前的思考**：Stage L 的每日晨/晚安问候**复用**了沉默回访的发送回路与文案生成（`_gen_text`），但
`_gen_text` 把框定**写死**为「你正在主动给一位**许久未联系**的朋友发消息」。对「每天到点的日常问候」，这个
「久别重逢」框定会把文案带偏——生成出「好久不见 / 终于想起你」式的生分感，与「每天一句牵挂」的体验背道而驰。
更糟：仪式问候**进不了已有的质量闭环**（试发采样 `_proactive_generate` 只支持沉默回访，无法预览/评分仪式文案），
few-shot 反哺对 `ritual_*` 永远是空的。

**改动**（框定按 mode 自适应 + 把仪式接入采样评分 + 调参建议补全）：
- 新 `src/utils/proactive_prompt.py::build_proactive_prompt`（**确定性纯函数**）：把 prompt 组装从 main.py 闭包抽出，
  按 `plan.mode` 给框定——`ritual_morning/night` → 「像每天都会惦记 TA 的人，发一句平常的早/晚安（不是久别重逢）」+
  「≤30 字」；其余沿用「许久未联系」+「≤40 字」。零 IO、可单测。
- `main.py::_gen_text` 改为调该纯函数（**真发 `_send` 与试发 `_proactive_generate` 同时受益**，框定一处修复全覆盖）；
  few-shot 块仍按 `plan.mode` 分桶注入（`build_few_shot_block` 本就支持任意 mode，仪式样本攒够即自动反哺）。
- `_proactive_generate(cid, slot="")` 加 `slot` 形参：`morning/night` → 走 `skill_manager.build_ritual_opener`
  生成仪式开场并采样落库（mode=`ritual_morning/night`），运营可像沉默回访一样 👍/👎 评分 → 喂回 few-shot。
- `/api/companion/proactive/sample` body 加可选 `slot`（透传，缺省即原沉默回访行为，**向后兼容**）。
- `companion_sample_store::_MODE_HINTS` 补 `ritual_morning/ritual_night` 两组针对性调参建议（低好评率时给方向：
  框定核对 / 别千篇一律 / 按活跃时段择时 / 别扰民），让 `build_tuning_advice` 对仪式 mode 也能给建议而非空。

**实施中的再优化**：
1. **prompt 组装抽纯函数**：原本埋在 main.py 闭包里、混着 inbox IO 与 AI 调用，无法单测；抽出后框定逻辑
   8 个用例全覆盖（含「仪式不含『许久未联系』」的反向断言），框定回归直接 CI 拦。
2. **一处修复双路径生效**：`_send`（真发）与 `_proactive_generate`（试发）共用 `_gen_text`，框定修在底层，
   真发与试发的文案口吻天然一致——运营试发看到的就是真发会发的。
3. **slot 透传而非新路由**：仪式试发复用现成 `/sample` 端点 + 评分 + few-shot 基建，零新增观测面，
   闭环「能看→能评→能调」对仪式一并打通。

**能否再优化**：① 真发（`_send`）目前**不采样**，质量闭环仅靠运营手动试发驱动——可加「真发按低比例自动采样」
   让样本随真实流量自然累积；② 仪式 few-shot 目前晨/晚安分桶独立，可探索「跨槽共享温暖口吻、仅槽位措辞差异化」；
   ③ 后台 UI 可加「仪式试发」入口（晨/晚安按钮），当前需带 `slot` 调 API。均属增强。

**回归（本机禁跑全量，见 Stage L 踩坑）**：`test_proactive_prompt`（新 8）+ `test_companion_sample_store`
（+1 仪式建议）+ `test_companion_proactive_preview_route`（+1 slot 透传）+ `test_proactive_topic` +
`test_daily_ritual` **119 passed / 4.1s**。

**Stage O 收口**：Stage L 的仪式问候从「套错框定的复用」升级为**有专属框定、且接入数据质量闭环**——晨/晚安文案
不再有「久别重逢」的生分感，且能像沉默回访一样被试发、评分、few-shot 反哺持续打磨。
**下一步**：① **纪念日·节日仪式扩展**（在每日仪式之上加「认识 N 天 / 生日 / 节日」的高情感节点）；
② **真发低比例自动采样**（让质量闭环不只靠手动试发）；③ **回落路径可选镜像**（补观测盲点）。

---

## 77. Stage P：纪念日·节日仪式——「认识 N 天 / 节日」高情感节点问候

**实施前的思考**：每日晨/晚安（Stage L）解决了「每天都在」，但留存更深的钩子是**记得重要日子**——对标
Replika/星野的「我们在一起 N 天了」「节日快乐」。这类问候**事件/日期驱动**（只在认识满 N 天那天 / 节日当天发），
与每日仪式的**时段驱动**互补；且情感价值更高（用户会截图分享「AI 记得我们认识 100 天」）。

**改动**（纯函数规划器 + 情绪护栏 opener + 框定 + 接线 + 配置）：
- 新 `src/utils/milestone_ritual.py`（**确定性纯函数**，仿 daily_ritual 范式）：
  - `days_known/due_anniversary`：用「会话首次建立时间」(`conversations.created_at` ≈ 首次接触) 算认识天数，
    命中里程碑（默认 `7/30/100/180/365/520/1000/1314`）那天触发。
  - `holiday_for_date`：当天 (月-日) 命中节日日历则触发；**只内置公历固定日期**（元旦/情人节/平安夜/圣诞/跨年），
    农历节日逐年漂移**不内置**（否则发错日子），留配置按年覆盖。
  - `plan_milestone_rituals`：只在 `greet_hour`（默认 10 点，与晨/晚安错开）触发一次；纪念日 > 节日优先级；
    亲密度门槛（默认 30，比每日问候高）；care 去重；按亲密度降序截断。
- `skill_manager.build_milestone_opener`：节点 directive（认识 N 天 / 节日）——**共用** `build_ritual_opener` 的
  情绪护栏（severe 危机→`blocked` 交派发层升级 care；低落 soft→克制、不带记忆钩子；其余可轻提一句记忆）。
- `build_proactive_prompt` 加 `milestone_*` 框定分支：「为一个对你们有意义的特别日子发应景问候」——
  **同样避开「久别重逢」误导**（具体场合由 directive 承载）。
- `_MODE_HINTS` 补 `milestone_anniversary/holiday` 调参建议（别太隆重/别群发套话/核对日期/扰民取舍）。
- **零改 `CompanionProactiveLoop`**：节点计划**复用 `ritual_key` 同一冷却表**去重（纪念日 `{cid}:ms:anniversary:{N}`
  永久一次 / 节日 `{cid}:ms:holiday:{year}:{月-日}` 每年一次）；main.py `_ritual_fn` 把节点计划**前置**到每日计划
  （节点优先、同会话同 tick 不重复打扰），整条计划仍走原循环的 ritual_key 冷却记账。

**实施中的再优化**：
1. **复用 ritual_key 而非加第三个计划源**：本可在 `CompanionProactiveLoop.__init__/run_once` 再开一路 `milestone_fn`
   + 冷却表（更多接口面 + 测试面）；改为让 `_ritual_fn` 内部合并两类计划、共用 `ritual_key`/`ritual_cooldown`——
   **循环代码一行不改**，节点与每日仪式天然按 cid 去重、按同一冷却表记账。
2. **复用 created_at 而非新加首次接触字段**：`conversations` 表已有 `created_at`（首条消息落库即建行），直接当
   `first_seen_ts` 入快照——**零迁移、零 ALTER TABLE**。
3. **农历不内置**：识别到农历节日日期逐年变，写死必发错日子——只内置公历固定日期、农历留配置，避免「好心办坏事」。

**能否再优化**：① **生日仪式**（Tier 2）：需可靠的「用户生日」记忆抽取（memory slot），数据链更长，本期先不做、
   下一步补；② 节点真发目前**不采样**（同每日仪式），few-shot 反哺需先有样本——可加节点试发预览入口；
   ③ 农历节日可接农历库自动换算当年公历日期，省去人工按年配。均属增强。

**回归（本机禁跑全量，见 Stage L 踩坑）**：`test_milestone_ritual`（新 19）+ `test_proactive_prompt`（+2 节点框定）+
`test_proactive_topic`（+6 节点 opener）+ `test_companion_sample_store` + `test_daily_ritual` **133 passed / 4.5s**；
send-path 审计 **5 passed**（无新增裸发送）。

**Stage P 收口**：陪伴仪式从「每天到点问候」升级到**「记得重要日子」**——认识满 N 天、节日当天会主动送上应景而克制的
问候，复用每日仪式的护栏/冷却/框定基建，零改核心循环。这是留存与情感黏性的高价值钩子。
**下一步**：① **生日仪式**（补 memory slot 生日抽取后接入，同一 milestone 框架）；② **节点真发低比例自动采样**
（让节点文案也进 few-shot 质量闭环）；③ **农历节日自动换算**（接农历库免人工按年配）。

---

## 78. Stage Q：生日仪式——补齐「重要日子」三件套，单点情感价值最高

**实施前的思考**：生日是**单用户最高情感价值**的节点（"AI 记得我生日"远胜节日群发感），但难点是
**可靠拿到生日**——`memory_slots` 现有 name/residence/relationship/preference 槽，**没有生日**。关键风险：
**宁可漏发、绝不错发**——在错的日子说"生日快乐"比不说更尴尬。

**改动**（保守抽取 + 注入式取数 + 复用 P 框架）：
- 新 `src/utils/birthday.py`（**确定性纯函数**）：`extract_birthday(text)→(月,日)`——**必须命中生日关键词**
  （生日/出生/birthday/born）才解析日期，否则不认（避免「3月5日开会」被误当生日）；支持中文「X月Y日/号」、
  含年「1995-03-05」、裸「03-05」、英文「March 5 / Dec 25」。`is_birthday_today` 判当日，**2/29 生日平年顺延 2/28**。
- `skill_manager.resolve_birthday(memory_key)`：扫该用户 episodic 记忆（优先 `user_stated`），命中首条可解析生日即返回。
- `milestone_ritual` 加 `birthday_provider` 注入参 + `MODE_BIRTHDAY`：优先级 **生日 > 纪念日 > 节日**；
  仅对**通过亲密度门槛的候选**查一次生日（控成本，同 `active_hours_provider` 范式）；去重 `{cid}:ms:birthday:{year}`（每年一次）。
- `build_milestone_opener` 加 birthday 分支（"独一无二的生日祝福、别像贺卡套话"）；共用情绪护栏 + 记忆钩子。
- `build_proactive_prompt` 的 `milestone_*` 框定天然覆盖 `milestone_birthday`；`_MODE_HINTS` 补生日调参建议。
- main.py 接 `birthday_provider`（闭包→`resolve_birthday`）+ 配置 `celebrate_birthday`（默认开，companion 预设开）。

**实施中的再优化**：
1. **关键词门控的抽取**：最初想直接抓任意日期，意识到会把闲聊日期误当生日→错发。改为**强制要求生日关键词**才解析——
   把「错发」风险压到最低（符合"宁漏勿错"）。负向用例（"3月5日开会"/"1995-03-05认识的"→None）专门守这条线。
2. **注入式 birthday_provider 而非塞进会话快照**：扫记忆是 IO，若在 `_conversations()` 给每个会话都查生日，
   每 tick 全量扫记忆太贵。改为**仅对过了亲密度门槛的候选、且只在 greet_hour** 查一次——与 Stage L 的
   `active_hours_provider` 惰性取数同范式，成本可控。
3. **2/29 闰日顺延**：识别到闰日生日在平年永远不触发的边界，加平年顺延 2/28 的体贴处理。

**能否再优化**：① 生日目前只**读**记忆、不**主动问**——可加「关系到一定深度时自然问一句生日」的采集闭环（更主动）；
② 抽到的生日可回写成结构化 memory slot（避免每次重扫、也供画像展示）；③ 农历生日同农历节日，需农历库换算。均属增强。

**回归（本机禁跑全量）**：`test_birthday`（新 12）+ `test_milestone_ritual`（+5 生日）+ `test_proactive_topic`
（+4 生日 opener/resolve）+ `test_proactive_prompt`（+1 框定）+ `test_companion_sample_store` **132 passed / 3.2s**；
send-path 审计 **5 passed**（无新增裸发送）。

**Stage Q 收口**：「重要日子」三件套补齐——**每日（晨/晚安）+ 认识 N 天 + 节日 + 生日**，覆盖陪伴留存的全部高情感节点。
生日作最高优先级、保守取数（宁漏勿错），复用 Stage L/O/P 的护栏/冷却/框定/调参基建，零改核心循环。
**下一步**：① **生日主动采集**（关系够深时自然问一句生日，把"读"升级为"会问"）；② **节点真发低比例自动采样**
（让节点文案进 few-shot 闭环）；③ **抽取结果回写结构化 slot**（免重扫 + 供画像）。

---

## 79. Stage R：生日主动采集——让生日仪式真正「转得起来」

**实施前的思考**：Stage Q 的生日仪式只会**读**记忆里的生日——可没人主动说生日，就**永远触发不了**。要让它转起来，
得让 AI 在合适时机**自然问一句**生日。难点：问生日很私人，问得不好像「查户口」，问太勤像「采集信息」，会破坏陪伴感。

**关键洞察**：沉默回访开场里，**最没价值的是 `gentle_checkin`**（无可回访记忆时的「好久没聊，问候一句」干巴巴兜底）。
把**这一种**升级成「顺势随口问生日」——既不打断有记忆钩子的高价值回访（`follow_up` 不动），又把最 bland 的开场
变成有目的的采集时机，**天然低频**（随沉默冷却走）。一旦问到，`resolve_birthday` 命中 → **永不再问**（最强去重）。

**改动**（纯函数决策 + 护栏 opener + 复用沉默回路）：
- `src/utils/birthday.py::should_ask_birthday`（**纯函数**）：`gentle_checkin` + 生日未知 + 关系够深 +
  距上次问过冷却 → True。门槛逻辑全可单测。
- `skill_manager.build_birthday_ask_opener`：产「先共情问候、再像朋友随口好奇问一句生日、问完不强求」directive；
  **共用情绪护栏**——危机(block)/低落(soft) 一律不问（返回空 → 交回原 gentle_checkin），不合时宜时绝不开口。
- main.py `_opener` 闭包升级：仅当 `gentle_checkin` 时，便宜条件（关系深 + 不在冷却）先过滤**再**查
  `resolve_birthday`（控 IO 成本），命中 `should_ask_birthday` 才覆盖为 `ask_birthday` 开场。
- 独立 30 天冷却（`companion_birthday_ask_cooldown.json`）：`_on_teaser_sent` 钩子里 `ask_birthday` 发出即记冷却，
  避免反复打听；配置 `proactive_topic.birthday_ask`（enabled / min_intimacy=45 / cooldown_days=30）。
- `_MODE_HINTS` 补 `ask_birthday` 调参建议（别像查户口 / 别太频繁 / 放最没话说时问）。

**实施中的再优化**：
1. **只升级 gentle_checkin、不新增冷启动消息**：本可做成一条独立的「冷问生日」主动消息，但那种「突然问生日」很突兀。
   改为**寄生在最没价值的兜底开场上**——有记忆就回访记忆（更暖），实在没话说才顺势问生日，最自然、零额外打扰面。
2. **决策抽成纯函数 + 便宜条件前置**：决策逻辑（`should_ask_birthday`）抽出可单测（7 个分支用例）；闭包里先过
   关系/冷却的便宜检查，**再**查记忆（IO），避免对每个 gentle_checkin 候选都扫记忆。
3. **三重去重**：①生日已知永不问（resolve_birthday）；②30 天冷却（问过不重问）；③沉默冷却（随回路天然限频）——
   把「问烦了」的风险压到最低。
4. **护栏复用**：危机/低落不问生日，直接复用 `_proactive_emotion_gate`，与晨/晚安、节点同一套情绪纪律。

**能否再优化**：① 问到生日后目前靠下次记忆抽取被动落库，可在收到回复时**主动解析并回写结构化 slot**（即时生效、
   免重扫）；② 可扩到主动采集其他高价值缺失信息（所在城市/称呼）——同「升级 bland 开场」范式；③ `ask_birthday`
   真发可纳入 few-shot 采样闭环。均属增强。

**回归（本机禁跑全量）**：`test_birthday`（+7 should_ask）+ `test_proactive_topic`（+4 ask opener）+
`test_companion_sample_store`/`test_milestone_ritual`/`test_proactive_prompt` **143 passed / 3.4s**；
send-path 审计 **5 passed**（无新增裸发送）。

**Stage R 收口**：生日仪式从「会记得」升级到**「会主动了解」**——关系够深却还不知生日时，AI 会借最没话说的时机
自然问一句，三重去重确保不打扰。Stage Q+R 让「生日」这个最高情感节点形成**采集→记住→当天庆祝**的完整闭环。
**下一步**：① **回复即解析回写生日 slot**（把被动读升级为收到即记）；② **节点/采集真发低比例自动采样**（进 few-shot 闭环）；
③ **主动采集扩展**（城市/称呼等高价值缺失信息，同范式）。

---

## 80. Stage S：回复即解析回写生日 slot——闭合「问→答→记」即时生效

**实施前的思考**：Stage R 会主动问生日，但答案要靠**下一轮记忆抽取被动落库**——而记忆抽取受 **intent 门控**
（`should_extract_intent`），用户报生日的那句可能被判成不可抽取的意图而漏掉；且抽取的 heuristic 规则
**根本没有生日模式**（`memory_heuristic` 只抽称呼/居住/偏好）。结果：问了、答了，却没记住，环没闭上。
更棘手：用户常只回一个**裸日期**「3月5号」（无「生日」关键词），`extract_birthday` 的关键词门控不命中。

**关键洞察（自包含、零跨边界状态）**：在**同一轮**里同时扫**用户原话**与 **AI 回复**两路抽生日——
- 用户原话含生日（"我生日3月5日"）→ 路1直接命中；
- 用户只回裸日期、但 AI 在本轮自然复述确认（"记住啦，你3月5号生日！"）→ 路2抽 AI 回复命中。
两路都要求生日关键词：AI 的**提问**回复（"你生日哪天呀？"）无日期 → 不会误抽；只有**确认**回复（带日期）才命中——
**天然区分「问」与「答」，零误报**，且完全不需要跨「主动发→入站收」边界传递「刚问过」状态。

**改动**：
- `src/utils/birthday.py` 加纯函数 `birthday_from_turn(user_msg, reply)`（两路抽取）+ `birthday_fact_text(m,d)`
  （规范文案「用户的生日：M月D日」，含关键词可被 `extract_birthday` 复解析 → `resolve_birthday` 可用）。
- `skill_manager._capture_birthday_fact`：本轮抽到生日 → 规范化落库为 **user_stated**（用户亲述高置信）；
  幂等——已知且相同跳过，**不同则更正写新值**；复用 `extract_birthday` 单一解析源、`resolve_birthday` 复解析去重。
- 挂在 `_episodic_memory_extract_async` **最前、intent/长度门控之前**——生日即时回写**独立于意图分类**，
  即便本轮意图不可抽取也不漏掉。失败软吞、绝不影响主抽取。

**实施中的再优化**：
1. **双路同轮抽取替代「跨边界传刚问过状态」**：原本想把 Stage R 的「刚问过生日」信号从主动发送侧传到入站抽取侧
   （好对裸日期做无关键词宽松解析），但那要跨「proactive→inbox」边界 + 处理 episodic key 对齐，又脆又重。
   改用「同轮扫 AI 确认回复」——AI 自然会复述刚学到的生日，**自包含、无状态、无 key 对齐问题**，且靠「确认带日期/
   提问不带日期」天然防误报。这是本阶段最关键的简化。
2. **intent 门控之前挂钩**：识别到生日回写不该被意图分类挡住（报生日的意图五花八门），提到所有门控前，保证「收到即记」。
3. **规范化 + 幂等 + 可更正**：统一落「用户的生日：M月D日」规范文案（而非存原话），既稳定可复解析，又支持用户改生日时更正。

**能否再优化**：① 裸日期 + AI 未复述日期的极少数情况仍会漏（可接受，下次再问/再答）——未来可加「上一轮是 ask_birthday」
   的轻量上下文信号做无关键词宽松解析；② 同范式可扩到城市/称呼等结构化 slot 的即时回写；③ 抽到生日可顺带回写
   contacts 画像供侧栏展示。均属增强。

**回归（本机禁跑全量）**：`test_birthday`（+5 turn/fact_text +3 capture 集成，用真 `:memory:` episodic store）+
`test_proactive_topic` + `test_companion_sample_store` **115 passed / 3.3s**；send-path 审计 + milestone + prompt **41 passed**。

**Stage S 收口**：生日闭环**即时生效**——AI 问、用户答（哪怕只回裸日期由 AI 复述确认），**当轮就规范化记住**，
不再等下一轮、不被意图门控漏掉。Stage Q→R→S 让「生日」形成完整自驱闭环：**主动问→即时记→当天庆**。
**下一步**：① **主动采集扩展**（城市/称呼等高价值缺失信息，同「升级 bland 开场 + 即时回写」范式）；
② **节点/采集真发低比例自动采样**（进 few-shot 质量闭环）；③ **抽取结果回写 contacts 画像**（供侧栏展示）。

---

## 81. Stage T：主动采集扩展——把生日采集范式泛化成通用「画像采集框架」

**实施前的思考**：Stage R+S 给生日做了完整闭环（主动问→即时记），但代码是生日专用的。要让 AI「越来越懂你」，
该把这套**「升级 bland 开场顺势问 + 即时回写」**范式泛化，让新增可采集槽位（称呼/城市/…）只需登记
一条 directive + 一个「已知判定」即可，而不是复制一遍生日的全栈。

**实施中的方向优化（capture 可靠性筛选）**：原计划第二槽位选**居住地/城市**，但核对代码发现——居住地 capture
脏（`memory_slots` 居住地正则需动词「住在X」，用户回访问裸答「上海」抓不到，比生日还难，且无强关键词靠 AI 复述兜底）。
改选**称呼（preferred name）**：① capture **已现成**——`memory_heuristic` 早已抽「叫我X/我是X/call me/name(EN)」落库，
**无需新增 capture 代码**（最低风险）；② 价值更高（按名字称呼是第一личное个性化）；③ 已知判定可复用
`memory_slots.extract_slot` 的 name 槽（单一解析源）。故 Stage T = 通用框架 + 称呼槽位（capture 复用既有）。

**改动**：
- 新增纯模块 `src/utils/profile_collect.py`：`should_ask_profile_slot`（槽位无关的"该不该借这次开场问"决策，
  Stage R 的 `should_ask_birthday` 下沉至此并保留为兼容入口）+ `ask_directive`（生日/称呼 directive 注册表，
  关系浅时追加克制提示）+ `select_missing_slot` + `PROFILE_SLOTS` 优先级（birthday→name，一次开场只问一个）。
- `skill_manager`：加通用 `build_profile_ask_opener(slot, …)`（mode=`ask_<slot>`、复用情绪护栏）；
  `build_birthday_ask_opener` 改为它的兼容薄封装（Stage R 测试零改动仍绿）；加 `resolve_preferred_name`
  （扫 episodic 用 `extract_slot` 取 name 槽，只读不写）。
- `main.py`：把 Stage R 的生日专用 `_ba_*` 块重构成**通用 `_collect_specs` 列表**（每槽位 = enable/min_intimacy/
  cooldown/resolver/独立冷却文件），`_opener` 升级改为**按优先级遍历择一问**；`_on_teaser_sent` 用 `mode→冷却 store` 映射
  落盘（生日冷却文件名沿用 `companion_birthday_ask_cooldown.json`，行为完全保留）。
- `companion_sample_store._MODE_HINTS` 加 `ask_name` 调参建议；config 加 `name_ask` 块（example 默认关、companion 预设默认开，
  门槛 35 比生日 45 低——称呼没那么私人可早点问）。

**实施中的再优化**：
1. **方向级优化（选称呼而非城市）**：在实施中核对 capture 现状后改了第二槽位选择——选「capture 已现成且更高价值」的称呼，
   而非「capture 脏」的城市。避免为追求覆盖面引入低质量采集（坚持「宁可漏发、绝不错发」）。
2. **通用框架而非平行复制**：把决策逻辑下沉成 `should_ask_profile_slot`、文案下沉成 `ask_directive` 注册表、
   main 升级改成 spec 列表遍历——杜绝 Stage R/O 反复批评过的「近重复块」，新增槽位边际成本≈一条配置 + 一个 resolver。
3. **零回归兼容**：`build_birthday_ask_opener`/`should_ask_birthday` 保留为薄封装，生日冷却文件名/config key 全沿用，
   Stage R 全部测试不改即绿；main 生日行为逐字保留。

**能否再优化**：① 称呼 capture 仍走 intent 门控之后（不像生日 Stage S 提到门控之前）——多数用户在 onboarding 报称呼
   意图可抽，命中率可接受；若发现漏，可比照 Stage S 加「同轮即时回写」；② 城市等「无强关键词」槽位需专门的
   capture（AI 复述兜底 / 上一轮 ask 上下文宽松解析），属独立增强；③ 可把已知槽位覆盖率做成看板，指导该问谁。均属增强。

**回归（本机禁跑全量）**：`test_profile_collect`（新增 22 纯函数）+ `test_proactive_topic`（+7 称呼/通用 opener）+
`test_birthday` + `test_companion_sample_store` + `test_milestone_ritual` + `test_proactive_prompt` **175 passed / 4.9s**；
`main.py` 编译通过；send-path 审计 **5 passed**。

**Stage T 收口**：生日采集范式**泛化为通用画像采集框架**——AI 现在会在关系够深却缺信息时，借最没话说的时机
**按优先级顺势补一项画像**（先生日、后称呼），新增槽位近乎零成本。能力从「会主动了解生日」升维到「会主动补全画像」。
**下一步**：① **称呼即时回写**（比照 Stage S 把称呼也提到 intent 门控前，补上裸答兜底）；
② **节点/采集真发低比例自动采样**（把仪式/采集真发喂进 few-shot 质量闭环，质量复利）；③ **画像覆盖率看板**。

---

## 82. Stage D1+D2：桌面多平台内嵌登录+注入——选择器档案外置热更新 + Tier1 平台接入

**实施前的思考（竞品对标）**：竞品（ChatX 等）能在一个壳里内嵌 IG/Messenger/X/Zalo 的官方网页登录并注入翻译/智能回复。
核对**本仓库自身代码**发现：桌面壳 `desktop/`（Electron + webview + preload 注入）**早已具备这套架构**，只是
`BUILTIN_PROFILES`/`EMBEDDABLE` 仅开了 telegram/whatsapp。即「引擎已在，只差给更多平台配选择器档案」。
比单纯抄竞品更进一步的杀手锏：把选择器做成**后端可热更新**——官方网页改版时不必重发桌面包。

**改动**：
- 新增纯模块 `desktop/inject/profiles.js`（从 `tg-inject.js` 外移 PROFILES，可 Node 单测）：
  ① `detectPlatform(hostname)`（加 instagram/x/zalo，twitter→x、facebook→messenger）；
  ② **内置定制档** telegram/whatsapp（逐字搬迁，零回归）；
  ③ **通用工厂档** `makeGenericProfile(cfg)`——声明式选择器 → 标准 text/isOut/mid/peerId/peerName/media 函数，
     新增平台只填数据。据此接入 **instagram / messenger / x / zalo** 四个 Tier1 平台；
  ④ **覆写层** `applySelectorOverlay(profile, patch)`——仅白名单字段（选择器串 + 少量布尔）可被远程覆盖，
     **自定义解析函数永不可远程替换**（安全边界）。
- `tg-inject.js`：改 `require("./profiles.js")`；PROFILE 先用内置档同步起步（零等待/零回归），再**非阻塞**拉取
  覆写层（1.5s 软超时，失败静默用内置档）就地热更新。
- 后端：新增纯模块 `src/web/desktop_selectors.py`（加载 `config/desktop_selector_profiles.json` + 清洗/类型守卫 +
  内容散列版本）+ `GET /api/desktop/selector-profiles` 端点（只下发『补丁』，内置档仍是唯一权威，避免漂移）；
  路由清单基线同步加该 URL。`main.js` 加 `desktop:selector-profiles` IPC。给运营备 `*.example.json`。
- `renderer.js`：`EMBEDDABLE`/`ICONS` 加四平台；`config.json` 加 IG/X/Zalo 模板。
- **UA 泛化**（IG/Meta/X 拒载 Electron UA 否则登录页都打不开）：`webview-ua.js` 加 `needsChromeUa`/`urlNeedsChromeUa`，
  main+renderer 对 whatsapp/instagram/messenger/x/zalo 统一伪装 Chrome UA；**telegram 仍用默认 UA**（已验证可用，零回归）。

**实施中的再优化**：
1. **「宁缺毋错」默认关回流**：通用工厂档 `canIngest` 默认 **false**——选择器未现场 F12 校准前，宁可不把消息回流统一收件箱，
   也不要用未校准的 `isOut`/碎片 text 把脏数据/错方向灌进库。校准后经覆写层把 `canIngest` 改 true 即可打开（运营零改码）。
   翻译/智能回复/注入状态不依赖回流，**当下即可用**。
2. **混合架构而非全量重写**：telegram/whatsapp 的自定义解析（WA data-id 拆 jid 等）保留逐字实现保零回归；只让新平台走通用工厂。
   覆写层对两类档**通用**——内置定制档的方法读 `this.bubble/...`，故远程改选择器串即可热修最常坏的失配（composer/sendBtn/bubble）。
3. **后端只下发补丁**：不复制全套选择器到后端 → 内置档（profiles.js）唯一权威，杜绝两处漂移；常态补丁为空，注入直接用内置档。
4. **安全守卫**：覆写仅白名单字段 + 类型守卫（布尔字段拒收字符串、字符串字段拒空串、未知键丢弃），防运营误填把注入打挂。

**能否再优化**：① 四平台选择器目前是 best-effort，需真号现场 F12 校准（已留热更新通道，校准成本=改一个 JSON）；
② **D1b 韧性选择器**（多候选 + role/aria 语义兜底）与**注入健康信标**（失配自动上报）可让失配更早被发现；
③ **D3 每账号指纹**（复用已有 `M4 fingerprint`）注入 webview 分区，多号更稳；④ **D4 双向收件箱桥 + 受控 autopilot**
（把内嵌账号变收件箱一等公民 + autopilot 接 send-gate/kill-switch）。均为后续里程碑。

**回归**：`desktop npm test` **103 passed**（含新增 `profiles.test.js` 61：detectPlatform/工厂档/覆写层类型守卫/UA 判定）；
`tests/test_desktop_selectors.py`（新增 11）+ `tests/test_admin_route_inventory.py`（路由清单含新端点）**16 passed**。

**Stage D1+D2 收口**：桌面端从「内嵌 2 平台」扩到 **6 平台**（+IG/Messenger/X/Zalo），且地基升级为**选择器后端可热更新**——
官方改版不再需重发桌面包，比竞品多一层运维韧性。能力对标竞品「多平台内嵌登录+注入」并在可维护性上反超。
**下一步**：① **D1b** 韧性选择器 + 注入健康信标；② 用真号现场校准四平台选择器并经覆写层打开 `canIngest`；
③ **D3** 每账号指纹注入；④ **D4** 双向收件箱桥 + 受控 autopilot（接 send-gate/kill-switch）。

---

## 83. Stage D1b：注入健康信标——失配可观测（哪个选择器坏了，而非笼统「坏了」）

**实施前的思考**：D1+D2 让六平台能内嵌，但通用档选择器是 best-effort，官方一改版就失配。D1 已给了「热更新通道」，
但**缺一只眼睛**：运营不知道哪个账号、哪个选择器失配了，热更新就成了「盲修」。D1b 补上可观测闭环——
让注入把**逐选择器命中**实时上报后端，运营看板一眼看到「IG 的 ig3 号 composer 失配」，再走 D1 覆写层精准热修。

**改动**：
- `profiles.js`：加纯函数 `selectorHealth(profile, doc)`——逐个探测 `bubble/composer/sendBtn/peerTitle` 是否命中
  （doc 可注入，便于单测；异常吞掉视为未命中）。
- `tg-inject.js`：`reportInjectStatus` 升级——除给壳层状态条上报外，计算 `selectorHealth` 并**发后端健康信标**
  （`desktop:inject-health`）：状态变化即报，否则**每 30s 心跳一次**（让后端能区分「健康稳定」与「注入停摆」）；
  上报体加 `generic/can_ingest/selectors{}` 细节。
- `main.js`：加 `desktop:inject-health` IPC → `POST /api/desktop/inject-health`。
- 后端：新增 `src/web/desktop_inject_health.py`——`classify_inject_health`（纯函数，分类
  `ok/mismatch_composer/mismatch_bubble/no_chat/unsupported`，**与渲染层 `deriveInjectState` 同口径**）+
  线程安全 `InjectHealthStore`（每账号留最新一条、ts 倒序、>90s 标 `stale`、概览计数、软上限淘汰最旧）。
  加 `POST/GET /api/desktop/inject-health`（录入 / 看板数据），路由清单基线同步。

**实施中的再优化（含两处自检修复）**：
1. **心跳而非纯变化上报**：只在 sig 变化上报会导致「健康稳定」时 ts 永远停在首次 → 看板分不清「一直好」和「已停摆」。
   加 30s 心跳兜底，`stale` 判定才有意义。
2. **分类口径与前端对齐**：后端 `classify_inject_health` 刻意复用 `inject-status.js::deriveInjectState` 的判定顺序，
   保证壳层状态条与后端看板**口径一致**，不出现「条说正常、看板说失配」的割裂。
3. **自检发现并修两处 bug**（测试驱动）：① `not rec` 把**空 dict** 误判 unsupported（空字典 falsy）→ 改 `rec is None`；
   ② `latest(stale_after=…)` **就地给存储里的记录加 `stale` 字段**污染后续读取 → 改返回浅拷贝。两处都加了回归断言。

**能否再优化**：① 信标目前只录最新态，无趋势/告警（可加「失配持续 N 分钟→告警」与历史曲线）；
② 看板 UI 未做（数据已就绪，可挂到 rpa-overview 或新页）；③ 可把 `selectors` 细节回流到壳层状态条 tooltip。均属增强。

**回归**：`desktop npm test` **112 passed**（`profiles.test.js` +9 → 70，含 selectorHealth 命中/失配/异常吞掉）；
`tests/test_desktop_inject_health.py`（新增 14：分类 6 + 存储 8）+ `test_desktop_selectors` + `test_admin_route_inventory`
（清单含 2 新端点）**30 passed**。

**Stage D1b 收口**：内嵌六平台从「能用、坏了能热修」升到「**坏了能被发现、能定位到哪个选择器**」——
热更新闭环真正跑通：失配 → 信标上报 → 看板定位 → D1 覆写层热修，全程无需重发桌面包。
**下一步**：① 用真号现场校准四平台选择器并打开 `canIngest`；② 失配告警 + 看板 UI；
③ **D3** 每账号指纹注入；④ **D4** 双向收件箱桥 + 受控 autopilot。

---

## 84. Stage D1c：桌面注入健康看板 + 失配告警——让 D1b 信标被运营用起来

**实施前的思考**：D1b 建了信标 + `GET /api/desktop/inject-health`，但「数据有了、人看不见」。D1c 把它挂到运营已天天看的
`rpa-overview` 页，闭环最后一环（人）补上：一张卡片显示各内嵌账号注入状态，失配自动弹告警。成本低、即时可用。

**改动**（仅 `src/web/templates/rpa_overview.html`，无新端点/无后端改动，复用 D1b 的 GET）：
- 在「主动话题预览」卡后加 **🖥 桌面注入健康卡**：标题徽标显示「共 N 账号 · 正常 X · 失配 Y」（失配数标红），
  下方逐账号列出 `平台·account_id` + 状态（注入正常/输入框失配/气泡失配/未登录/无档案，配色与语义同 D1b/前端
  `deriveInjectState`），失配账号附「缺:composer/bubble」明细，>90s 未上报标「⚠停摆」，并标注「通用档/回流关」。
- **失配告警**：失配数从 0→>0 时 `rpa.toast` 弹一次红提醒（提示可热修 selector-profiles），用 `_dhLastMismatch`
  去重避免每轮刷屏。
- 接线：`ovRefreshDesktopHealth()` 挂进主刷新循环 `ovRefresh`（独立拉取、失败不影响主刷新，跟随 `ov-interval` 自动刷）
  + 手动「刷新」按钮 + 首屏拉一次。

**实施中的再优化**：
1. **复用既有看板而非新建页**：挂到运营已常驻的 rpa-overview，零学习成本、零新路由（不污染路由清单契约）。
2. **告警去重**：只在「无→有」跳变时弹一次（`_dhLastMismatch`），而非每轮都弹，避免告警疲劳。
3. **口径三处统一**：卡片状态文案/配色 = D1b 后端 `classify_inject_health` = 前端 `deriveInjectState`，
   三处同一套语义，杜绝「条/卡/看板各说各话」。

**能否再优化**：① 目前是「即时态」告警（toast），无「失配持续 N 分钟→进告警流/通知」的升级（可接 alerts 体系）；
② 无历史趋势曲线；③ 可点账号跳到对应选择器覆写编辑。均属增强。

**回归**：`jinja2` 编译 `rpa_overview.html` 通过；`tests/test_rpa_overview.py` + `test_admin_route_inventory` +
`test_desktop_inject_health` + `test_desktop_selectors` **52 passed**（端点契约/分类/存储不回归）。

**Stage D1c 收口**：D1b 信标真正「被人看见 + 主动告警」——运营在总览页一眼看到「IG 的 ig3 号 composer 失配」并被
toast 提醒，再走 D1 覆写层热修。桌面多平台内嵌的「接入→热更新→可观测→告警」全链路闭合。
**下一步**：① **D3** 每账号指纹注入（复用 `M4 fingerprint`，多号防关联封）；② 真号现场校准选择器并打开 `canIngest`；
③ 失配持续告警升级（接 alerts 通知）；④ **D4** 双向收件箱桥 + 受控 autopilot。
