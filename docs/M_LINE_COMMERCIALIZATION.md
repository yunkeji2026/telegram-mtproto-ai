# M 线 · 商业化开发文档（持久记忆）

> 本文件是 M 线（Monetization / 快速商业化）的**单一事实来源与工作记忆**。
> 重入/失忆时先读本文件「当前状态」与「下一步」两节，再继续。
> 每完成一个阶段：跑测试 → 把结果与进度回写「执行日志」→ 自动进入下一阶段。
> 项目铁律：**文档可能落后于代码，动手前先 grep/读码验证实况**（见 CLAUDE.md）。

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

- **现在在做**：✅ **全部完成**。Phase 0-2 全部交付/审计；全量回归连跑 2 次全绿。
- **下一步（后续迭代候选，非本轮）**：见 §6 各 M 项「下一步可优化」聚合。

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
