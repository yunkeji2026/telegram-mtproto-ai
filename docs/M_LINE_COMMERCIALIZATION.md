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

### M7 反封号 v1（守命门）
账号预热状态机 + 1账号1代理 + 每日配额/随机延迟 + 账号健康红绿灯，接 D/E 线 health 看板。（设计待 M6 完成后细化回写。）

### M8 结构化转人工
转人工携带 intent+历史+客户画像（`conversation_meta` 已有 last_intent/summary/csat），接 contacts/handoff。

### M5 首跑向导
串 `setup`/`golive`/`kb-start` 成一条龙首跑引导。

## 4. 当前状态（每次更新）

- **现在在做**：Phase 1 · M7 反封号 v1（M6 已完成）。
- **下一步**：M7 → 测试 → 回写 → M8 → M5。

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
