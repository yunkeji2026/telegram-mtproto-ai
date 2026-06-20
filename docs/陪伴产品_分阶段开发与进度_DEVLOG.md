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
