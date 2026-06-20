# M 线 · 商业化开发文档（持久记忆）

> 本文件是 M 线（Monetization / 快速商业化）的**单一事实来源与工作记忆**。
> 重入/失忆时先读本文件「当前状态」与「下一步」两节，再继续。
> 每完成一个阶段：跑测试 → 把结果与进度回写「执行日志」→ 自动进入下一阶段。
> 项目铁律：**文档可能落后于代码，动手前先 grep/读码验证实况**（见 CLAUDE.md）。

## ⚠️ 0. 产品定位修正（2026-06-17，最高优先，先读这条）

**本产品的核心是「AI 情感陪伴聊天」，不是客服/订单/查单。**
个人号主动获客 → 建立长期情感关系 → AI 扮演有人设的角色陪聊 → 提升亲密度与长期留存。
运营形态 = **全自动**（AI 全程扮演角色，真人几乎不接管）。

由此推翻早期"客服模型"假设，关键纠偏：
- **北极星指标是「关系深度 + 留存」，不是「解决率/首响」**。客服要"快速结案"，陪聊要"聊得久、黏、用户愿意回来"——**用户回访=好事(留存)，不是坏事(复发工单)**。
- 已有成熟情感栈（以代码为准）：`persona_manager`（人设/多角色/禁客服腔）、`episodic_memory_store`+`portrait_extractor`（长期记忆/画像）、`intimacy_engine`+`companion_relationship`（亲密度+四阶段 initial→warming→intimate→steady，直接注入 prompt）、`emotional_context`+`reunion_prompts`+`reactivation_scheduler/loop`（情绪+久别重逢+沉默召回）。
- 早期 M6「AI 解决率」**方向错误已重构**为「关系健康·留存」（见 §7）。M7 反封号仍是命门（个人号规模化），保留。M8 转人工/M9 KB 在全自动陪聊里降为边缘。

## 0. 商业模式与定价（开发前提）

- **交付模式**：单租户自托管 + **agency 白标转售**（复刻 GoHighLevel 护城河；白标能力已具备）。
- **定价分层**（落在 `src/utils/billing.py::DEFAULT_PRICING`，授权 `src/licensing/gate.py` 控权）：
  - community（免费/社区）、basic($49)、pro($149)、flagship($499)——已存在 4 档，本线沿用，不再造新命名。
- **战略定位**：出海「个人号主动获客 → 转化 → AI 自动服务 → 运营闭环」自托管一体机。

## 1. 阶段总览

| 阶段 | 任务 | 状态 |
|---|---|---|
| **Phase 0 商业化打包** | M1 一键部署 / M2 授权签发CLI / M3 激活UX / M4 套餐落定 | ✅ **审计：已存在** |
| **Phase 1 首单闭环** | M5 首跑向导 / **M6 解决率度量** / **M7 反封号v1** / M8 结构化转人工 | ⏳ 进行中 |
| **Phase 2 留存复购** | M9 KB缺口飞轮 / M10 Copilot内联 / M11 白标转售 / M12 评测harness | ⬜ 待开始 |
| **Phase 3（选做）** | M13 Agent执行引擎 / M14 主动式触达 | ⬜ 待定 |

## 2. Phase 0 审计结论（代码实况，非计划）

逐项 grep/读码验证，Phase 0 四项**均已存在**，无需重复开发：

| 任务 | 证据（代码位置） | 结论 |
|---|---|---|
| M1 一键部署 | `Dockerfile`、`docker-compose.yaml`、`docker/docker-compose.ha.yml` | ✅ 已具备（HA 版亦有） |
| M2 授权签发 CLI | `scripts/license_tool.py`（genkeys + issue，Ed25519 离线签发） | ✅ 已具备 |
| M3 激活 UX | `src/web/routes/license_routes.py` + `licensing/license_manager.py`（active/grace/expired/invalid 状态机 + 只读中间件 `gate.py`） | ✅ 已具备 |
| M4 套餐落定 | `billing.py::DEFAULT_PRICING`（4 档 + 超额）、`compute_charges`、`gate.py`（席位/渠道/功能位门控） | ✅ 已具备 |

> 注：存在 `workspace_id`（默认 `default`）多租户预留列；当前按单租户运营，白标转售走「每客户独立实例」。

**Phase 0 结论**：商业化"卖得出去"的底座（部署/授权/计费/白标/演示/上线清单）已就位。真正缺口在 Phase 1 的价值证明（M6）与稳定性护城河（M7）。

## 3. Phase 1 设计（开发中）

### M6 AI 解决率度量（keystone，最高优先）
**目标**：拿到真实「AI 自主解决率 + 72h 再联系率」，进 ROI 看板/周报——售卖与续费的核心数字。
**数据源**：`draft_audit_log`（action: `autosend`=AI；`approved|edit_send|force_override`=人工；`rejected|blocked`=拦截；带 `conversation_id`、`ts`）。
**定义（v1，会话级，窗口 [since, until)）**：
- `ai_handled`：窗口内有 ≥1 `autosend` 且 **0 人工动作**的会话数。
- `human_handled`：有 ≥1 人工动作的会话数。
- `ai_resolution_rate` = ai_handled /(ai_handled + human_handled)：AI 全程独立处理（未转人工）的会话占比。
- `reopened`：ai_handled 会话在其"最后一次动作"后 `recontact_window`(默认 72h) 内又有新动作 → 视为未真正解决（再联系）。
- `recontact_rate` = reopened / ai_handled（行业识别"假解决"的关键指标，滞后量）。
- `ai_resolved` = ai_handled − reopened；`true_resolution_rate` = ai_resolved /(ai_handled + human_handled)。

**落点**：`store.get_resolution_stats(since, until)` → 接入 `unified_inbox_roi.build_roi_summary` 的 automation 段 + `ops_intel.build_ops_report` 周报 + ROI 看板展示。

### M7 反封号 v1（守命门）✅ 完成
**审计先行**：日配额（`AccountLimiter`）、proxy 绑定（`account.proxy_id`+`proxy_pool`）、设备 fail_rate 统计已存在。真正缺口 = **预热爬坡 + 账号健康红绿灯**。
**已交付**：
- `src/skills/account_health.py`（纯函数）：`warmup_cap`（新号每日上限从 start 在 ramp_days 内线性升到 target）、`account_health`（信号→0-100 分 + green/amber/red + 建议上限 + 原因）、`fleet_health`（机群汇总，取最差灯）。
- `AccountLimiter` 增**可选**预热爬坡：`warmup_enabled` + `age_days_fn` 回调；`effective_cap()` = min(daily_cap, warmup_cap(age))；新号超预热上限拒发并报 `warmup_cap_exceeded`；阈值告警与 `get_counts` 同步用 effective cap。**默认关 → 历史行为零变更**（既有 account_limiter/cap_alert 测试全过）。
- 健康信号：age_days / sends_today / flood_waits_24h / errors_24h / proxy_bound / banned。

### M8 结构化转人工 ✅ 完成
**审计**：escalations 仅携带 reason/agent/wait_sec，**无结构化上下文**；conversation_meta 已有 intent/emotion/risk/csat/summary。
**已交付**：
- `src/utils/handoff_brief.py::build_handoff_brief`（纯函数）：汇 conversation_meta + 最近往来 → 结构化简报（profile + recent_turns + highlights 风险提醒）。
- 端点 `GET /api/workspace/handoff-brief?conversation_id=&reason=`：坐席接手前一键拉取，3 秒进入状态；store/元数据缺失优雅降级。
- 已登记 route inventory 契约。

### M5 首跑向导 ✅ 审计：已存在
`setup.html`、`setup_wizard.html`、`golive_checklist.html`、`src/utils/channel_setup.py` 已具备首跑/上线引导。Phase 1 收口确认其覆盖即可，无需新建。

## 4. 当前状态（每次更新）

- **M 线**：✅ 全部完成（Phase 0-2 交付/审计，全量回归连跑 2 次全绿）。
- **🆕 N 线（A/B 双线收敛）已开线**：见 §8 能力盘点 + §9 收敛方案。
  - **方向已对齐（2026-06-17，用户拍板）**：协议栈优先 → Telegram 首发 → 云端多开 → 反封号平衡；**A 线（配置/session 登录）与 B 线（扫码登录）两条都要打通，不重复造轮子**。
  - **收敛策略**：抽 5 个共享内核（统一回复大脑 / 账号+代理 / 反封号闸门 / 登录注册表 / 运行时），两条线消费同一套（§9.1）。
  - **已完成**：N1+N2+N3 三个共享内核（统一回复大脑 / A 线加 proxy / 反封号闸门）+ N3 信号接线（统一 account_signals）+ **优化1 统一发送计数器（A/B 共用一个 AutoReplyLimiter day_used，A 线反封号满血）** + N6 机群健康灯端点 + **前端看板（rpa_overview 机群反封号卡）**。全量 4884 passed。**实况修正**：B 线本就有记忆+情绪+人设（skill_manager 内部注入），原"空壳"判断被夸大（见执行日志）。
  - **N4 骨架已完成**（2026-06-17）：A 线 `initialize()` 放宽（session_string/会话文件免 phone）+ `start(block=False)` 编排器托管 + `TelegramCompanionWorker` 用扫码 session 拉起 A 线丰富 client（flag `platform_login.telegram.companion_runtime`，默认关）。代码 + 13 例 mock 单测全绿（全量 4897 passed）。
  - **N4b 入站/出站收件箱镜像已完成**（2026-06-17）：A 线 `_emit_inbox` + `_mirror_inbox`，协议号收/发自动镜像进统一收件箱（坐席台可见），默认关、companion worker 自动开。全量 4901 passed。
  - **下一步**：N4 真号联调（扫码 session 实连）+ N5 登录归一（phone+QR 写同一 registry，需真号）。编排自愈/批量扫码 UX 可并行。**纯代码侧 N1-N4（含 N4b）骨架+优化1/3 已收口，N4 验证与 N5 需要真号联调。**
- **M 线**后续迭代候选见 §6。

### 最终回归（连跑 2 次）

| 轮次 | 结果 | 用时 |
|---|---|---|
| 全量 #1 | **4830 passed, 31 skipped, 0 failed** | 207s |
| 全量 #2 | **4830 passed, 31 skipped, 0 failed** | 196s |

命令：`python -m pytest tests/ -n auto -q --timeout=120 --timeout-method=thread`
（31 skip 为环境门控用例——缺外部依赖/设备时跳过，非失败。）

## 6. 本轮交付总结

- **新建高值能力**（代码中确实缺失，填补后均有测试）：
  - M6 AI 解决率度量（keystone 售卖数字）
  - M7 反封号 v1（账号预热爬坡 + 健康红绿灯）
  - M8 结构化转人工简报
  - M9+ KB 缺口统一优先级待办
- **审计确认已存在**（避免重复造轮子）：M1-M5（部署/授权/激活/套餐/首跑向导）、M9-M12（KB 飞轮/Copilot/白标/评测 harness）。
- **核心收获**：再次验证项目铁律——文档落后于代码。本轮 14 个计划项中 10 项已在代码中实现，真正缺口仅 4 项，集中在「价值证明（解决率）」与「稳定性护城河（反封号）」。商业化的瓶颈不在功能数量，而在这几个能直接影响成交与续费的关键指标/能力。

## 3b. Phase 2 审计结论（代码实况）

逐项 grep 验证，**M9-M12 均已存在**（再次印证「文档落后于代码」）：

| 任务 | 证据 | 结论 |
|---|---|---|
| M9 KB 缺口飞轮 | `kb_store.py`：`kb_miss_log`+`log_miss`+`get_miss_stats`+`get_weak_hits`+`get_overloaded_entries`+`get_auto_suggestions`；`kb_routes.py`：`/api/kb/auto-suggestions`、`/api/kb/miss-to-entry`（一键入库）、`/api/kb/reply-quality` | ✅ 已具备 |
| M10 Copilot 内联 | `copilot_polisher.py`、`copilot_stats.py`、`copilot_routes.py` | ✅ 已具备 |
| M11 白标转售 | `branding_routes.py`、`utils/branding.py` | ✅ 已具备 |
| M12 评测 harness | `src/eval/faq_eval.py`、`src/eval/intent_eval.py`、`scripts/run_eval.py`、`contacts/draft_eval.py`、`tests/test_eval_harness.py` | ✅ 已具备 |

**M9+ 增量增强（本次新增，非重复造轮子）**：`src/utils/kb_gap.py` 把分散的 miss/弱命中/过载建议折算成**统一数值优先级**（来源权重 × 对数频次）排成一条可执行待办，折进 `/api/kb/auto-suggestions` 的 `backlog`/`backlog_summary` 字段——解决「一堆建议不知先做哪条」。

## 5. 执行日志（每阶段追加，勿删历史）

- 2026-06-17：完成 Phase 0 审计（四项均已存在，证据见 §2）；建立本文档作为持久记忆；进入 Phase 1，从 M6 开工。
- 2026-06-17 · **M6 AI 解决率度量 ✅ 完成**：
  - `src/inbox/store.py::get_resolution_stats`：按会话聚合 draft_audit_log，产出 ai_handled / human_handled / reopened / ai_resolved 及 4 个比率。再联系判定用「会话内静默间隔」窗口内自包含算法（不依赖 until_ts），since-only 即有效。
  - `unified_inbox_roi.build_roi_summary` 增 `resolution` 段 → `/api/workspace/roi` 自动带出。
  - `ops_intel.build_ops_report` 增 `resolution` 段 + headline（解决率进运营周报）。
  - `workspace_roi.html` 增「AI 解决率 / 再联系率」KPI 卡（头号售卖数字）。
  - 测试 `tests/test_resolution_stats.py`（6 例）：AI/人工分类、再联系判定、超窗新问题、窗口过滤、空库、ROI 接线。
  - **测试结果**：`test_resolution_stats + test_ops_intel + test_ops_incidents + test_unified_inbox_stage1` 共 **158 passed**，0 fail；改动文件无 lint。
  - 优化思考：再联系判定最初设计为「last_ts 后查 until 边界外」，发现 since-only 场景恒为 0 → 改为「会话内相邻动作间隔落在 (session_gap, recontact_window] 」的自包含算法，更稳健且无需第二次 SQL。
  - 下一步可优化：①真实「再联系」应结合 messages 入站（当前用 audit 动作近似，足够 v1）；②解决率环比（compare）；③按 workspace_id 维度拆分。
- 2026-06-17 · **M7 反封号 v1 ✅ 完成**：
  - 审计：日配额/proxy 绑定/设备 fail_rate 已存在；补齐缺口「预热爬坡 + 健康红绿灯」。
  - `src/skills/account_health.py`：warmup_cap / account_health / fleet_health（纯函数）。
  - `AccountLimiter` 可选预热爬坡（effective_cap，默认关零破坏）。
  - 测试 `tests/test_account_health.py`（16 例）+ 回归 `test_account_limiter`/`test_cap_alert` 共 **37 passed**；无 lint。
  - 优化思考：预热设计为 limiter 的 opt-in 旁路（age_days_fn 回调）而非硬编码，既零破坏又可让上层用任意「账号天龄」来源（contacts 表/registry）。健康评分用「最差账号灯=机群灯」的保守聚合，避免一个红号被均值掩盖。
  - 下一步可优化：①把 fleet_health 接进 /api/rpa-overview 或 ops health 看板（需集中账号 age/flood 信号源）；②FLOOD_WAIT 计数实时采集；③随机延迟 pacing 纳入评分。
- 2026-06-17 · **M8 结构化转人工 ✅ 完成**：
  - `src/utils/handoff_brief.py::build_handoff_brief`（纯函数）：profile + recent_turns + highlights。
  - 端点 `GET /api/workspace/handoff-brief`（已登记 route inventory）。
  - 测试 `tests/test_handoff_brief.py`（6 例）+ route inventory 共 **10 passed**；无 lint。
  - 优化思考：简报做成纯函数 + 瘦端点，store 取数失败逐项 try/except 优雅降级（永不因缺元数据阻断接手）。highlights 用关键词集合判定负面情绪/高风险，便于后续扩词。
- 2026-06-17 · **M5 首跑向导 ✅ 审计已存在**：setup_wizard.html / golive_checklist.html / channel_setup.py 已覆盖，无需新建。
- 2026-06-17 · **Phase 1 收口**：M6/M7/M8 交付 + M5 审计；进入 Phase 2。
- 2026-06-17 · **Phase 2 审计 + M9+ 增强 ✅**：
  - 审计：M9 KB 飞轮 / M10 Copilot / M11 白标 / M12 评测 harness **均已存在**（证据见 §3b）。
  - M9+ `src/utils/kb_gap.py`：rank_kb_gaps / gap_priority_score / gap_backlog_summary（纯函数），折进 `/api/kb/auto-suggestions`。
  - 测试 `tests/test_kb_gap.py`（7 例）+ KB 全量 **130 passed**；无 lint。
  - 优化思考：不重写已有飞轮，只补「统一优先级」这块缺失的连接组织；频次用对数压缩避免长尾被碾压；折进既有端点而非新增路由（零契约 churn）。
- 2026-06-17 · **全量回归收尾 ✅**：连跑全量 2 次，均 **4830 passed / 31 skipped / 0 failed**（见 §4 表）。本轮 M 线开发全部完成。
- 2026-06-17 · **🔄 产品方向修正 + M6 重构（见 §0、§7）**：明确产品=情感陪聊+全自动运营；M6「AI 解决率」方向错误（解决率/再联系率在陪聊里指标相反），**重构为关系健康·留存**。
- 2026-06-17 · **🆕 N 线开线（情感陪聊核心，见 §8、§9）**：
  - 两个只读探查代理逐文件盘点四平台「登录×全自动陪聊」实况（报告 8cf4f1f8=Telegram、ed9f9f19=RPA 三平台）。核心发现：系统存在**协议栈 vs RPA 两条互不打通的路线**；「扫码即用」只属协议栈（TG/WA-Baileys，雏形待联调），全自动陪聊最成熟的是 RPA（Messenger★★★★）但不扫码、需真机+人工登号。
  - **回答用户问题**：「扫码登录个人号全自动聊天」目前**不能开箱即用**——TG 协议线骨架已搭但 3 道闸门默认全关 + 未真号联调 + 协议线陪聊上下文贫瘠（见 §8.3 命门清单）。
  - **用户拍板方向**：协议栈优先 / Telegram 首发 / 云端多开 / 反封号平衡加固。
  - 出 N 线方案（N1 打通命门 → N2 反封号 → N3 多开运维 → N4 陪聊深化 → N5 WA Baileys → N6 RPA 补充）。下一步从 **N1** 开工。
- 2026-06-17 · **🔁 N 线改为 A/B 双线收敛（用户要求"两条都打通、不重复造轮子"）**：
  - 读码定位重复根因（§9.0）：A 线 `TelegramClient` 与 B 线 `TelegramProtocolWorker` 各维护一套 client/入站/回复/注册表。A 线有记忆人设无代理、B 线有代理无灵魂。
  - 重写 §9 为「五个共享内核」收敛方案：统一回复大脑(核心1) / 统一账号+代理(核心2，复用 B 线 `_to_pyrogram_proxy`) / 统一反封号闸门(核心3，接 M7) / 统一登录注册表(核心4) / 统一运行时(核心5，扫码 session 跑 A 线 client)。
  - 阶段重排为 N1-N8；N1+N2+N3 不依赖真号、纯代码可单测，搭好后 A/B 两线**自动同时**获得完整陪聊+代理+反封号。下一步从 **N1 核心1** 开工。
- 2026-06-17 · **N1+N2+N3 三个共享内核 ✅ 完成（A/B 双线收敛落地）**：
  - **🔬 关键实况修正（代码 > 文档/分析）**：追 `skill_manager.process_message`（L688 白名单合并、L718 episodic 记忆注入、L729 emotional_context 情绪注入）发现——记忆/情绪/人设由 **skill_manager 内部按 platform+user_id+chat_id 自动注入**，而 B 线**早已传齐这三者**。故先前探查报告称 B 线"空壳/没灵魂"**被夸大**：B 线实际已有记忆+情绪+人设。真正缺口收窄为 chat_type 路由一致性 + 情绪 hint + 防漂移，以及 A 线无代理(N2)、反封号闸门未接(N3)。
  - **N1 核心1**：新建 `src/utils/companion_context.py`（`route_persona_id`/`emotion_hint`/`build_companion_context`，纯函数）。A 线 `_process_message_async` 的人设三级路由 + 情绪 hint **等价重构**为调共享件；B 线 `protocol_autoreply.build_reply_hook` 从 4 字段 → 调 `build_companion_context`（补 chat_type/is_group/情绪 hint，私聊默认）。改一处两线生效、防未来漂移。测试 `tests/test_companion_context.py`（18 例，含与 A 线原三元逻辑逐 case 比对）。
  - **N2 核心2**：A 线 `TelegramClient` 加 `proxy=`，复用 B 线现成 `proxy_pool` + `_to_pyrogram_proxy`（`_resolve_proxy()`，proxy_id 空/失败→直连旧行为）；`telegram_account_registry` 的 `TelegramAccountContext`/`account_cfg`/`from_config`/`stats` 全程贯通 `proxy_id`。A 线补上反封号命门、零新代理轮子。测试 `tests/test_telegram_account_proxy.py`（8 例）。
  - **N3 核心3**：新建 `src/skills/companion_send_gate.py`（`gate_decision`/`evaluate`/`aggregate_fleet`，**编排** M7 `account_health`/`fleet_health`，不重造评分/爬坡）。A 线 `_send_reply` 与 B 线 `_send` 前置同一道闸门，`companion_send_gate.enabled` **默认关→零破坏**；开启则超预热上限/红灯/封禁 → 拒发或转人工。测试 `tests/test_companion_send_gate.py`（11 例）。
  - **测试**：新增 37 例全绿；触达子系统回归 591 passed；**全量 4867 passed / 31 skipped / 0 failed**（207s 基线 → +37 例）。无 lint。
  - **测试踩坑**：proxy 单测在批量运行中因 pyrogram import 期 `asyncio.get_event_loop()` 在裸 MainThread 抛错（pytest-asyncio 拆 loop 后）→ 导入前加 `_ensure_event_loop()` 守卫（仅测试需要）。
- 2026-06-17 · **N3 信号接线（优化1）+ N6 机群运维（部分）✅**：
  - **统一信号源**：新建 `src/skills/account_signals.py`（`build_account_signals`/`lifecycle_stage`/`fleet_overview`，纯函数 + 依赖注入）。信号统一来自 `account_registry`（created_at→age_days / proxy_id / status / meta.banned）+ `AutoReplyLimiter.snapshot`（day_used→sends_today / circuit_open）。A/B 两线发送闸门 + ops 看板**共用同一份事实**。
  - **N3 满血**：B 线 `_send` 的闸门信号从"仅 proxy/banned"升级为 `build_account_signals`（真 `sends_today` + `age_days`），预热爬坡上限现在真正生效（开闸时）。
  - **N6 机群运维（部分）**：新增 `GET /api/accounts/fleet-health` → 机群健康灯（M7 `fleet_health`）+ 生命周期分布（pending/warming/active/restricted/banned/offline）。已登记 route inventory。编排自愈/批量扫码 UX 留待真号/前端。
  - **测试**：`tests/test_account_signals.py`（15 例）+ `test_accounts_aggregator.py` fleet-health 端点（1 例）；**全量 4882 passed / 31 skipped / 0 failed**（232s）。无 lint。
  - 优化思考：把信号装配抽成独立 provider 而非塞进闸门/端点，是因为"发送前按需取单号信号"与"看板批量取全机群信号"是同一份数据的两种消费——一个源喂两处，避免 N3/N6 各写一套读 registry+limiter 的逻辑。
- 2026-06-17 · **优化1 闭环（统一发送计数器）+ 优化3（fleet-health 前端看板）✅**：
  - **统一发送计数器（A/B 真收敛）**：之前 A 线闸门读的是 `self._sends_today`（自有、与 B 线各算各的），B 线读 `AutoReplyLimiter`——两线计数割裂，A 线反封号"半血"。本次让 A 线 `src/client/sender.py::_send_reply`：① 闸门信号改用 `build_account_signals(..., limiter=_shared_send_limiter())`；② 发送成功后 `limiter.record_sent("telegram:<account_id>")` 记入**与 B 线同一份** `AutoReplyLimiter` day_used。新增 `_shared_send_limiter()` 取 B 线 `get_autoreply_limiter` 单例。**一个计数器喂两线，A 线预热爬坡上限真正生效**。
  - **fleet-health 前端看板**：`rpa_overview.html` 聚合 KPI 条下新增"机群反封号健康"卡（机群健康灯 🟢/🟡/🔴 + 活跃/预热中/受限封禁 三态计数），`ovRefresh` 每轮调 `ovRefreshFleet()` 拉 `/api/accounts/fleet-health`（独立 try，失败静默不拖累主刷新）。运维侧首次可视化机群反封号态势。
  - **测试**：新增 `tests/test_account_signals.py` 2 例（统一计数器喂信号+闸门拦截、`_shared_send_limiter` 取到同一单例）；**全量 4884 passed / 31 skipped / 0 failed**（218s）。无 lint。
  - 优化思考：**不另造发送计数器**，直接复用 B 线既有 `AutoReplyLimiter`——反封号关心的是"该号今日总外发"，与哪条线发出无关；同号同时只在一条线跑，复用不会串账。这正是"不要重复造轮子"的落地：A 线没新表/新计数器，只接进既有单例。
- 2026-06-17 · **N4 统一运行时骨架（协议号跑 A 线"有灵魂"client）✅ 骨架**：
  - **收敛命门确认**：B 线扫码登录（`telegram_protocol_login`）把 session 落到 `sessions/<session_name>.session` 并登记 `account_registry(mode=protocol, meta.session_name)`；A 线 `TelegramClient` 本就用 `session_name`+`workdir="sessions"` 同一目录。**唯二缺口**：① A 线 `initialize()` 硬要 `phone_number`；② `start()` 末尾 `await idle()` 永不返回，无法被编排器托管。
  - **A 线改造（最小侵入、全加法）**：① `initialize()` 凭据校验放宽——`api_id/api_hash` 必需，`phone` 仅在"无既有 session"时必需；新增 `session_string` overlay（in-memory 已授权 session，云端拉起常用），有 `session_string`/会话文件即免 phone。② `start(block=True)` 新增 `block` 参数；`block=False` 时连接+装处理器+起消息任务后**即返回**（编排器监督循环保活），默认 True 不改 main.py 行为。
  - **B 线 worker 包装层**：新建 `src/integrations/telegram_companion_worker.py::TelegramCompanionWorker`——用 `meta.session_name` 拉起 **A 线 TelegramClient**（记忆/人设/情绪/语音图片/四层触发/人工转接全在线），实现编排器 worker 协议（start/stop/healthy/send/status）。app 启动经 `set_companion_context(config_manager, skill_manager, ai_client)` 注入进程级运行时上下文（编排器只有 config dict，构不出 A 线 client，故用上下文 seam 解耦）。
  - **flag 选择 + 零破坏**：`platform_login.telegram.companion_runtime: true` → 协议号走 A 线丰富 worker；默认/关 → 仍用既有 `TelegramProtocolWorker`（B 线薄连接）。`ensure_builtin_workers` 据此二选一注册。pyrogram/TelegramClient 全惰性导入。
  - **测试**：新建 `tests/test_telegram_companion_worker.py`（13 例，全 mock 无真号）：flag/上下文、account_cfg 组装、worker start 需上下文/需 session/构建 A 线 client/init 失败回滚、send/stop/healthy/status、A 线 `initialize` session-string 免 phone、`start(block=False)` 不进 idle、`ensure_builtin_workers` 按 flag 选 worker。**全量 4897 passed / 31 skipped / 0 failed**（213s）。无 lint。
  - 优化思考：**不复制 A 线那套丰富管线到 B 线**（那才是重复造轮子），而是反过来——让 B 线 session 去"喂"A 线 client。代价最小（A 线只加 3 处加法），且自动获得 A 线全部能力 + 后续 A 线增强自动惠及协议号。**真号待办**：① 扫码 session 实连验证（DC 迁移后 session 能否被 A 线直接 start）。
- 2026-06-17 · **N4b 入站/出站收件箱镜像（协议号"有灵魂"且坐席可见）✅**：
  - **痛点**：N4 让协议号跑 A 线 client，但 A 线走自己的 `_setup_handlers` 收消息、自己回复，**不经 `protocol_bridge`**——于是统一收件箱/坐席台**看不到**这些会话。这会让"扫码号全自动陪聊"和"既有 Telegram 坐席台"割裂。
  - **方案（最小侵入、默认关）**：A 线新增 `_emit_inbox(...)` 镜像方法 + `_mirror_inbox` 开关（默认 False → standalone main.py 零影响）。① 入站：`_process_message_async` 提取消息后镜像 `direction="in"`（用户原话）；② 出站：`sender._send_reply` 发送成功后镜像 `direction="out"`（AI 回复内容）。companion worker 的 `account_cfg` 默认带 `mirror_inbox=True`。
  - **防重复**：只镜像"A 线自有的两条路径"（入站 handler + AI 自动回复）；坐席**手动**发送走 `orchestrator.send`，那条路径**已**自带 `emit_incoming(out)`，故不在 `client.send_message` 里再镜像，避免双发。镜像只 `emit_incoming`（入收件箱 sink），**不触发** B 线 autoreply，杜绝与 A 线自身回复打架。
  - **测试**：`tests/test_telegram_companion_worker.py` +4 例（默认关不镜像 / 开启镜像 in / 镜像 out / 失败静默），并在 account_cfg & worker build 用例加 `mirror_inbox` 断言。**全量 4901 passed / 31 skipped / 0 failed**（219s）。无 lint。
  - 优化思考：镜像走既有 `protocol_bridge.emit_incoming`（坐席台/收件箱本就消费这个 sink），**不新造一条镜像通道**——又一次"复用既有 sink 而非造轮子"。至此协议号做到"全自动陪聊（A 线灵魂）+ 坐席台全程可见可接管"两全。**真号待办**：扫码 session 实连后，验证镜像消息在收件箱线程的 chat_key 归并是否与坐席台预期一致。
- 2026-06-17 · **配置可达性 + 误配护栏 + 真号联调清单（真号前最后一公里）✅**：
  - **痛点**：N1–N4 的开关（`platform_login.*` / `companion_send_gate.*`）**全程没进 `config.example.yaml`**——运维照抄样例根本不知道这些键存在，等于功能"做了但开不出来"。
  - **配置文档**：在 `config.example.yaml` 补两段带注释的块：`platform_login`（orchestrator_enabled / telegram.protocol_enabled / companion_runtime / backfill_dialogs，逐项标注依赖关系）+ `companion_send_gate`（enabled / target_cap / warmup_start_cap / warmup_ramp_days）。样例全 `false`，`check_config` 验证零 error。
  - **误配护栏**：`config_check.py` 加两条规则——`_check_protocol_login`（`companion_runtime` 开但漏 `protocol_enabled`/`orchestrator_enabled` → WARN 点名；开协议登录但无 api 凭证 → WARN）+ `_check_companion_send_gate`（`warmup_start_cap > target_cap` / ramp 非正整数 → WARN）。这类"开关依赖 footgun"以前只能线上踩坑，现在启动自检即拦。
  - **真号联调清单**：新增 `docs/N_LINE_REAL_ACCOUNT_CHECKLIST.md`——准备物（api_id/号/代理/2FA）+ 4 步验证（扫码落盘→编排拉起→收发镜像→反封号灯）+ 每步预期/排错 + 真号触发的优化候选（session_string 优先 / N5 登录归一 / chat_key 归并）+ 一键回退。
  - **测试**：`tests/test_config_check.py` +7 例（协议登录/编排/runtime 依赖一致性 + 闸门阈值合法性）。**全量 4908 passed / 31 skipped / 0 failed**（230s）。无 lint。
  - 优化思考：真号到来前，纯代码能做的最高价值不是再堆新功能（会变"空中楼阁"），而是**让已做的能被正确开启、误配能被自动拦、联调有据可依**——把"做完"真正变成"能用"。N5/真号验证已无纯代码空间，等测试号。
- 2026-06-17 · **Q1 人设一致性守卫（陪聊沉浸感保护）✅**：A/B 收敛纯代码收口后，转向产品内核——陪聊质量。
  - **选题依据**：对陪聊四支柱（人设/记忆/亲密度/情绪）做了全管线测绘（见会话）。最高 ROI + 全可测 + 平台无关的缺口是「**人设禁用语只在 prompt 里嘱咐、产出端零强制**」：LLM 不总遵守，一旦回复漏出客服腔（"有什么可以帮您的"）或自曝 AI（"作为一个人工智能"），陪聊"真人感"瞬间崩塌——这是本产品最致命的体验事故。
  - **方案**：新建 `src/utils/persona_guard.py`（纯函数）——`find_violations`（命中 `speaking.forbidden_phrases` + `deny_ai` 时命中"自曝 AI 身份"保守模式，"我不是 AI"等否定句不误伤）+ `sanitize`（**按句剥离**违规句、保留其余；剥光则 inline 抹短语；仍空回退原文，**绝不吞回复**）。`SkillManager` 在反repeat 之后、记忆写入之前接 `_enforce_persona_consistency`（经 `PersonaManager.get_persona` 取生效人设），守卫异常一律回退原文。
  - **零影响默认**：`companion.persona_guard.enabled` 默认 true，但**仅当人设声明了 forbidden_phrases/deny_ai 才有实际动作**——对无禁用项的默认人设是 no-op；默认陪聊人设（conversion）本就声明了，于是自动获得保护。
  - **测试**：`tests/test_persona_guard.py`（17 例）：collect/find（短语命中、空格不敏感、deny_ai 自曝命中、否定不误伤、deny_ai 关时不查）+ sanitize（剥句留余、客服腔整句删、无配置不动、整段违规不空、合规不动、空串）+ SkillManager 接线（剥离/关闭直通/异常回退）。**全量 4925 passed / 31 skipped / 0 failed**（216s）。无 lint。
  - 优化思考：**确定性后置剥离 > 重新生成**作为第一道防线——零延迟、确定、可测，且"剥一句不在线"远胜"漏一句出戏"。重生成（更高质量改写）留作上层可选第二道防线（需 LLM、属真号/在线调优范畴）。下一步可做的同类纯代码增强：① `direct_chat` 意图缺专用 skill（落到 small_talk）；② 三套"warmth"信号（exchange_count / reply_count / intimacy_score）口径不一致的归一。

- 2026-06-17 · **Q2 关系热度信号归一（同一 prompt 内不再自相矛盾）✅**：陪聊"亲密度关系"支柱的地基。
  - **问题**：同一条 AI 上下文里并存两块关系深度提示且**各算各的**——`build_relationship_prompt_block`（companion，按 `exchange_count` + intimacy 融合 → 初识/升温/暧昧/稳定）与 `build_emotional_context_block` 的「关系温度」（按 `reply_count` 启发式 → stranger/acquaintance/familiar/close）。两者计数口径、量纲、阈值全不同，极端时 prompt 同时出现「关系阶段·稳定陪伴」与「关系温度·stranger」，给 LLM 自相矛盾的语气指令，关系逻辑不可信。
  - **方案**：以 **companion 关系阶段为权威信号源**，让「关系温度」跟随它。`emotional_context` 抽出共享文案字典 `_WARMTH_GUIDANCE`（`compute_warmth_level` 与新路径共用，一份文案）+ `_STAGE_TO_WARMTH`（initial→stranger / warming→acquaintance / intimate→familiar / steady→close，1:1）；新增纯函数 `warmth_from_stage()` 与 `lookup_companion_stage()`（按 chat_key 读持久化的 `companion_relationship[chat_key].stage`，单聊回退单条）。`build_emotional_context_block(..., chat_id=)` 优先用阶段映射温度，**无阶段（非 conversion / 首轮）回退原 reply_count 启发式**。`SkillManager` 仅多传 `chat_id`，无重排。
  - **向后兼容**：新增 `chat_id` 为关键字可选参；不传或 `user_context` 无 `companion_relationship` 时行为完全等同旧版。`compute_warmth_level` 数学不变（启发式最低档实测恒为 acquaintance，stranger 经此路径不可达——既有量级未动）。
  - **测试**：`tests/test_emotional_warmth_unify.py`（12 例）：四阶段映射 / 未知→None / 两路径同档文案字面一致 / chat_key 命中与单聊回退 / 多会话不误命中 / 集成（steady→close 且不出现 stranger、无阶段回退 acquaintance、intimate→familiar 无矛盾）。**全量 4937 passed / 31 skipped / 0 failed**（236s）。无 lint。
  - 优化思考：本可直接"重排"让融合后的 effective_stage（含 reunion 降阶）喂温度块，但重排大 try-块风险高、收益边际；改用**读持久化阶段（上一轮已落库的当前关系深度）**——零重排、跨轮自洽，且温度不会在阶段块确认前中途跳档，更稳。reunion（长沉默降阶）由 emotional 块自带的 `classify_time_gap` 时间感知兜底，方向一致不冲突。下一步候选：③ Q3 让主平台 Telegram 关系深化注入统一事实源（把 IntimacyEngine 的 score 在 runner 侧稳定喂入，A 线 sender 路径也享受融合）；④ `direct_chat` 专用 skill。

- 2026-06-18 · **Q3 主平台 Telegram 接通统一关系事实源（A 线吃上 IntimacyEngine 融合）✅**：让主平台追上 RPA 各线。
  - **问题**：`companion_relationship.fuse_with_intimacy` 的「轮次×衰减」双信号融合（沉默自动降阶 + reunion 问候）需要 `intimacy_score`。LINE/Messenger 各 runner 都在入站后 `get_journey_intimacy` 透传，但**主平台 Telegram(A 线 `telegram_client`) 从不传** → A 线融合恒跳过，长沉默回归仍直接接旧梗（最伤陪聊真实感的场景之一）。根因有二：① A 线 client 构造早于 contacts bootstrap，无 hooks 句柄；② **Telegram 从未把收发记入 contacts** → 即便查也无 journey/score。
  - **方案（惰性 provider + 显式记录，复用 RPA 同一套 hooks，不重复造轮子）**：
    - `companion_context`（N 线共享大脑）新增**进程级关系事实源** provider：`set_relationship_providers(intimacy_lookup/funnel_lookup/message_recorder)` + `resolve_intimacy_score/resolve_funnel_stage`（只读）+ `record_relationship_message`（写）。惰性注册化解构造顺序问题；签名与 `rpa_hooks.get_journey_intimacy/_funnel_stage/on_message` 对齐，直接复用 contacts hooks。
    - `telegram_client._process_message_async`：入站先 `record_relationship_message(..., "in")`（刷新 journey intimacy）再 `resolve_*` 读"含本轮"的最新分，注入 `intimacy_score/funnel_stage` 到 skill context（与 RPA 同序）。`sender._send_reply`：出站 `record_relationship_message(..., "out")` 补齐 IntimacyEngine 的收发互动（mutuality），分数不再因只见入站而偏低。
    - `main.py`：contacts bootstrap 后注册 provider。**只读查询**在 `rpa_hooks.telegram` 开时即注册（无数据时 `resolve_*` 返 None，安全）；**写入 contacts** 仅在显式 `platform_login.telegram.contacts_recording: true` 时注册 recorder。
  - **零影响默认**：`contacts_recording` 默认 false（遵循"新子系统默认关"）→ 无 recorder → 无 Telegram journey → `resolve_*` 恒 None → A 线行为完全等同旧版。开关一开即点亮整条闭环（记录→intimacy 刷新→融合）。开前需 `contacts.enabled=true` 且接受 Telegram 联系人纳入 contacts 下游（reactivation/handoff）纳管。
  - **测试**：`tests/test_companion_context.py` +12 例（resolve 无 provider/传参/None 值/空 chat_key 短路/吞异常/funnel 规范化空→None；record no-op/传参与 120 截断/空 chat_key 短路/吞异常；partial 覆盖不互清）。**全量 4947 passed / 31 skipped / 0 failed**（234s）。无 lint。
  - 优化思考：① 选**惰性进程级 provider** 而非"给 client 加 setter / 改造构造顺序"——彻底解耦构造期与 contacts bootstrap，且 A 线直连 client 与 N4 companion-worker client **同享一个 provider，零额外接线**；② 把记录从只做入站扩到**收发都记**，否则 IntimacyEngine 的 mutuality 只见单边、分数系统性偏低；③ 记录开关独立于只读开关，做到"读安全永远在、写谨慎需显式"。下一步候选：④ `direct_chat` 专用 skill（现落 small_talk，陪聊主场景却无专用技能）；⑤ 把 A 线入站也接 `RelationshipStager`（funnel_stage 已透传，跨域语气校准可顺带打通）；⑥ 真号验证 Q3 闭环（开 `contacts_recording` 后看 journey/intimacy 是否随对话上升、reunion 是否触发）。

## 9. R 线·研究驱动的陪聊质量（对标 2026 前沿）

> 调研了 2026 情感陪聊/情感支持 AI 前沿（学术 + 开源），按本仓库铁律（API provider 不微调 / 纯函数优先 / feature flag / 可单测 / 无需真号）筛出可落地项。三大杠杆：**记忆架构**（REMT 情绪显著性×衰减重排、PersonaTree/nskit 分层巩固、Zep+Graphiti 双时态图）、**共情/主动策略**（STRIDE-ED 策略先行、ProESC look-forward、COCOON 主动倾听）、**人设一致**（已有 Q1 persona_guard；可选 role-aware reflection 二道防线）。底线：Engagement-Optimized Care 警示反谄媚/防依赖。

- 2026-06-18 · **R1 共情策略选择器（蒸馏版 STRIDE-ED / 主动倾听）✅**：在「情绪理解→回复生成」之间插显式策略层。
  - **选题依据**：研究共识——让 LLM 直接出稿不如**先选共情策略再生成**（策略是情绪理解与回复之间的关键中间层，STRIDE-ED/ProESC/PACEP 一致）。我们此前有情绪分析/弧线/温度/natural_dialogue，但**无显式"这一轮该怎么接"的行动策略**。这是对"聊得好"最直接、纯函数可测、无需真号的提升。
  - **方案**：新建 `src/utils/empathy_strategy.py`（纯函数）——`select_strategy()` 按 `dimension×intensity×arousal×arc` 确定性选标签（validate 确认安抚 / explore_needs 主动倾听探询 / accompany 低能量陪伴 / savor 共享放大 / curiosity 顺势好奇 / active_listen 承接式倾听）；`strategy_directive()` 取一行行动指令并按**关系阶段做克制修饰**（关系尚新时对深挖/高亲密型策略追加"别过度深挖/过度亲密"）；`build_strategy_block()` 装配「应对策略」块。`emotional_context.build_emotional_context_block(enable_strategy=True)` 复用已算的情绪/弧线/阶段（Q2 的 `_stage`）注入；`SkillManager` 经 `companion.empathy_strategy.enabled`（默认 true）控制。
  - **零风险默认**：纯 prompt 提示、不改任何发送/状态逻辑；选择确定性、异常吞掉返回 ""；关掉开关即完全不注入。与 arc_hint/natural_dialogue 取向一致（互相强化而非冲突）。
  - **测试**：`tests/test_empathy_strategy.py`（19 例）：六维度映射 + 负面高强度/高激活/恶化→validate、轻负面→explore + 坏数值不抛 + 阶段克制（新关系深挖加修饰、steady 不加、浅策略不加）+ block 装配/坏输入空串 + 集成（默认注入/可关闭/阶段克制联动/positive-savor steady 无修饰）。**全量 4966 passed / 31 skipped / 0 failed**（223s）。无 lint。
  - 优化思考：① 策略层与温度块**共用同一关系阶段**（Q2 的 `_stage`），不新增第二套阶段口径——延续 Q2"信号归一"原则；② 选**确定性规则**而非 LLM 策略规划器（ProESC 用小模型规划）作为第一版——零延迟/零成本/可测，LLM 级 look-forward 规划留作上层可选增强；③ 发现 arc_hint(情感感知) 与 validate 策略文案有轻微重叠（一个是"情绪读"、一个是"行动"，互相强化可接受），把"合并/去重情感类块"列为后续清理项。下一步候选（R 线）：**R2 记忆检索情绪显著性+时间衰减重排（REMT-lite）**——已有情绪/embedding/created_at，只改排序公式，对"记得准"提升大；R3 记忆离线巩固/分层（扩 PortraitExtractor，PersonaTree/nskit-lite）；R4 wellbeing/反谄媚守卫（延续 persona_guard 家族，危机识别+反谄媚）。

- 2026-06-18 · **R2 记忆检索情绪显著性 + 时间衰减重排（REMT-lite）✅**：让"记得准"。
  - **选题依据**：2026 REMT 指出记忆检索不该只看语义相似度，应让**情绪显著性 + 时间衰减**共同参与排序（情绪浓的、近期的记忆更该被想起）。我们 episodic 检索此前是"向量×关键词融合"或纯近期，**情绪浓度与新鲜度未进排序**——会想起一堆中性琐事却漏掉"上次 TA 失恋很难过"。
  - **方案**：新建 `src/utils/memory_salience.py`（纯函数）——`salience_score(text)`（用 `analyze_emotion` 把一条事实折成 0-1：0.5|valence|+0.3arousal+0.2intensity）+ `recency_factor(created_at)`（指数半衰，默认 30 天）+ `blend_rank(base, salience, recency)`（在既有相关度 base 上**温和叠加**，默认权重 salience 0.15 / recency 0.10，确保强相关不被盖过）。`episodic_memory_store.get_bullets_for_prompt` 新增 `use_salience_rerank`（默认 **False**）+ 三个权重参数，SELECT 增取 `created_at`；开启后在向量/关键词/纯近期三条路径上统一叠加重排。`SkillManager._inject_episodic_into_context` 经 `memory.salience_rerank.*` 配置透传。
  - **零行为变化默认**：`use_salience_rerank` 默认关 → 非重排路径**字节级不变**（既有 `test_episodic_memory` 全过）；关键词 base 用归一值但与原始分单调一致，排序不变。
  - **测试**：`tests/test_memory_salience.py`（11 例）：salience 中性<情绪浓/空串 0、recency 越新越高/缺失中性 0.5/未来钳 1、blend 加权正确且 **base 占主导**（强相关中性 > 弱相关情绪浓）；store 集成：默认近期序不变、开重排后情绪浓上浮、关键词路径强相关仍被选、空用户空串。**全量 4977 passed / 31 skipped / 0 failed**（220s）。无 lint。
  - 优化思考：① 把重排做成**既有相关度之上的温和叠加层**而非替换——`base` 占主导，salience/recency 只做"同分区分/边际上浮"，避免情绪浓的无关琐事盖过强相关记忆（用 `test_blend_base_dominates` 锁死该性质）；② 默认关 + 既有路径零改动，**风险被隔离在开关后**，可线上灰度 A/B；③ 第一版 salience 在**检索期**用 lexicon 现算（零迁移），下一步可在 `add_fact` 写入期落一列 `salience`（PersonaTree/REMT 的"写入即定权"），省去每次重算并支持更强的离线巩固。下一步候选：**R3 记忆离线巩固/分层**（扩 `PortraitExtractor`：去重 + 把复发事实晋升"稳定人设层"，并落 salience 列）；R4 wellbeing/反谄媚守卫；以及把 R1 的 arc_hint 与策略块去重合并。

- 2026-06-18 · **R3 记忆离线巩固 / 分层（PersonaTree/REMT-lite 思想）✅**：让"记得久、记得稳"。
  - **选题依据**：R2 让"记得准"（检索期重排），但记忆库本身仍是**一堆扁平 raw 事实**——反复提起的核心人设（"养了只叫团子的猫""最近失恋"）与一次性琐事**同权**，且 prune 按近期裁剪时核心事实会被近期刷量挤掉。PersonaTree / REMT 的核心思想是"原始证据 → 复发模式 → 稳定结论"分层；本步做其**轻量落地**：写入即定权 + 复发计数 + raw→stable 晋升 + stable 永不裁剪。
  - **方案**：
    - **schema 升列**（`_ensure_consolidation_columns`，沿用 `_ensure_embedding_column` 的 ALTER 模式，旧库平滑升级、`last_seen` 回填 `created_at`）：`salience REAL` / `tier TEXT='raw'` / `hits INTEGER=1` / `last_seen REAL`。
    - **写入即定权**：`add_fact` 落库时算一次 `salience`（`memory_salience.salience_score`，承接 R2 优化点③，检索期不必再算）；**重复事实**（同 hash）不再静默丢弃，而是 `hits+=1, last_seen=now`——把"反复提起"沉淀为复发信号（仍返回 None，兼容 `test_add_dedupe` 语义）。
    - **离线巩固** `consolidate(user_id, min_hits=2, min_salience=None)`：把 raw 层里**复发**（hits≥min_hits）或**情绪浓**（salience≥min_salience，若给）的事实晋升 `stable`；幂等（已 stable 不重复晋升）。
    - **prune 保护**：`prune_oldest` 只淘汰 `raw` 最旧者，**stable 永不被裁**——核心人设不再被近期琐事挤掉。
    - **检索加权**：`get_bullets_for_prompt` SELECT 增取 `salience, tier`；开 `use_salience_rerank` 时优先用**写入期 salience**（省重算），并给 `stable` 层 +0.05 小幅加权（`_STABLE_TIER_BOOST`）。
    - **触发**：`SkillManager._inject_episodic_into_context`（实为 extract 写入路径）在 **prune 前**按 `memory.consolidation.{enabled,min_hits,min_salience}` 调 `consolidate`，先晋升再裁剪。默认 **关**（遵循"新子系统默认关"）。
  - **零行为变化默认**：`memory.consolidation.enabled` 默认关 → 不晋升任何 stable → prune/检索退化为既有行为；`use_salience_rerank` 关时 tier 不参与排序（`test_rerank_off_unaffected_by_tier` 锁死）。
  - **配置**（top-level `memory:`，与 episodic 其它键同级；沿用 R2 惯例不写进 `config.example.yaml`）：
    ```yaml
    memory:
      consolidation:
        enabled: false      # 开 → extract 写入后、prune 前做 raw→stable 晋升
        min_hits: 2         # 复发 ≥ 此次数即晋升
        min_salience: 0.5   # 可选；情绪显著性 ≥ 此值也晋升（省略=只看复发）
        semantic_dedup: 0.92  # R5：可选；填阈值(或 true=0.92) → 晋升前先并语义近义事实
        resolve_contradictions: false  # R10：开 → 同槽冲突值(旧住北京/新住上海)旧值标 stale 排除注入
    ```
  - **测试**：`tests/test_episodic_consolidation.py`（10 例）：写入落 salience、重复累加 hits 仍返 None、consolidate 复发/情绪浓晋升、幂等、prune 保护 stable、stable 检索上浮、关重排时 tier 不影响序、**旧库 ALTER 升列 + last_seen 回填 + 仍可读写**。`test_episodic_memory`（含 `test_add_dedupe`/`test_prune`）全过。**全量 4986 passed / 31 skipped / 0 failed**（240s）。无 lint。
  - 优化思考：① **写入期定权**承接 R2 优化点③——salience 一次算落库、检索期复用，并成为巩固判据；② 复用 R2 的 `blend_rank` 通道叠加 stable 加权，不另起排序口径；③ 复发信号**复用既有 hash 去重**（零额外计算），把"丢弃重复"反转为"沉淀证据"，是改动最小的复发探测；④ stable 防裁 + 默认关，**风险隔离在开关后**可灰度。下一步候选：**R4 wellbeing / 反谄媚守卫**（延续 persona_guard 家族：危机识别 + 反谄媚护栏，情感陪聊的安全底线）；R5 近似去重巩固（当前只折叠**完全相同**事实，下一步用 embedding 把**语义近似**事实也归并/择优，进一步抗冗余）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R4 wellbeing / 反谄媚守卫（情感陪聊安全底线）✅**：聊得好之前，先聊得**安全**。
  - **选题依据**：陪聊产品最大的风险不是"聊得不够好"，而是**在用户最脆弱时聊错了**——对自伤/轻生信号轻描淡写、或为讨好一味附和强化对方有害念头（Replika 类产品被诟病、甚至致监管/诉讼的两点）。此前管道有 `persona_guard`（保沉浸感）却**无安全护栏**：危机消息和闲聊走同一条共情链路。
  - **方案**：新建 `src/utils/wellbeing_guard.py`（纯函数，与 `persona_guard` 同家族，**预防优于事后修剪**——注入 prompt 让模型一开始就答对，而非生成后判它谄媚/危险）：
    - `detect_crisis(text)` → `{level: none|elevated|severe, category, matched}`：**severe**=自伤/轻生明确意图（"不想活了/想自杀/割腕/want to die"…），**elevated**=深度绝望/无助（"撑不下去/好绝望/没人在乎我/我就是个废物"…）。**保守优先**：先用 `_IDIOM_EXCLUDE` 抹掉"累死了/笑死/想死你了/deadline"等日常夸张惯用语再匹配，从根上消除误伤；severe 优先 elevated。
    - `build_wellbeing_block(...)`：severe→「⚠️安全优先」指令（认真温柔接住、不说教/不轻描淡写/不表演震惊、表达"你很重要"、温柔引导求助、**绝不附和自伤**，可附配置热线）；elevated→「关怀优先」指令（充分确认情绪、多听少劝）；**反谄媚**常驻护栏（不为讨好强化自毁/有害念头，温柔但诚实）。
    - 集成 `emotional_context.build_emotional_context_block`：新增 `enable_wellbeing/enable_anti_sycophancy/wellbeing_hotline`，安全块**置于所有块最前**（最高优先级）；危机命中时把 `_wellbeing_crisis_level` 写回 `user_context` 供上层日志/指标，并 `logger.warning`。`SkillManager` 经 `companion.wellbeing.{enabled,anti_sycophancy,crisis_resources}` 透传。
  - **默认开**（与 `persona_guard` 同为安全家族；漏接危机是最坏结果，且纯 prompt 注入零行为风险）。可经配置关闭。
  - **测试**：`tests/test_wellbeing_guard.py`（12 例）：severe/elevated 分级、**惯用语零误伤**（累死/笑死/想死你/deadline…）、空与中性、severe 优先级、指令组装（危机在反谄媚前、热线仅 severe 附、全关→空串、只关反谄媚危机仍在）、emotional_context 集成（危机块置顶 + `_wellbeing_crisis_level` 落库 + 关闭后干净）。**全量 4998 passed / 31 skipped / 0 failed**（227s）。无 lint。
  - 优化思考：① **反谄媚改为按情绪触发**而非常驻——发现若每条 prompt 都塞"别盲目附和"，开心闲聊会被串味且白烧 token，遂在 `emotional_context` 里用 `cur_valence < -0.05 或危机` 门控（纯函数本身仍可独立常驻，门控只在集成层），既保安全又不污染日常语气；② **白名单抹惯用语再匹配**而非给每条危机正则写负向 lookahead——一处集中维护、可读、误伤面最小；③ 安全块**置顶**确保在长 prompt 里最高显著性；④ 默认开但全链路可配置关，风险与开关解耦。下一步候选：**R5 近似去重巩固**（承接 R3：用 embedding 把语义近似的 raw 事实归并/择优，进一步抗冗余、提巩固质量）；R6 危机**事后兜底**（severe 时即使模型仍答偏，可在 `_enforce_persona_consistency` 同位置加一道"安全句注入/软兜底"——本步先做预防，事后兜底留作下一层）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R5 近似去重巩固（语义近义事实归并）✅**：补全 R3 的"近似去重"挂账。
  - **选题依据**：R3 只折叠**完全相同**（hash 相等）的事实，但"喜欢猫"/"我养了只猫"/"家里有猫"是同一件事的不同说法——会各占一行、**稀释复发信号**（每条 hits=1 永远到不了晋升线）、并挤占 prune 名额。需用 embedding 把语义近义的 raw 事实归并。
  - **方案**：`episodic_memory_store.merge_near_duplicates(user_id, threshold=0.92, max_scan=200, min_raw=6)`——对 raw 层有 embedding 的事实做**贪心聚类**（余弦≥阈值即同簇），每簇择优留一条（salience→hits→新近→更长者胜），把其余条的 **hits 累加**到 survivor（"换着说法反复提"也沉淀为复发证据）后删除其余。仅作用 raw（stable 不动）。`consolidate(..., dedup_threshold=)` 新增可选参数：给值则**先并近义、再晋升**，返回增 `merged`。`SkillManager` 经 `memory.consolidation.semantic_dedup`（阈值或 `true`=0.92）透传。
  - **零行为变化默认**：`semantic_dedup` 不配 → `consolidate` 不调去重（`dedup_threshold=None`），既有 `test_episodic_consolidation` 全过；`merge_near_duplicates` 独立可调。
  - **性能**：O(n²) 但 n≤`max_scan`(200)，且**向量预归一化为点积**（省 n² 次开方）；raw 不足 `min_raw`(6) 直接早退（早期/小历史不值当）。
  - **测试**：`tests/test_episodic_semantic_dedup.py`（8 例，用 16 维近独热向量精确构造相似度）：高相似归并 + hits 累加、survivor 按 salience 择优、低相似不动、**stable 永不被并**、min_raw 早退、无 embedding 跳过、`consolidate(dedup_threshold)` 端到端（两条各 hits=1 的近义 → 并后 hits=2 → 跨过 min_hits 被晋升 stable）、不配去重时不变。**全量 5006 passed / 31 skipped / 0 failed**（264s）。无 lint。
  - 优化思考：① **向量预归一化**把 n² 余弦降为 n² 点积 + n 次开方，是这层最大的常数优化；② **hits 累加而非丢弃**让去重与 R3 复发晋升**正向耦合**——近义复述本就是最强的"这事对 TA 很重要"信号；③ 贪心聚类（非全连通）足够且 O(n²) 可控，避免引入聚类库依赖；④ 只动 raw + min_raw 早退 + 默认关，风险隔离。下一步候选：**R6 危机事后兜底**（把 R4 的"预防"升级为"预防+兜底"：severe 时在回复后置位补一道安全软兜底/指标）；R7 写入期 embedding 普及（当前近义去重依赖已落 embedding 的事实，可在 `add_fact` 路径补齐 embedding 覆盖率以提升去重召回）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R6 危机事后兜底（回复红线 + 安全覆盖）✅**：把 R4 的"预防"升级为"预防+兜底"双保险。
  - **选题依据**：R4 在**输入侧**注入安全指令（预防），但 LLM 仍可能在极端情况下答出**鼓励/认同自伤**的回复（"那就去死吧"）——这是陪聊产品最不可接受的失败，必须有一道**输出侧**的确定性兜底，不能只靠 prompt 自觉。与 `persona_guard`（保沉浸感的后置体检）同位置、同思路，补上"保命"这一层。
  - **方案**：`wellbeing_guard.py` 加纯函数——`detect_harmful_reply(reply)`（极保守：只命中明确祈使/认同自伤的句子，**含"别/不要/不想你"等否定劝阻词的句子一律放行**，避免把"别去死，你对我很重要"误判）+ `safe_fallback_reply(level, hotline)`（温柔、保持人设的安全兜底文案）。`SkillManager._apply_crisis_safety_net`（在 `_enforce_persona_consistency` 之后调用）：① **红线兜底**（随 `wellbeing.enabled` 默认开，无论是否检出输入危机）——回复触红线 → 整段覆盖安全兜底，并 `logger.error` + 置 `_wellbeing_safety_override`；② **资源保障**（`crisis_resource_assurance` 默认关）——severe 危机且配热线且回复未含求助提示 → 温柔补一句资源。
  - **零行为变化默认**：正常温暖回复不命中任何红线 → 原样返回（`test_safety_net_keeps_good_reply` 锁死）；资源保障默认关 → severe 也不强行附热线（避免每条机械化）。
  - **测试**：`tests/test_wellbeing_safety_net.py`（13 例）：红线命中（中英祈使/认同）、**否定语境放行**（别去死/我不想你死/千万别伤害自己）、正常回复放行、兜底文案含陪伴/热线；集成 `_apply_crisis_safety_net`：覆盖触红线回复 + 置 override 标记、保留正常回复、资源保障开/关、非 severe 不附、`enabled=false` 整体放行。**全量 5016 passed / 31 skipped / 0 failed**（238s）。无 lint。
  - 优化思考：① **红线兜底默认开、资源保障默认关**——前者是"保命"不可妥协（且只在真触红线时才动，正常零影响），后者会改动正常文案故保守留开关，安全与体验各取其位；② **否定词整句放行**而非逐 pattern 写 lookahead——把"别/不要/不想你"集中成一道前置闸，可读、误伤面最小（与 R4 的"白名单抹惯用语"同款思路）；③ **整段覆盖而非剥句**——危机场景下留半句残文比覆盖风险更高，安全优先选确定性最强的整段兜底；④ 复用 R4 写回的 `_wellbeing_crisis_level`，输入/输出两侧共享同一危机判定，不另起口径。下一步候选：**R7 写入期 embedding 普及**（补齐 `add_fact` 路径 embedding 覆盖率，提升 R5 近义去重召回）；R8 危机会话**人工接管/升级**钩子（severe 连续命中 → 触发 handoff/告警，把安全闭环接到 contacts/handoff 子系统）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R8 危机人工接管/升级（机器兜底之上让真人介入）✅**：安全闭环的最后一环。
  - **选题依据**：R4 预防 + R6 机器兜底已能拦住"答错"，但**真实危机最负责任的处理是让真人介入**——自动陪聊不该独自承担一条人命的判断。仓库已有成熟的 escalation 设施（`_check_escalation`/`_fire_escalation_webhook`，事件 `escalation_needed` + `webhook_settings.json`），R8 不重造轮子，把危机信号接到这条既有告警通道。
  - **方案**：`SkillManager._maybe_escalate_crisis`（在 R6 兜底之后调用）——始终维护**危机连击计数** `_wellbeing_crisis_streak`（severe 自增、非危机清零、elevated 维持），仅当 `companion.wellbeing.crisis_escalation` 开（默认关，需配 webhook + 真人值守）且连击 ≥ `escalate_after`（默认 1）且过 30 分钟冷却时，置 `_crisis_escalation_triggered` 并 `loop.create_task(_fire_crisis_webhook(...))`。`_fire_crisis_webhook` 复刻既有 webhook 读取逻辑，复用 `escalation_needed` 事件通道（无需用户新增订阅）并附 `category=crisis / severity=high` 标记，summary 注明"疑似自伤/轻生，请尽快人工介入"。
  - **零行为变化默认**：`crisis_escalation` 默认关 → 只数连击不告警（连击计数供观测，零外部副作用）；冷却 30 分钟防止刷屏。
  - **测试**：`tests/test_wellbeing_escalation.py`（9 例）：连击 severe 自增 / 非危机清零 / elevated 维持；门控——关闭不触发、开启即触发、`escalate_after` 阈值、冷却阻止重复、非 severe 不触发、`enabled=false` 仍计数但不告警。**全量 5025 passed / 31 skipped / 0 failed**（217s）。无 lint。
  - 优化思考：① **复用既有 escalation_needed 通道**而非新建危机事件——现有 webhook 订阅者零改动即可收到，用 `category=crisis` 区分路由，降落地成本；② **连击计数与告警解耦**——计数始终维护（即使 escalation 关），既为将来阈值策略留数据、也让 `enabled=false` 时仍可观测，告警单独 gate；③ **危机冷却 30 分钟 < 普通升级 60 分钟**——危机更急但仍需防刷屏，取折中；④ 纯旁路 + 全异常吞掉，绝不影响主回复。下一步候选：**R7 写入期 embedding 普及**（补齐 `add_fact` embedding 覆盖率，提升 R5 近义去重召回）；R9 危机**事件落库/审计**（当前仅 webhook + 日志，可在 contacts 侧落一张危机事件表供复盘/合规）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R7 写入期 embedding 普及（覆盖率跟随需求）✅**：给 R5 近义去重补地基。
  - **选题依据**：R5 近义去重靠 embedding 算余弦，但**整条 embedding 管线（写入期 patch / 启动 / 周期 / 手动补全）都只看 `memory.vector.enabled`**。于是若运营只想要"近义去重"而没开"向量检索融合"，事实就**全程不带 embedding** → R5 静默空转。这是 R5 留下的隐性耦合缺口。
  - **方案**：抽一个单一真源谓词 `SkillManager._episodic_embeddings_needed()`——`memory.vector.enabled` **或** R5 `memory.consolidation.semantic_dedup` 任一为真即返回 True。把 `_episodic_patch_embedding`（写入期）与 `episodic_backfill_embeddings`（手动/启动/周期补全的统一入口）的旧 `vector.enabled` 闸**改走该谓词**。于是"打开 semantic_dedup"会**自动**让新事实写入带 embedding、并让补全任务可用于回填历史事实——覆盖率跟随需求普及，而成本仍由各功能各自显式开关 + 既有 `daily_embed_budget` 把关。
  - **零行为变化默认**：两者都关 → 谓词 False → 行为与旧版完全一致（`episodic_backfill_embeddings` 仍回 `vector_disabled`，既有 `test_episodic_backfill` 全过）。
  - **测试**：`tests/test_episodic_embedding_coverage.py`（9 例）：谓词四态（全关/vector/dedup 阈值/dedup 布尔/dedup=0 假值）、写入期 patch 在"仅开 dedup"时也嵌入、啥都没开时跳过、补全在 dedup-only 不再被判 `vector_disabled`、全关仍 `vector_disabled`。**全量 5034 passed / 31 skipped / 0 failed**（184s）。无 lint。
  - 优化思考：① **单一谓词收口**——把"要不要 embedding"从三处散落的 `vector.enabled` 收成一个方法，新增向量消费方（未来 R 步）只改一处即可纳入；② **覆盖率跟随需求、成本仍按功能开关**——不是无脑全量嵌入，而是"有人要用才嵌"，与既有日预算叠加，既补召回又不失控；③ 默认零变化、旧测试全过，风险隔离。下一步候选：**R9 危机事件落库/审计**（contacts 侧落危机事件表，承接 R8 的 webhook/日志做可复盘/合规闭环）；R10 记忆**矛盾消解**（近义归并已做，下一步处理"相互冲突"的事实——如旧"住北京"vs新"住上海"，按新近/置信择一并标注）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R10 记忆矛盾消解（冲突值按新近择一）✅**：让"真的记得我"不自相矛盾。
  - **选题依据**：R5 近义归并处理"同一件事的不同说法"，但还有一类是**相互冲突**——旧"住北京" vs 新"住上海"、旧"单身" vs 新"有对象了"、旧"喜欢猫" vs 新"讨厌猫"。这些**既不能并**（会丢信息）、**也不能都留**（AI 会前后矛盾，直接击穿"它真的记得我"的体感）。更隐蔽的是：R5 阈值 0.92 下"住北京/住上海"语义很近，**可能被 R5 误并**——必须在去重**之前**先消矛盾。
  - **方案**：新建 `src/utils/memory_slots.py`（纯函数）——`extract_slot(text)` 把事实归到**单值属性槽** `(slot_key, value, polarity)`：身份槽 `name`/`residence`/`relationship`（含 heuristic 模板"用户自称：X""住在X"与关系状态关键词→规范值 single/partnered/married/divorced），偏好槽 `pref:对象`（喜欢=+1/不喜欢=-1）；`slots_conflict(a,b)` 判同身份槽不同值或同对象相反极性。`episodic_memory_store.resolve_contradictions(user_id)`：按槽分组，组内以 `last_seen`（回退 `created_at`）取最新，把与最新**冲突**的旧条标 `stale` 层。`get_bullets_for_prompt` 新增 `tier != 'stale'` 过滤（stale 排除出 prompt 但保留备查）。`consolidate` 新增 `resolve_contradictions` 参，**顺序：消矛盾 → 近义去重 → 晋升**。`SkillManager` 经 `memory.consolidation.resolve_contradictions`（默认关）透传。
  - **零行为变化默认**：默认关 → 不标任何 stale；地名规范化（"北京"=="北京市"）保证同地不同写法不误判冲突。
  - **测试**：`tests/test_memory_contradiction.py`（14 例）：槽解析（住址/城市后缀归一/关系规范值/称呼模板/偏好极性/无槽）、冲突判定（同槽异值、偏好反极性、异槽不冲突）、store 集成（旧标 stale 新留 raw、stale 排除出 prompt、同地不误标、consolidate 消矛盾、默认不消解）。**全量 5048 passed / 31 skipped / 0 failed**（232s）。无 lint。
  - 优化思考：① **顺序锁定"先消矛盾再去重"**——这是关键交互洞察：stale 后旧冲突值就退出了 R5 的 raw 扫描面，根除"住北京/住上海被误并"；② **stale 软删而非物删**——超期旧值保留可审计/可回溯（万一是误判也能恢复），且 prune 仍可正常淘汰它（非 stable）；③ **保守解析、宁漏不误**——只归"确定单值"的槽，自由偏好对象需精确匹配才判冲突，把误杀好记忆的风险压到最低；④ 默认关 + 纯函数可单测。下一步候选：**R9 危机事件落库/审计**（接 R8 落危机事件表做合规闭环）；R11 矛盾消解**接入 stable**（当前只在 raw 内消解，真实"搬家/分手"应能更新已晋升的 stable 结论，需更高置信门槛）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R9 危机事件落库/审计（安全链的"留痕"一环）✅**：让 R4→R6→R8 可复盘、可合规。
  - **选题依据**：R4 预防 / R6 兜底 / R8 接管已成安全链，但全程**只有 webhook + 日志，留不下结构化记录**。一个会触及真实心理危机的陪聊产品，必须能事后复盘"哪些用户出过危机、是否被人工处理过"、满足合规审计、追踪处置闭环——这是责任底线。
  - **方案**：新建 `src/utils/crisis_event_store.py`（SQLite 轻量表 `crisis_event`，落 `bot.db` 同库独立表）——`record(...)`（时间/用户/会话/等级/类别/连击/是否升级/是否触发兜底/≤120 字短摘要）+ `list_recent(only_unhandled, user_prefix)` + `mark_handled(by, note)` + `count`。`SkillManager.__init__` 初始化 `_crisis_store`；`_maybe_escalate_crisis` 在升级判定后，按 `companion.wellbeing.crisis_audit`（默认关）落库，并把本轮 `escalated_now` 与上一步 `_apply_crisis_safety_net` 写的 `_wellbeing_safety_override` 一并记入。
  - **顺带修一个 R8 潜伏 bug**：原 `emotional_context` 只在**检出危机时**写 `_wellbeing_crisis_level`、平静轮不清零 → 危机等级会"粘住"，导致 R8 连击计数在后续平静轮**继续累加 / 误升级**。改为**每轮都回写**（含 `none`）→ 平静轮自动清零（`test_calm_turn_resets_crisis_level` 锁死）。同时写 `_wellbeing_crisis_category` 供审计。
  - **隐私 & 默认关**：`crisis_audit` 默认关（含敏感数据）；只存短摘要非全文；override 信号读后即清（`pop`），不跨轮污染。
  - **测试**：`tests/test_crisis_event_store.py`（10 例）：record/list、摘要截断 120、only_unhandled + mark_handled 闭环、user_prefix 筛、缺失行 mark 返 False；SkillManager 接线——开启即落、关闭不落、非危机不落、捕获 escalated+override 且 override 读后清零；以及 R8 等级清零修复。**全量 5058 passed / 31 skipped / 0 failed**（225s）。无 lint。
  - 优化思考：① **审计与升级解耦**——`crisis_audit` 独立于 `crisis_escalation`，可"只留痕不告警"（轻量观测）或两者并用；② **修了潜伏 bug 才敢落库**——若不先修等级粘住，审计会记下一堆平静轮的假危机，"实现下一层时回头夯实上一层地基"；③ **独立表 + 独立 store**（不动 contacts schema）——降耦合、可单独清理/导出，符合"危机数据单独治理"；④ override 读后清零，杜绝跨轮误标。下一步候选：**R9b 危机审计后台页/接口**（list/mark_handled 已就绪，补一个 admin 路由 + 页面给值守人员用）；R11 矛盾消解接入 stable；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R9b 危机审计 API（把 R9 数据底座变成可操作工作台）✅**：值守人员能"列未处理危机 → 标记处置 → 留痕"。
  - **选题依据**：R9 已把危机事件落库（`CrisisEventStore`），但只有 store 没有出口——值守人员无从看、无从处置。R9b 把数据底座暴露成 admin API，闭合"接管 → 处置 → 留痕"。
  - **方案**：① `SkillManager` 加三个薄包装 `crisis_list_for_admin / crisis_count_for_admin / crisis_mark_handled_for_admin`（防御式 `getattr(self, "_crisis_store", None)`，store 缺失返回空/False，不抛）；② 新建 `src/web/routes/crisis_audit_routes.py`：`GET /api/crisis-events`（`only_unhandled`/`prefix`/`limit`，附 `unhandled_total` 角标）+ `POST /api/crisis-events/{id}/handle`（body `note`，处理人取自 session username/role）；③ 读用 `api_auth`、写（标记处置）用 `manage_ops` 权限（与"确认/指派运维事件"同级，master+admin）；④ 经 `AdminRouteContext` 注入，在 `admin.py` 紧随 episodic 路由注册，整体 try/except 包裹不阻断启动。
  - **测试**：`tests/test_web_crisis_audit_api.py`（5 例）：列表 + `unhandled_total`、`only_unhandled` 过滤、handle 写库回填 note、缺失事件 404、错 token 拒绝（401/403）；并把两个新端点登记进 `test_admin_route_inventory.py` 白名单。**全量 5067 passed / 31 skipped / 0 failed**（约 263s）。无 lint。
  - 优化思考：① **薄包装而非路由直连 store**——路由不碰 `_crisis_store` 私有属性，未来换存储只改 SkillManager，与既有 `episodic_*_for_admin` 风格一致；② **复用现成权限模型**——不新造 perm，挑语义最贴的 `manage_ops`，零改 RBAC 表；③ **handled_by 取自 session**——留痕带真实处理人，满足合规"谁处置的"；④ **本阶段只做 API、页面拆为 R9c**——保持每步可评审、可回滚，避免一步牵动 nav/perms/template/inventory 四处。下一步候选：**R9c 危机审计页面**（`/crisis-audit` 模板 + 导航 + `PAGE_PERMISSIONS`，给非技术值守人员一个真 UI）；R11 矛盾消解接入 stable；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R9c 危机审计页面（值守工作台 UI）✅**：非技术人员可点进来看、处置、留痕。
  - **选题依据**：R9b 已有 API，但值守人员不会调 curl。R9c 补 `/crisis-audit` 页面 + 侧栏导航 + 仪表盘告警，把危机审计变成**日常可操作**的工作台。
  - **方案**：① 新建 `crisis_audit.html`（筛选用户/仅未处理、等级徽章、升级/兜底标记、modal 备注处置）；② `admin.py` 注册 `/crisis-audit` 页面路由 + `_PATH_TO_PAGE` / `_PATH_TO_ACTIVE`；③ `PAGE_PERMISSIONS` + `SIMPLE_MODE_MORE_PAGES` 加 `crisis_audit`（master/admin/viewer 可看，仅 master/admin 可处置，与 `manage_ops` 对齐）；④ `base.html` 简洁/完整双模式侧栏加「危机审计」+ `badge-crisis` 角标；⑤ `health_routes` `/api/alert-status` 聚合未处理危机——有 severe 则 critical 横幅，否则 warn，action 链到 `/crisis-audit?only_unhandled=1`。
  - **测试**：`test_web_crisis_audit_api.py` 增 2 例（页面 200 + `ca-body`、alert-status 含 crisis 类型告警）；route inventory 登记 `/crisis-audit`。**全量 5065 passed / 31 skipped / 0 failed**（约 250s）。无 lint。
  - 优化思考：① **三层提醒共用一数据源**——侧栏角标 + 仪表盘横幅 + 页面内汇总 pill，全走 R9b 的 `unhandled_total`；② **viewer 只读、admin 可处置**——与 episodic 删除按钮同模式；③ **告警 severity 按 severe 有无分级**——避免 elevated-only 也刷红横幅；④ **处置后即时刷新角标**。下一步候选：**R11 矛盾消解接入 stable**；**R9d 坐席工作台危机入口**（unified inbox 用户侧栏显示最近危机 + 一键跳审计页）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R11 矛盾消解接入 stable（让"搬家/分手"能更新已晋升的结论）✅**：长期记忆可信度的关键一环。
  - **选题依据**：R10 只在 `raw` 层内消解矛盾，但用户「搬家」「分手」「结婚」这类真实变更，旧结论往往**早已晋升 `stable`**（受检索加权、永不被 prune），新值再怎么说也盖不过——AI 会长期坚持过时事实。但 `stable` 是高置信结论，不能被一次随口提及（"出差去上海一周"）冲掉，需要**更高的推翻门槛**。
  - **方案**：`resolve_contradictions` 新增 `supersede_stable` / `stable_min_hits`（默认 2）。开启后：① 额外按槽分组拉 `stable` 行；② 对每个槽，算 newest raw 槽值的**累计 hits**（同槽同值的 raw 事实 hits 之和——"反复提及=真的变了"）；③ 仅当累计 hits ≥ `stable_min_hits` 时，把同槽冲突的 stable 条标 `stale`。`consolidate` 透传二参；`SkillManager` 经 `memory.consolidation.{supersede_stable,stable_min_hits}` 接线。返回新增 `stable_superseded`。
  - **闭环妙处（prune 自然收口）**：被推翻的 stable → `stale`，而 `prune_oldest` 删的正是 `tier != 'stable'`，故旧结论会按新近**自然淘汰**、不会无限堆积；同一轮 `consolidate` 里 supersede（标旧 stale）→ dedup → promote（新 raw 若 hits 够则晋升 stable），**旧结论退役 + 新结论上位**一气呵成。
  - **零行为变化默认**：`supersede_stable` 默认关 → `resolve_contradictions` 行为与 R10 字节级一致（`test_supersede_stable_off_by_default` 锁死），`stable_superseded` 恒 0。
  - **测试**：`tests/test_memory_contradiction.py` 增 5 例：单次提及不推翻（证据不足）、反复提及（hits≥门槛）推翻且 stale 排除出 prompt、默认关不动 stable、`consolidate` 透传、同值不算冲突不动 stable。**全量 5070 passed / 31 skipped / 0 failed**（约 203s）。无 lint。
  - 优化思考：① **置信门槛用累计 hits 而非单条**——复用 R3 既有的 hits 复发信号，零新增计算，"真的搬了会反复说"语义自洽；② **supersede 在 dedup 前跑**——dedup 只会让 survivor hits 更高、晋升更稳，顺序无副作用；③ **stale 而非物删**——误判可回溯、且 prune 兜底防膨胀，与 R10 一致；④ **默认关可灰度**——线上可单独 A/B `supersede_stable` 的体验收益。下一步候选：**R9d 坐席工作台危机入口**（把 R9c 的审计能力嵌进 unified inbox，坐席处置不用切后台）；R12 **记忆置信/来源标注**（区分"用户明说"vs"AI 推断"，推断类不该轻易晋升 stable）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R12 记忆置信/来源标注（按来源分级置信）✅**：让"用户明说"与"AI 推断"在巩固/推翻时区别对待。
  - **选题依据**：R11 让 stable 可被推翻后，暴露更深的问题——当前所有事实**一视同仁**。但"用户亲口说我搬家了"和"AI 从语气推断 TA 可能搬家了"可信度天差地别。推断类事实不该轻易晋升 stable、更不该推翻用户明说的稳定结论，否则一次 LLM 误判就能污染长期人设。
  - **方案**：① schema 升列 `source TEXT DEFAULT 'user_stated'`（`_ensure_source_column`，旧行默认明说=行为不变）；② `add_fact(source=)`——`SkillManager` 把**启发式事实**（从用户原话正则提取）标 `user_stated`、**LLM 抽取**（对话推断/概括）标 `ai_inferred`；③ **复发升级**：AI 先推断、用户后亲口确认同一事实（同 hash）→ 升格 `user_stated`，绝不反向降级；④ `consolidate(source_aware=)`：开后 `ai_inferred` 晋升 stable 需更高复发门槛（`inferred_min_hits`，默认 `min_hits+1`）**且不走情绪显著性捷径**（"推断+情绪浓"仍是猜测）；⑤ `resolve_contradictions(source_aware=)`：推翻 stable 的证据**只数 `user_stated` 的 hits**。`source_aware` 经 `memory.consolidation.source_aware` 接线，默认关。
  - **零行为变化默认**：`source` 列纯数据始终落（审计友好），但**行为门槛全部 gated 在 `source_aware` 后**，默认关 → 巩固/推翻与 R11 字节级一致（`test_source_aware_off_keeps_r11_behavior` 锁死）。
  - **测试**：`tests/test_memory_source_confidence.py`（13 例）：默认 user_stated、ai_inferred 标注、非法值回退、复发升级/不降级、source_aware 下推断高门槛+无情绪捷径、明说走原门槛、推断证据推不翻 stable / 明说能推翻、旧库 ALTER 升列回填。**全量 5083 passed / 31 skipped / 0 failed**（约 238s）。无 lint。
  - 优化思考：① **来源映射零额外推断**——直接用既有"启发式 vs LLM"两条抽取路径天然对应 user_stated/ai_inferred，不引入额外分类器；② **复发即升级**——把"AI 猜→用户证实"建模为置信跃迁，比静态标注更贴真实对话；③ **纯数据 always-on、行为 gated**——source 列永远落（未来审计/画像可用），但门槛行为默认关可灰度，风险隔离；④ **推断不享情绪捷径**——堵住"AI 脑补一句煽情的话就固化人设"的污染路径。下一步候选：**R9d 坐席工作台危机入口**（unified inbox 嵌危机审计）；**R13 记忆来源可视化**（admin 情景记忆页显示 source 徽章 + 按来源筛，让运营看清哪些是 AI 推断）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R13 记忆来源可视化（把 R12 数据底座变成运营可纠错的视图）✅**：运营一眼分辨"AI 推断"并手动纠错。
  - **选题依据**：R12 已把 `source` 落库，但运营/值守在情景记忆页看不到——分不清哪些是用户明说、哪些是 AI 脑补。R13 把来源/层级暴露到既有 admin 页，让人能**看清并删掉错误推断**，是 R12 数据底座的自然出口。
  - **方案**：① `list_rows` SELECT 增取 `source/tier/hits` 并加可选 `source` 筛选（白名单校验，非法值忽略=全量）；② `episodic_list_for_admin` 与 `/api/episodic-memory` 透传 `source`；③ `episodic_memory.html` 新增「来源/层级」列——`用户明说`（绿）/`AI 推断`（琥珀）徽章 + `稳定`/`已弃`层级徽章 + `×hits` 复发次数，并加来源下拉筛选。
  - **零行为变化默认**：不传 `source` → 行为与既有一致；新列纯展示，删除按钮逻辑不变。
  - **测试**：`test_memory_source_confidence.py` 增 3 例（list_rows 暴露 source/tier/hits、source 筛选、非法值返全量）；`test_web_episodic_memory_api.py` 增 2 例（API 透传 source、非法值归空）。**全量 5088 passed / 31 skipped / 0 failed**（约 219s）。无 lint。
  - 优化思考：① **复用既有页面而非新建**——R13 只在 episodic 页加一列一筛，零新增路由/权限，改动最小闭环；② **一列承载三维信息**（来源+层级+复发次数）——运营纠错时一眼看全"这条多可信"；③ **白名单筛选防注入**——source 参数仅认两个枚举值，其余忽略；④ **服务端筛选而非前端**——避免 limit 截断后再过滤导致少显。下一步候选：**R9d 坐席工作台危机入口**（把 R9c 审计嵌进 unified inbox）；**R14 记忆画像聚合卡**（按 source/tier 在用户侧栏汇总"已知 N 条稳定事实/M 条待确认推断"）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-18 · **R9d 坐席工作台危机入口（把安全链送到一线手边）✅**：坐席处置危机不用切后台。
  - **选题依据**：R9→R9c 把危机安全链（落库→API→审计页）建全了，但一线坐席在 unified inbox 接待用户时，看不到这个用户**是否出过危机**——要切到后台审计页才知道。R9d 把危机概览嵌进坐席工作台的客户画像侧栏，处置时一眼可见 + 一键直达审计页。
  - **方案**：① `SkillManager.crisis_summary_for_user(user_key)`——按 `user_key` 前缀查危机库，返回最近 N 条 + 未处理数 + 最新一条精简信息（防御式，store 缺失/无命中返空概览，绝不抛）；② `unified_inbox_services` 加 `_skill_manager(request)` helper（经 `telegram_client.skill_manager`）；③ 复用既有 `/api/unified-inbox/contact-profile` 聚合端点，新增 `crisis` 块（从 `conversation_id=platform:account:chat_key` 取 chat_key 作 user_key；**仅在有危机记录时挂出**，避免给侧栏添噪）；④ `draft_review.html` 的 `_renderProfile` 顶部加醒目危机横幅（severe 红/elevated 琥珀 + 未处理数 + "前往危机审计"深链）。
  - **零行为变化默认**：无 `skill_manager` 或无危机记录 → `crisis` 为 `null`，画像渲染与既有一致；纯增量字段。
  - **作用域诚实声明**：以 `chat_key` 作前缀匹配，命中情感陪聊**主场景 1:1 私聊**（chat_key 即对端用户 id）；群聊（chat_key=群 id，而危机 user_id=个人）暂不覆盖，已记为已知限制。
  - **测试**：`tests/test_crisis_inbox_sidebar.py`（8 例）：summary 空 store/无命中/计数+最新/未处理排除已处置/空 key；contact-profile 挂出 crisis 块 / 无记录为 None / 无 skill_manager 为 None。**全量 5096 passed / 31 skipped / 0 failed**（约 334s）。无 lint。
  - 优化思考：① **复用聚合端点而非新路由**——挂进既有 contact-profile，侧栏已经在调它，零前端新请求、零 inventory 改动；② **有记录才挂出**——`crisis=null` 时前端不渲染，绝不给正常用户的侧栏添噪；③ **精简投影**——summary 只回 level/category/escalated/handled/ts 五字段，不泄全文摘要到坐席侧栏（敏感数据最小暴露）；④ **作用域诚实**——明确只覆盖 1:1 私聊、群聊记为限制，不假装通用。下一步候选：**R9e 群聊危机对齐**（按 chat_id 维度补群聊场景）；**R14 记忆画像聚合卡**（source/tier 汇总）；以及仍挂账的 R1 arc_hint↔策略块去重合并。

- 2026-06-19 · **R9e 群聊危机对齐（补齐 R9d 主动声明的限制）✅**：群聊侧栏也能看见危机概览。
  - **选题依据**：R9d 诚实声明了"只覆盖 1:1 私聊"——群聊里危机 `user_id`=触发者个人、`chat_id`=群，而侧栏拿到的 `chat_key`=群 id，故 R9d 的 user_id 前缀匹配命中不了群聊危机。R9e 立即补齐这块，不让"已知限制"长期悬着。
  - **方案**：① `CrisisEventStore.list_recent` 新增 `match_key`——`(user_id LIKE key% OR chat_id = key)` 的 OR 语义，**一个 key 同时覆盖私聊（key=对端 user_id 前缀）与群聊（key=群 chat_id 精确）**；② `crisis_summary_for_user` 从 `user_prefix=key` 改用 `match_key=key`，坐席侧栏无需区分场景；③ 后台审计页的 `user_prefix` 保持不变（仍按 user_id 筛，语义清晰）。
  - **关键设计——chat_id 精确而非前缀**：群 id 形如 `-100200`，若也走前缀匹配，`-10` 会误命中 `-100200`、`-100999` 等相邻群；故 `chat_id = key` 用等值，杜绝跨群串台（`test_summary_chat_id_exact_not_prefix` 锁死）。
  - **零行为变化默认**：`match_key` 为新增可选参数，不传则 `list_recent` 行为与既有完全一致；后台审计页未改。
  - **测试**：`tests/test_crisis_inbox_sidebar.py` 增 4 例（群聊按 chat_id 命中、私聊仍按 user_id 命中、chat_id 精确不前缀误命中、store `match_key` 的 OR 语义）。**全量 5178 passed / 31 skipped / 0 failed**（约 216s）。无 lint。
  - 优化思考：① **一个 key 双语义**——不让坐席侧栏判断"这是私聊还是群"，OR 匹配自动覆盖，调用方零心智负担；② **user_id 前缀 vs chat_id 精确分而治之**——user_id 留前缀（兼容 `群id_用户id` 复合 key 的历史写法），chat_id 走精确（防跨群串台），两种 key 各按其特性匹配；③ **审计页与侧栏解耦**——`user_prefix`（审计，user 维度）与 `match_key`（侧栏，会话维度）两个独立入口，互不影响。下一步候选：**R14 记忆画像聚合卡**（source/tier 在侧栏汇总"已知 N 条稳定事实/M 条待确认推断"，把 R12/R13 的来源置信送到坐席手边）；**R1 arc_hint↔策略块去重合并**（仍挂账，省 token）。

- 2026-06-19 · **R14 记忆画像聚合卡（把 R12/R13 来源置信送到坐席手边）✅**：坐席侧栏一眼掌握"对这个用户我们确切知道什么、哪些还只是 AI 猜的"。
  - **选题依据**：R12 落了 `source`（user_stated/ai_inferred）、R13 让运营在后台能看/筛，但**一线坐席接待时**仍看不到"这条记忆是用户明说的还是 AI 推断的"——容易把 AI 猜测当事实用。R14 把聚合视图送进工作台侧栏，复用 R9d/R9e 同一套 contact-profile 机制。
  - **方案**：① `EpisodicMemoryStore.profile_summary(user_id, top_stable=3)`——一条 `GROUP BY tier, source` 聚合出 `{total, stable, raw, user_stated, ai_inferred, top_stable[]}`，**排除 stale（已弃事实不计）**，再取 salience/hits 最高的若干稳定事实做"核心摘要"；② `SkillManager.episodic_profile_summary` 薄包装（无 store/空 key 返回空概览，绝不抛）；③ contact-profile 端点新增 `memory_profile` 块——先按 `chat_key`（私聊=对端 user_id，episodic 主存储键）查，命不中退回完整 `conversation_id`，**有记录才挂出**；④ `draft_review.html` 侧栏渲染"稳定/原始/用户明说/AI 推断"计数 badge + 核心稳定事实 chips（AI 推断>0 才显琥珀标，正常用户不添噪）。
  - **关键设计——store 实例选择**：`app.state.episodic_memory_store` 实际未在 main 注册（`_memory_bullets` 生产环境多半空跑），故 R14 走 `skill_manager._episodic_store`（必定已装），与 R9d 危机块同源，可靠且键一致。
  - **零侵入**：未加新路由（挂进既有 contact-profile，inventory 零改动）；`memory_profile=null` 时前端不渲染；`profile_summary` 任何异常返回空概览。
  - **测试**：`tests/test_memory_source_confidence.py` 增 4 例（空/按 tier·source 计数/top_stable 且 stale 排除/空 key）；新建 `tests/test_memory_profile_sidebar.py` 7 例（包装空 store·空 key·计数 + 端点挂出·空为 None·无 skill_manager·cid 回退）。memory+inbox 链 **85 passed**；无 lint。
  - 优化思考：① **一条 GROUP BY 出全部计数**——不为每个 tier/source 单独 query，单次聚合 O(1) 往返；② **stale 不计入**——R10/R11 推翻的旧事实不该再算进"我们知道什么"，聚合层就过滤；③ **top_stable 按 salience→hits→新鲜度排序**——把最重要、最常被印证的稳定事实顶到坐席眼前，而非随机几条；④ **键回退策略**——chat_key 优先（覆盖 scope=user 的私聊主路径），cid 兜底（覆盖复合键/CPI 场景），双候选与侧栏既有 memory bullets 查法对齐。下一步候选：**R15 侧栏"待确认推断"一键转明说**（坐席点确认即 `add_fact(source=user_stated)` 触发 R12 升级链，把 AI 猜测升格为事实，形成人工校正闭环）；仍挂账的 **R1 arc_hint↔策略块去重合并**（省 token）。

- 2026-06-19 · **R15 侧栏"待确认推断"一键转明说（人工校正闭环）✅**：坐席看见 AI 推断后能当场背书，把猜测升格为已知事实。
  - **选题依据**：R14 让坐席**看见**了"AI 推断 N 条"，但只读——发现猜对了也无从落地。R15 补上动作：每条 raw 的 ai_inferred 事实旁加"确认"按钮，点一下即把它升格为 `user_stated` 且直接置 `stable`，形成"AI 猜 → 坐席核 → 成事实"的人工校正闭环。
  - **方案**：① `profile_summary` 增 `pending_inferred:[{id,content}]`——按 salience/hits/新鲜度取最多 6 条 **raw+ai_inferred** 待确认事实（count 含全部非 stale 推断，列表只挑可确认的 raw 子集）；② `EpisodicMemoryStore.confirm_inferred_fact(row_id)`——`UPDATE source='user_stated', tier='stable'`，**WHERE 限定当前仍是 ai_inferred**（防误改用户明说事实的 tier），返回是否命中；③ `SkillManager.episodic_confirm_for_admin` 薄包装；④ `POST /api/episodic-memory/{id}/confirm`（复用 `_api_write("episodic_memory")` 权限，与 delete 同级）；⑤ `draft_review.html` 渲染待确认 chips + 绿色"确认"按钮，点击 POST 后**就地重拉 contact-profile 刷新画像**（按钮即时消失、稳定计数+1）。
  - **关键设计——人工背书 > 复发**：确认不只改 source，而是**直接置 stable**（跳过 consolidate 的 min_hits 门槛）。理由：一个真人坐席当面核实，是比"AI 又听到两次"更强的置信信号，应立即生效给坐席即时反馈，而非等下一轮 consolidate。
  - **零侵入回退**：`pending_inferred` 仅在有 ai_inferred 时填充；`confirm` 对非推断行/缺失行返回 False → 端点 404；前端确认失败按钮自动复原。
  - **测试**：`tests/test_memory_source_confidence.py` 增 6 例（pending 列表/无推断为空/确认升格 stable+user_stated/只作用 inferred/缺失行 False/坏 id）；`tests/test_web_episodic_memory_api.py` 增 3 例（确认调包装/非推断 404/无 bot 503）；`tests/test_memory_profile_sidebar.py` 补 pending 透传断言；route inventory 增 `/api/episodic-memory/{row_id}/confirm POST`。**全量 5322 passed / 31 skipped / 0 failed**（约 279s）。无 lint。
  - 优化思考：① **WHERE 守门而非应用层判断**——`confirm_inferred_fact` 把"仅 ai_inferred 可确认"写进 SQL WHERE，并发下也不会误改；② **count 与 list 解耦**——徽标计数保留全部非 stale 推断（含已晋升 stable 的），待确认列表只列 raw（能确认的），语义各自清晰；③ **就地刷新而非整页**——确认后只重拉该侧栏 contact-profile，坐席视线不跳走；④ **权限对齐 delete**——确认是写操作，沿用 `episodic_memory` 写权限，不新设角色。下一步候选：**R16 确认动作落审计**（谁在何时确认了哪条推断，进 AuditStore，与危机处置审计对称，便于回溯校正质量）；或回收一直挂账的 **R1 arc_hint↔策略块去重合并**（省 token，纯清理）。

- 2026-06-19 · **R16 确认动作落审计（人工校正可追溯）✅**：每次"把 AI 推断确认成事实"留痕，谁/何时/哪条一目了然。
  - **选题依据**：R15 让坐席能改记忆了，但改完无痕——无法回溯"这条稳定事实当初是谁拍板的"，也没法统计校正质量（坐席确认了多少推断、有无滥点）。R16 对称补上可追溯性，与 R9b 危机处置审计同构。
  - **方案**：① `confirm_inferred_fact` 返回类型从 `bool` 改 `Optional[str]`——命中即先 `SELECT content` 再 `UPDATE`，**返回被确认的原文**（而非仅 True），供路由写审计；② `episodic_confirm_for_admin` 同步返回 content/None；③ confirm 端点成功后调 `ctx.audit_store.log(actor, "episodic_confirm_inferred", target=row_id, old_val="ai_inferred", new_val=content[:200])`，actor 取 session username/role；④ 审计写入包 try/except，绝不因审计失败阻断主流程。
  - **关键设计——记 content 而非只记 id**：被确认的行后续可能被 prune/merge，单记 row_id 日后回查会"查无此行"。故确认时就把**当时的原文快照**进审计 new_val，留痕自包含、不依赖记忆表后续状态。
  - **零破坏返回值演进**：返回 `bool→Optional[str]`，但端点 `if not content` 对 True/非空串/None/False 行为一致，R15 端点测试（mock 回 True/False）无需改；仅 store 层 R15 断言从 `is True/False` 调整为 `== content / is None`。
  - **测试**：`tests/test_memory_source_confidence.py` 改 3 例（确认返回 content、非推断/缺失返回 None）；`tests/test_web_episodic_memory_api.py` 增 1 例（确认后 `audit.query(action="episodic_confirm_inferred")` 命中且 new_val 含原文）。**全量 5331 passed / 31 skipped / 0 failed**（约 226s）。无 lint。
  - 优化思考：① **content 快照入审计**——与"target=id"互补，行没了也能回查改的是什么；② **审计失败不阻断**——log 包 try/except，审计是旁路不是主路；③ **复用 ctx.audit_store**——不新建存储/表，挂既有 audit_log，与配置/模板/危机审计同一条时间线，运营一处可查；④ **action 命名对齐既有约定**（动词_对象：`episodic_confirm_inferred`），便于按 action 聚合统计。下一步候选：**R17 记忆校正质量看板**（按 `episodic_confirm_inferred` 审计聚合：各坐席确认量/AI 推断采纳率，反哺"AI 推断到底准不准"的度量）；或回收一直挂账的 **R1 arc_hint↔策略块去重合并**（纯清理省 token）。

- 2026-06-19 · **R17 记忆校正质量看板（闭合 R12→R16 度量环）✅**：把 R16 落库的确认审计变成"AI 推断准不准"的运营度量。
  - **选题依据**：R16 让确认动作留了痕，但只能查单条，答不了"近一个月坐席确认了多少推断、AI 推断采纳率多高、谁在校正"。R17 把审计聚合成看板，闭合 R12（产出推断）→R15/16（人工确认+留痕）→R17（质量度量）整条链路。
  - **方案**：① `EpisodicMemoryStore.inferred_counts()`——一条 `SUM(CASE WHEN tier='raw'…)` 出 `{pending（待确认 raw 推断）, total（任意 tier 的 ai_inferred）}`；② `SkillManager.episodic_inferred_counts` 薄包装；③ `GET /api/episodic-memory/correction-stats?days=30`——从 `ctx.audit_store.query(action="episodic_confirm_inferred", since=窗口)` 聚合采纳数/各坐席确认量/最近 10 条，叠加库内 pending，算**近似采纳率 = confirmed/(confirmed+pending)**；④ 复用既有「情景记忆」后台页顶部加 4 格统计卡 + 各坐席确认量 chips，`csLoad()` 独立 fetch（失败不阻断主表）。
  - **关键设计——采纳率诚实标注"近似"**：确认后事实翻成 user_stated 已移出 ai_inferred 集合，故分母无法直接拿"历史产出总量"。采用 `confirmed/(confirmed+pending)` 近似"已表态(采纳)占已表态+待表态"，并在 UI/接口注释明确标"近似"，不假装精确——与 R9d"作用域诚实"一脉相承。
  - **零侵入挂载**：复用 episodic 页（无新页/导航/权限）；只增 1 个 GET 路由（读权限 `_api_auth`，与列表同级）；看板 JS 失败静默不影响记忆主表。
  - **测试**：`tests/test_memory_source_confidence.py` 增 3 例（inferred_counts 空/pending vs total/确认后移出集合）；`tests/test_web_episodic_memory_api.py` 增 2 例（聚合采纳率·各坐席计数·only 本 action / 空看板）；route inventory 增 `/api/episodic-memory/correction-stats GET`。**全量 5345 passed / 31 skipped / 0 failed**（约 330s）。无 lint。
  - 优化思考：① **单条 SUM(CASE) 出双计数**——pending/total 一次扫描，不两趟 query；② **聚合在路由、计数在 store**——审计聚合天然属 web 层（持有 ctx.audit_store），库内计数属 store，各司其职不耦合；③ **采纳率近似且自陈**——宁可标"近似"也不造一个看似精确实则错位的分母；④ **看板与主表解耦加载**——两个独立 fetch，看板挂了记忆管理照常用。下一步候选：**R18 采纳率趋势/低采纳告警**（采纳率持续偏低=AI 推断在产噪声，接入 alert-status 提示调推断阈值）；或终于回收一直挂账的 **R1 arc_hint↔策略块去重合并**（纯清理省 token）。

- 2026-06-19 · **R18 校正趋势 + 低采纳告警（度量→告警→调参 自调节环）✅**：采纳率会画趋势、持续偏低会主动喊话。
  - **选题依据**：R17 把校正数据变成"一个数"，但①看不出走向（在变好还是变差）②没人会被提醒"AI 推断在产噪声、白占坐席精力"。R18 补上趋势可视 + 主动告警，闭合"度量→告警→调参（调 inferred_min_hits）→再度量"的自调节环。
  - **方案**：① 把 R17 端点内联聚合**抽成模块级 `build_correction_stats(audit_store, sm, days, recent_limit, with_trend)`**——供 correction-stats 端点与 alert-status 共用，杜绝两处聚合漂移；新增 `trend`（按 `ts[:10]` 日桶的每日确认量）与 `sample`(=confirmed+pending) 字段；② episodic 页加迷你柱状「每日确认量」趋势条；③ `alert-status` 第 4 类告警：`sample≥10 且 adoption_rate<0.30` 时挂 `memory_adoption` warn，文案直指"建议调高 memory.inferred_min_hits 或人工清理"，深链 `/episodic-memory`。
  - **关键设计——只画"每日确认量"而非"每日采纳率"**：采纳率分母含 pending 是**当下快照**，无法重建历史每天的 pending，硬画"每日采纳率"会是假数据。故趋势诚实地只画可由审计 ts 真实还原的**每日确认量**（校正活动量），采纳率仍只给当前值。延续 R17"近似自陈"的诚实原则。
  - **关键设计——告警双闸门防误报**：`sample≥10`（样本足够）**且** `rate<0.30`（确实偏低）才报，且仅 `warn` 不升 `critical`——这是质量信号非安全事件。小样本期/高采纳期均静默（`test_alert_silent_on_small_sample`/`_high_adoption` 锁死）。
  - **测试**：新建 `tests/test_memory_correction_alert.py` 6 例（helper 空/趋势+采纳率/no-trend 开关；告警低采纳触发/小样本静默/高采纳静默）。**全量 5351 passed / 31 skipped / 0 failed**（约 222s）。无 lint。
  - 优化思考：① **聚合抽公共函数**——端点与告警同一真值源，改聚合口径只改一处；② **趋势只还原可还原的**——宁缺每日采纳率，不造假分母；③ **告警双闸门 + 只 warn**——样本+幅度双条件防小样本误报，等级与危机/通道告警分层（安全 critical / 质量 warn）；④ **alert 块复用 helper 时关趋势+空 recent**（`with_trend=False, recent_limit=0`）——告警只需 sample/rate，省去无谓的 trend/recent 构造开销。下一步候选：**R1 arc_hint↔策略块去重合并**（挂账已久的纯清理，省 prompt token，且与情感链解耦、风险低）——建议下一阶段终于回收它，给 R 线记忆/校正主题做一次收尾；或 **R19 采纳告警阈值可配**（把 10/0.30 提到 config，避免硬编码）。

- 2026-06-19 · **R1 情感弧线 ↔ 应对策略去重（还技术债 / 省 prompt token）✅**：挂账已久的清理终于回收，给 R 线情感链收尾。
  - **选题依据**：`build_emotion_arc_hint`（【情感感知】块）与 `build_strategy_block`（【应对策略】块，R1-era 新增）对**同一情绪**反复叮嘱——负向都说"先共情/别急着讲道理"、低能量都说"陪着就好别急着帮忙解决"、正向都说"轻松聊"。每轮两块说重话，白耗 prompt token 还稀释指令。挂账多轮，借 R 线收尾一并清理。
  - **方案**：给 `build_emotion_arc_hint` 加 `strategy_active` 关键字参，并在 `build_emotional_context_block` 传 `enable_strategy`。策略**开启**时 arc 只保留**跨情绪转折**这一独有价值（"之前低落现在好多了，可以点出变化" / "上次开心现在不好，先问怎么了"——策略块从不叙述"变化"本身），把"当前情绪怎么接"全部让给策略块；策略**关闭**时回退完整指引，绝不丢情绪引导。
  - **关键设计——按"独有 vs 重叠"切分而非按情绪**：保留的判据不是"哪种情绪"，而是"这句是不是在讲**转折/变化**"——转折是 arc 独有（策略只看当前情绪选 validate/accompany/…，不叙述 prev→cur 的迁移），当前情绪应对是两块重叠区。故 improving / pos→neg 两个跨价位转折无条件保留，其余（首轮基础反应、负向恶化追加、平峰负向/低能量）在策略开时静默。
  - **安全兜底**：策略可被 config 关（`companion.empathy_strategy.enabled=false`）；关时 arc 完整指引原样回退，负向首轮等仍有引导，不因去重而裸奔。危机/反谄媚（R4/R8）走 wellbeing_guard 独立前置，不受本次改动影响。
  - **测试**：新建 `tests/test_emotion_arc_dedup.py` 11 例（策略开：首轮负向/低能量/平峰负向/负向恶化均静默，improving/pos→neg 转折保留；策略关：完整指引回退；默认参数=旧行为；集成层负向首轮不再 arc+策略双叮嘱、策略关时 arc 在）。**全量 5362 passed / 31 skipped / 0 failed**（约 335s）。无 lint。
  - 优化思考：① **关键字参 + 默认 False**——`strategy_active` 默认关=旧行为，既有直接调 `build_emotion_arc_hint` 的代码/测试零改动，演进零破坏；② **按职责切分块**——情感感知=讲"变化"，应对策略=讲"怎么接"，两块各司其职、语义正交，不只是省 token 更是 prompt 结构更清晰；③ **去重不去引导**——策略关的降级路径完整保留，宁可多保留也不让某条件下情绪引导裸奔。**至此 R 线"AI 推断→人工确认→留痕→度量→告警"记忆校正闭环 + 情感链去重收尾，整体告一段落。** 下一步候选：**R19 采纳告警阈值可配**（10/0.30 提 config）；或转入此前挂账的工程线 **N4 真账号验证 / N4b 镜像 chat_key 校准 / N5 登录注册统一**（需真 Telegram 账号联调）。

- 2026-06-19 · **R19 采纳告警阈值可配（封口 R 线，去硬编码）✅**：R18 的 `30天/10样本/0.30率` 提到 config，可调可关。
  - **选题依据**：R18 低采纳告警是"魔法数硬编码"在 `health_routes`——不同部署的推断质量/坐席规模不同，30 天窗口、10 样本闸门、30% 率未必普适，改阈值要动代码。R19 把三参 + 开关提到 `memory.adoption_alert`，给 R 线做去技术债的收尾。
  - **方案**：① `alert-status` 第 4 类告警从 `config_manager.config["memory"]["adoption_alert"]` 读 `enabled / window_days / min_sample / low_rate`（缺省 true/30/10/0.30，与 R18 行为一致）；② `window_days` 同时驱动 `build_correction_stats` 的统计窗口与文案"近 N 天"，口径自洽；③ `config.example.yaml` 新增文档化顶层 `memory.adoption_alert` 块（并注明其余 memory.* 子项走代码默认）。
  - **关键设计——缺省即 R18 原值**：所有键 `.get(key, R18默认)`，未配置的既有部署行为**逐字不变**；只有显式配置才改变阈值或关闭，零迁移成本。
  - **健壮性**：`config_manager.config` 取不到/非 dict 时 `_acfg={}` 全走默认；窗口 clamp 1-365、样本 ≥1，防误配出界。
  - **测试**：`tests/test_memory_correction_alert.py` 增 3 例（`enabled:false` 静默、`low_rate:0.10` 收紧不报、`min_sample:5` 放宽小样本即报）。**全量 5379 passed / 31 skipped / 0 failed**（约 379s）。无 lint。
  - 优化思考：① **缺省=旧值的"无声演进"**——可配化最忌改默认行为，三参全部回落 R18 原值，存量部署无感；② **window 单一真值驱动统计+文案**——窗口只在一处定义，喂给 helper 也喂给提示文本，杜绝"统计 30 天文案却写 7 天"的口径漂移；③ **文档块自带闸门语义注释**——example 里写明 min_sample 是"防小样本误报"、low_rate 是"0-1"，运营改配不必翻代码。**R 线（记忆校正闭环 R12-R18 + 情感链去重 R1 + 阈值可配 R19）至此完整封口。** 下一步：建议转入挂账已久的**工程线 N4/N4b/N5**（真 Telegram 账号联调：N4 统一运行时真账号验证 / N4b 镜像 chat_key 校准 / N5 phone+code 与 QR 登录注册统一）——需你提供真实账号环境方可推进；在此之前可先做 **N5 登录注册统一的纯逻辑骨架**（不依赖真账号、可单测先行）。

- 2026-06-19 · **N5 登录注册统一（纯逻辑骨架，不依赖真账号）✅**：A 线 config 账号（phone+code）并入 B 线持久注册表，与 QR 扫码登录共用一张表。
  - **选题依据**：A/B 线融合后仍存"两张账号表各管一摊"的裂缝——A 线 `TelegramAccountRegistry`（config 驱动、只读、`telegram.accounts`）的 phone+code 账号**只活在内存配置里**，从不落 `platform_accounts`；B 线 QR/protocol 登录则 upsert 进持久 DB（meta 存 session_string）。结果：编排器重启恢复 / 舰队健康视图等**任何 DB 驱动的视图都看不见 config 账号**。N5 先做这条统一的**纯逻辑核（对账/合并）**，不碰真账号联调（留 N4）。
  - **方案**：`TelegramAccountRegistry.sync_to_account_registry(registry)`——遍历自身 context，幂等 upsert 进 B 线 `AccountRegistry`：新账号写 `mode=protocol/status=pending`；既有账号**只刷新 config 拥有的静态属性**（label/proxy_id），mode/status/会话凭据原样保留。main.py 启动时按 `telegram.unify_login_registry`（默认关）best-effort 调用。
  - **关键设计——不破坏既有 QR 登录态（三不碰）**：① **不覆盖会话凭据**——meta 走"读出既有→叠加 config 静态字段"的合并，绝不丢 QR 写入的 `session_string`；② **不打翻在线态**——已 online 的号同步后仍 online（不回退 pending）；③ **不改登录模式**——既有 mode 保留。三者由 `upsert` 的"仅覆盖显式非 None 字段" + meta 手动合并共同保证（`test_sync_preserves_qr_session_and_online` 锁死）。
  - **关键设计——config 为静态属性单一源**：label/proxy_id 以 config 为准刷新（含**清空**：config 删了 proxy 则同步清空），因为这两项本就由配置管理；运行态（status/last_online/session）则归 DB。职责边界清晰。
  - **默认关 + best-effort**：新子系统遵循 repo 约定 `unify_login_registry: false`；同步失败仅告警不阻断启动；`registry=None` 安全返回空。
  - **测试**：新建 `tests/test_n5_login_unification.py` 7 例（新账号写入字段全 / 幂等不重复 / 保留 QR session+online+mode / config 刷新 proxy / config 清空 proxy / default 取舍 / None 兜底）。**全量 5390 passed / 31 skipped / 0 failed**（约 284s）。无 lint。
  - 优化思考：① **对账逻辑与真账号联调解耦**——N5 只做"两表合一"的确定性合并，纯函数可单测，把需真账号的 N4 端到端验证留后；② **合并而非替换 meta**——统一最大的坑是"同步把 QR 凭据洗了"，故 meta 显式读-改-写、三不碰登录态，宁可少改也不误删；③ **静态/运行态按 owner 切分**——config 拥有的属性以 config 为准（含清空），DB 拥有的运行态不动，避免双写打架；④ **默认关**——存量纯 A 线部署不会凭空多出 account_registry.db。下一步：N5 逻辑核已就绪，**N4 统一运行时真账号验证 / N4b 镜像 chat_key 校准**需你提供真实 Telegram 账号环境联调；在此之前可继续情感陪聊体验类新主题（如主动话题发起、语音陪伴），或回收其它挂账项。

## 8. 情感陪聊体验线（P 线）

- 2026-06-19 · **P1 主动话题发起（确定性选择器，复用 R 线记忆置信）✅**：陪伴型 AI 的差异化能力——沉默后不是干巴巴"好久没聊"，而是回到对方真正在意的事。
  - **选题依据**：现有 reactivation 栈（`ReactivationScheduler` + `reunion_prompts`）是 contacts/journey(RPA) 线，且**刻意"不接续上次话题"**（冷启动重连场景）；而 Telegram 陪伴线手握 R12-R15 的高置信记忆，正该做相反的事：**记忆驱动的个性化开场**（"上次你说在备考，结果怎么样？"）。这是陪护核心差异化，且 0 复用冲突（两条线场景相反）。
  - **方案**：`src/utils/proactive_topic.py` 确定性纯函数（对齐 `empathy_strategy` 的"选择→注入一行指令"范式）：`select_proactive_topic(memory_facts, silent_hours, stage, intimacy)` → `{mode, fact, directive, long_absence}`；`build_proactive_topic_block` 包成【主动话题】prompt 块；`SkillManager.build_proactive_opener(memory_key, …)` 从 episodic `list_rows` 喂事实。三模式：`follow_up`（回访高置信记忆）/ `gentle_checkin`（无钩子温和问候）/ `""`（沉默不足不打扰）。
  - **关键设计——只回访高置信事实，把 R 线投资变现**：**绝不拿 `ai_inferred` 的猜测去主动回访**（"你不是在创业吗？"猜错一开口就尴尬、反噬信任），只回访 `user_stated`/已确认 + 非 stale 的事实。这让 R12（来源置信）/R15（人工确认）/R10-11（矛盾消解）的工作直接转化为"主动开场敢不敢提"的判据——记忆质量越高，主动话题越准。
  - **关键设计——纯选择器，发送/调度解耦**：本模块只决定"该开场时说什么"，"何时发"留给调度层（可复用 `ReactivationScheduler` 的沉默/cooldown 候选逻辑），"真实文案"留给回复生成层（注入 directive）。三层解耦，纯函数零 IO 可单测，无需真账号。
  - **排序与克制**：候选按 稳定层 > 复发数 > 新鲜度 排序选最值得回访的一条；长别离（>14天）先柔和重连再提旧事；新关系（initial/warming）追加"点到为止别越界"修饰；沉默 <24h 不打扰活跃用户。
  - **测试**：新建 `tests/test_proactive_topic.py` 20 例（沉默闸门/排除 ai_inferred·stale/优先 user_stated/稳定·复发·新鲜排序/无钩子退化/长别离柔和/新关系克制/block 装配/SkillManager 接线）。**全量 5420 passed / 31 skipped / 0 failed**（约 228s）。无 lint。
  - 优化思考：① **复用记忆置信而非新造信号**——主动话题"敢提什么"直接读 R12/R15 的 source/tier，不另立一套可信度，R 线投资一处建多处用；② **与 reunion 场景互补不打架**——journey 线"不接续话题"做冷重连，陪伴线"接续记忆"做暖回访，同名能力按场景分流；③ **选择器/调度/文案三层解耦**——P1 只做确定性选择（可测），发送时机复用既有 scheduler、文案交回复层，不重造轮子也不耦合发送副作用；④ **默认安全降级**——无记忆→温和问候、沉默不足→不开场、异常→空块，绝不冒失打扰。下一步候选：**P2 主动话题调度接线**（把 P1 选择器接到沉默检测+cooldown，复用 ReactivationScheduler 候选，真正触发 Telegram 主动消息——需真账号验证端到端）；或 **P1b 把 follow_up 的 fact 同时喂给 episodic 检索**（让回复生成既有"提什么"指令也有该记忆全文上下文）。

## 7. 关系健康·留存看板（替代错误的 M6 解决率）

**为什么换**：客服「解决率/再联系率」=快速结案、用户别再来；情感陪聊正相反——要聊得深、黏、用户主动回来。旧 `recontact_rate`（72h 回来=坏）在陪聊里恰恰是**留存(好事)**，指标方向反了，故重构。

**已交付（以 messages 为真实信号源，非 AI/人工拆分）**：
- `store.get_engagement_stats(since, until)`：`active_relationships`（有用户入站的关系）、`messages_in/out`、`avg_turns`（关系深度）、`reciprocity`（应答充分度）、`sticky_relationships`/`sticky_rate`（跨天回访黏性）、逐日 trend。
- `store.get_retention_cohorts(since, until, horizons=(1,7,30))`：以「首次入站落窗口内」为同期群，算 D1/D7/D30 回访率——**陪聊真正的"解决率"**。
- `build_roi_summary` 的 `resolution` 段 → 换为 `relationship` 段；`ops_intel.build_ops_report` 同步；`workspace_roi.html` 解决率/再联系卡 → 换为「活跃关系/黏性/人均轮次/D7 留存」卡。
- 测试 `tests/test_engagement_stats.py`（6 例，替代已删的 test_resolution_stats）；engagement+ops+stage1+route inventory 共 **168 passed**；无 lint。

**下一步（情感陪聊路线候选，待排期）**：
- 关系看板纳入**亲密度分布与递进**（数据在 contacts/journeys：`intimacy_score`/`funnel_stage`，需跨 store 聚合）。
- 情绪深度升级（词典规则 → LLM 级共情）。
- 关系阶段自动推进编排；主动陪伴个性化增强；M7 反封号接通 runner+健康灯。

---

## 8. 平台登录 × 全自动陪聊 能力盘点（2026-06-17 代码实况）

> 两个只读探查代理逐文件 grep/读码得出，**以代码为准**。回答用户核心问题：「能扫码登录个人号全自动聊天了吗？」

### 8.1 关键认知：系统里有**两条互不打通的技术路线**

| 路线 | 平台 | 是否"扫码即用" | 是否需手机 | 全自动聊天成熟度 | 封号风险 |
|---|---|---|---|---|---|
| **协议栈**（server 直连 MTProto/Web 协议） | Telegram(pyrogram)、WhatsApp(Baileys) | ✅ 扫码 | ❌ 无需手机，云端多开 | 🟡 雏形/待联调 | 较高 |
| **RPA**（安卓真机自动化 ADB/无障碍） | LINE、Messenger、WhatsApp | ❌ **不扫码**（需先在手机登好号） | ✅ 需真机农场 | 🟢 较成熟（Messenger 最强） | 较低（更拟真） |

**用户愿景「扫码登录个人号 → 全自动陪聊」= 押注协议栈路线**，但它恰恰最不成熟。RPA 全自动聊天最成熟，却**不走扫码、要真机+人工先登号**。

### 8.2 逐平台进度

| 平台 | 登录方式 | 扫码登录 | 全自动陪聊链路 | 一句话 |
|---|---|---|---|---|
| **Telegram** | 协议(pyrogram) | 🟡 `TelegramQrLogin`/`ExportLoginToken` + Web 弹窗已写，但 **3 道闸门默认全关、未真号联调** | A线(配置+session+手机号)**★★★★ 直发可用**；B线(扫码→编排)**★★ 雏形**且上下文贫瘠 | 最接近愿景，卡在 feature flag + 真号联调 + 协议线上下文注入弱 |
| **Messenger** | RPA(需手机已登录+手动选号) | ❌ 无 | **★★★★ 三平台最成熟**（12k 行 runner+Vision+多账号 AccountPool+通知触发+人设/亲密度/风控/审批投递） | 全自动陪聊最强，但登录靠人工真机 |
| **LINE** | RPA(需手机已登录) | ❌ 无 | **★★★ 可用**（测试最多；默认单聊，多会话 MVP；单聊路径 `reply_mode` 漏洞=即使 approve 也直发） | 稳，靠真机已登录 |
| **WhatsApp** | RPA + Baileys(分裂) | 🟡 Baileys 网页扫码存在但**未接 RPA**、默认关 | RPA **★★ 部分**（MIUI 适配占大量代码、**零 RPA 测试**、approve 投递链未闭环） | 扫码(协议)与自动聊(RPA)是两套没桥接的东西 |

### 8.3 Telegram「扫码→全自动陪聊」端到端缺口（B 线，命门清单）

链路已串通但默认走不通，缺口（详见探查报告 8cf4f1f8）：
1. **三道闸门默认全关**：`platform_login.telegram.protocol_enabled` + `platform_login.orchestrator_enabled` + `protocol_autoreply.enabled`（且每账号 `meta.auto_reply`）。
2. **QR 登录未真号联调**：`ExportLoginToken` + DC 迁移 `LoginTokenMigrateTo` 仅有 pending 单测，无真号 E2E。
3. **协议线陪聊上下文贫瘠**：`protocol_autoreply.build_reply_hook` 只传 4 个 context 字段（chat_id/channel/platform/account_persona_id），**未注入** episodic_memory / emotional_context / 亲密度 / contacts 画像——扫码号"会说话但没灵魂"，远弱于 A 线 `telegram_client` 与 RPA。
4. **A/B 双轨割裂**：扫码号进 B 线 orchestrator，`main.py` 的 A 线 `TelegramClient` 独立启动、不接管扫码 session。
5. **协议 worker 无消息过滤**：对群/频道也尝试 autoreply（缺 A 线 `filters.private` 四层触发）。
6. **收件箱 auto-draft 与 protocol autoreply 并行**：开 autoreply 时仍可能产草稿噪声。
7. **`config.example.yaml` 缺 `platform_login`/`protocol_autoreply` 段**：新部署易漏配。
8. **反封号未接协议 worker**：M7 的 `account_health`/`warmup` 与协议线 send 未连。

---

## 9. N 线 · A/B 双线收敛方案（两条都打通 · 零重复造轮子）

> **方向（用户 2026-06-17 拍板）**：① 协议栈优先 ② Telegram 首发 ③ 云端多开 ④ 反封号平衡。
> **新增约束（用户）**：**A 线（配置/session 登录，已有记忆人设）和 B 线（扫码登录，已有独立代理）两条都要打通，不要重复造轮子。**
> **N 线目标**：把 A/B 两条 Telegram 线**收敛到同一套共享内核**——同一个回复大脑、同一套账号/代理、同一道反封号闸门、同一份登录注册表；A 线保留"配置/session 登录"、B 线保留"扫码登录"，但**运行时与陪聊能力完全一致**。

### 9.0 重复造轮子的根因（代码实锤）

当前 A 线 `src/client/telegram_client.py` 与 B 线 `src/integrations/account_orchestrator.py::TelegramProtocolWorker` **各自维护一套**，这才是重复：

| 能力 | A 线 `TelegramClient` | B 线 `TelegramProtocolWorker` | 重复/缺口 |
|---|---|---|---|
| pyrogram Client | 自建（`initialize` L302），**phone+code 登录，无 proxy** | 自建（`start` L418），**session 登录，有 proxy**（`_proxy()`→proxy_pool） | **两套 client** |
| 入站处理 | `_setup_handlers`：`filters.private` + 四层触发 | `_wire_inbound`：**无 filter**（群/频道也回） | **两套入站** |
| 回复上下文 | **丰富**：persona/记忆/情绪/recent_bot_messages | **空壳**：仅 4 字段（chat_id/channel/platform/persona_id） | **两套回复路径** |
| 账号注册表 | `telegram_account_registry`（config 静态，无 proxy 字段） | `account_registry`（SQLite 动态，扫码，有 proxy_id） | **两套注册表** |
| 反封号 M7 | 未接 | proxy 有；warmup/health 未接 | 两边都缺 warmup 接线 |
| 登录 | phone+`code.txt`（手工） | QR/`ExportLoginToken`（Web） | 两种入口（保留，但应归一注册表） |

**收敛原则**：不是"各补各的另一半"，而是**抽出 5 个共享内核，两条线都消费同一套**。

### 9.1 五个共享内核（每个消灭一处重复）

| 核心 | 做什么（复用现成，不新造） | 消灭的重复 |
|---|---|---|
| **核心1 · 统一回复大脑** | 把 A 线 `_process_message` 的上下文装配（recent_bot_messages / user_emotion_hint / persona_ids / contact_id / intimacy / 记忆）抽成共享件 `build_companion_context(...)`；A 线 `_process_message` 与 B 线 `protocol_autoreply.build_reply_hook` **都调它** | 两套回复路径 → 一套（B 线立刻"有灵魂"，改一处两边生效） |
| **核心2 · 统一账号+代理** | 给 A 线 `TelegramClient` 加 `proxy=`（**复用 B 线已有 `_to_pyrogram_proxy` + `proxy_pool`**，不写新代理）；`account_cfg()` 增 `proxy_id` 字段；两套注册表桥接到统一读取 | 两套代理逻辑 → 一套；A 线补上反封号命门 |
| **核心3 · 统一反封号闸门** | A 线 send（`_send_reply`）与 B 线 send 都过同一道 M7 `effective_cap`/`account_health` 闸门（**M7 纯函数已写好，只接线**） | 两边各缺 warmup → 一道闸门 |
| **核心4 · 统一登录注册表** | phone+code 登录 与 QR 登录**都写入同一份账号注册表**（session 落库 + 元数据平权）；A 线首登做 Web 验证码输入框（去掉 `code.txt`） | 两套注册表 → 一份事实源；"配置号/扫码号"下游一视同仁 |
| **核心5 · 统一运行时（终态）** | B 线 orchestrator 的 worker **改为包一层复用 A 线 `TelegramClient`** 的 worker（它已有丰富入站+四层触发+回复）；proxy 经 `account_cfg` 传入 | 两套 client+入站 → **一个 client 类，两种拉起方式**（config 拉 / 扫码 session 拉） |

### 9.2 阶段总览

| 阶段 | 任务 | 真号依赖 | 优先级 | 状态 |
|---|---|---|---|---|
| **N0** | 现状对齐 + 收敛方案（本节） | — | — | ✅ 完成 |
| **N1** | 核心1 统一回复大脑（A/B 共用上下文装配） | ❌ 可单测 | P0 | ✅ 完成 |
| **N2** | 核心2 统一账号+代理（A 线加 proxy，复用 B 线 helper） | ❌ 可单测 | P0 | ✅ 完成 |
| **N3** | 核心3 统一反封号闸门（M7 接 A/B 两线 send） | ❌ 可单测 | P0 | ✅ 完成 |
| **N4** | 核心5 统一运行时（扫码 session 跑 A 线 client） | ✅ 需真号 | P1 | 🟡 骨架完成（含 N4b 收件箱镜像；代码+mock 单测全绿，待真号联调） |
| **N5** | 核心4 统一登录注册表（phone+QR 归一 + A 线 Web 验证码） | ✅ 需真号 | P1 | ⬜ |
| **N6** | 云端多开运维（编排自愈 + 账号生命周期 + 批量扫码 UX） | 部分 | P1 | 🟡 部分（机群健康灯+生命周期+信号源+前端看板已落，编排自愈/批量扫码 UX 待真号/前端） |
| **N7** | 陪聊深化（亲密度/四阶段/沉默召回 + 关系看板亲密度维度） | ❌ | P2 | ⬜ |
| **N8** | 复制 WhatsApp Baileys（扫码→统一回复大脑）+ RPA 补充（修 LINE/WA 漏洞） | 部分 | P3 | ⬜ |

> **可立即开工（不依赖真号、纯代码可单测）**：N1 + N2 + N3 —— 三个共享内核搭好后，A 线和 B 线**自动同时获得**完整陪聊 + 代理 + 反封号；真号联调（N4/N5）只是把"扫码 session 接到统一 client"收口。

### 9.3 N1 详细（核心1，先做，最高价值）

> 目标：抽出共享"回复大脑"，让扫码号（B 线）和配置号（A 线）用**同一套**人设/记忆/情绪逻辑。

- 新建共享件（如 `src/client/companion_context.py`）：`build_companion_context(platform, account_id, chat_id, text, *, history_provider, persona_ids, contact_lookup) -> dict`，产出 `recent_bot_messages` / `user_emotion_hint` / `account_persona_id(s)` / `contact_id` / `intimacy_score` / 记忆片段。
- A 线 `_process_message` 改为调用它（行为不变，等价重构 + 回归保证）。
- B 线 `protocol_autoreply.build_reply_hook` 改为调用它（从 4 字段 → 完整 context）。
- 单测：同一输入两条线得到等价 context；B 线回复带人设口吻 + 记忆。
- **验收**：B 线（扫码号）与 A 线（配置号）回复质量一致；改人设/记忆逻辑一处生效两边。

### 9.4 N2/N3 详细（核心2+3，与 N1 并行，纯代码）

- **N2**：`account_cfg()` 增 `proxy_id`；A 线 `initialize()` 的 `Client(...)` 注入 `proxy=_to_pyrogram_proxy(proxy_pool.get(proxy_id))`（直接 import B 线现成 helper）；两套注册表提供统一读取函数。单测：配置号带 proxy_id 时 Client kwargs 含 proxy。
- **N3**：A 线 `_send_reply` 与 B 线 send 前置同一道 `effective_cap`/`account_health` 检查；超限转人工/延后；`fleet_health` 汇两条线账号 → 接 ops 看板。单测：超额号被拦、健康灯聚合。

### 9.5 N4-N8 要点

- **N4 统一运行时**：`ensure_builtin_workers` 的 telegram worker 工厂改为返回"包 `TelegramClient`（account_cfg 带 session_name+proxy_id）"的 worker，替换 bare `TelegramProtocolWorker`；扫码 session → 直接跑 A 线丰富 client。真号联调 QR+DC 迁移。
- **N5 登录归一**：QR provider 与 phone+code provider 都 `upsert` 到统一注册表；A 线首登做 Web 验证码输入（替 `code.txt`）。补 `config.example.yaml` 的 `platform_login`/`protocol_autoreply` 段。
- **N6 云端多开**：orchestrator 监督/自愈/重连已具备；补账号生命周期状态机（扫码/配置→预热→活跃→受限/封禁→下线）可视化 + 批量扫码 UX + 多号 persona 分配。
- **N7 陪聊深化**：统一回复大脑里接亲密度引擎 + `companion_relationship` 四阶段 + `reactivation_loop` 沉默召回；关系看板纳入亲密度分布（承接 §7）。
- **N8 横向**：WhatsApp Baileys 扫码会话接统一回复大脑（核心1 复用）；RPA 修 LINE 单聊 `reply_mode` 漏洞 + WA `run_pending_deliveries` 审批投递闭环。

### 9.6 待你后续确认（不阻塞 N1-N3 开工）

- 测试用 Telegram 号 / api_id / api_hash 由谁提供（N4/N5 真号联调需要）。
- "全自动"是否要**高风险消息兜底转人工**（已有 `keyword_risk_level` 拦截，确认默认行为）。
- "**无代理不上线**"是否设为硬策略（核心2 可做成开关）。
