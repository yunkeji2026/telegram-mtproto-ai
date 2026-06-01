# admin.py 拆分盘点（Phase E1 中段复盘）

更新日期：2026-06-01（G1 完成后回写）
对应阶段：[`AI跨境电商客服平台_升级开发文档_v2_落地优化版.md`](AI跨境电商客服平台_升级开发文档_v2_落地优化版.md) §3 Phase E1
现状：admin.py **3905 行**（拆分前 6819，已 -42.7%）；已抽出 129 端点到 11 个路由模块。

> 进度更新（盘点后）：
> - **G1 KB 收尾 ✅ 完成**（批 5K + 5L，共抽 21 端点）：KB 全栈端点已从 admin.py 清零，
>   仅剩 `/api/kb/import` 重复注册 bug 对（遗留，待产品决策，见 §6）。
> - admin.py：盘点时 4350 → **现 3905**（G1 期间 -445 行）。
> - 下一步：**G2 监控/报表**（新建 monitoring_routes，预计首次给 AdminRouteContext 加 event_tracker 字段）。

---

## 0. 为什么做这次盘点

E1 已完成 KB 全栈 + learner + auth/strategy/settings/human_escalation 的模块化。剩余
~95 个内联路由里，**越往后越混杂"该抽的业务组"与"该留的骨架页"**。继续盲抽边际收益
递减且可能拆散内聚核心。本盘点给出：分组清单 + 估行 + 该抽/该留 + 重定目标。

---

## 1. 骨架区（**保留**在 admin.py，不可外迁）

| 区域 | 行 | 说明 |
|---|---|---|
| 模块导入 + 模块级 helper | 1–96 | templates、`_human_escalation_cfg_hash`、`_schedule_status_cache_*`、`invalidate_schedule_status_cache` |
| `create_app` 装配 | 97–273 | config 读取、CORS/Session 中间件、StaticFiles |
| 请求体限流中间件 | 273–360 | `_audit_oversize`/`_413_response` + middleware |
| 共享闭包 | 360–650 | `_broadcast_config_reload`/`_enrich_context`/`_auto_snapshot`/ 鉴权 5 件套/`_admin_ctx` 构造 |
| 各 register_*_routes 调用 | 散布 | auth_user/he/persona/strategy/settings/kb/kb_ai/learner/domain/line/fb/rpa×4/unified/voice/telegram |
| 后台任务 startup/shutdown | 2797/2811 | 通用启动 `app.state.kb_ai_loops` + weekly_report + watcher |
| 共享闭包（跨组） | 散布 | `_fire_webhook`、`_get_intent_display_names`、`_weekly_report_loop`、`_enrich_context` |
| `return app` + `_register_domain_routes` | 4325/4328 | 工厂收尾 + 域插件挂载 |

> 骨架合计约 **1200–1500 行**，这是 admin.py 的**合理终态下限**。

---

## 2. 剩余内联路由分组（**该抽**）

按内聚度 + 低骨架耦合排序，建议后续批次：

### G1. KB 收尾 ✅ **已完成**（批 5K + 5L，21 端点 → `kb_routes.py`）
已抽出：`health-stats`、`miss-log` POST/DELETE、`translations/pending|confirm|PUT`、
`entries/{id}/images`×3、`seed`、`maintenance-advice`（批 5K，12 个）；
`report`、`kb-images/{filename}`、`ai-generate`、`export-markdown`、`stats`、
`entries/{id}/translate`、`sandbox/ai-reply`、`auto-suggestions`、`reply-quality`（批 5L，9 个）。
- 全部逐行一致迁移；自带 AI 的 ai-generate/sandbox-ai-reply 保留 inline httpx。
- **唯一遗留**：`/api/kb/import`+`/import/save`（3978/4001 区）**未动**——`import` 与 kb_routes
  的 export-dump import 同 path（文档导入版被遮蔽），属遗留 bug，需产品决策（见 §6），不混入重构。

### G2. 监控/报表（~15 端点）→ 新建 `monitoring_routes.py`
`system-info`(668)、`vision-stats`(731)、`bot-metrics`(772)、`activity-stats`(1686)、
`health-check`(1760)、`notifications`(1880)、`snapshots`(2010)、`alert-status`(2033)、
`trigger-decisions`(2116)、`session-stats`(1366)、`report/daily|weekly`(3566/3730)、
`audit/activity`(884)、`ai/quality`(2337)、`config/summary`(2318)、`events`(595)。
- 多为只读聚合；依赖 metrics_store / event_tracker / audit_store / telegram_client。
- 需评估是否扩 ctx（event_tracker）。

### G3. 客服工作台（~8 端点）→ 新建 `cases_routes.py`
`chat/test`(2882)、`chat/test/correct`(3132)、`cases/active`(3056)、`cases/{id}/note|close`(3093/3111)、
`copilot/query`(3186)、`conversations/active`(3879)、`users/at-risk`(3849)。
- 含 `_get_test_session`(2866)、`_copilot_get_ctx_store`(3178) 局部 helper，随组迁移。

### G4. episodic-memory（~5 端点）→ 新建 `episodic_routes.py`
`/episodic_memory`(4049)、`/episodic-memory` 页(4053)、`api/episodic-memory`(4058)、DELETE(4069)、backfill(4079)。
- 依赖 `app.state.episodic_memory_store`。

### G5. identity（3 端点）→ `cases_routes.py` 或独立
`api/identity`(4116)、`identity/link`(4129)、`identity/unlink`(4145)。含 `_get_cpi`(4111) helper。

### G6. strategy/autopilot 收尾（~5 端点）→ 并入 `strategy_routes.py`
`data-purge`(1347)、`apply-param-suggestion`(1374)、`export-strategy-events`(1404)、
`autopilot-status`(1438)、`autopilot`(1448)。

### G7. 模板/迁移/webhook（~8 端点）→ 并入 `settings_routes.py`
`templates` GET/PUT(2222/2226)、`batch-strategies`(2247)、`batch-templates`(2277)、`migrate`(2330)、
`webhook-settings` GET/PUT(1982/1988)、`webhook-test`(2002)、`rollback`(1648)。

### G8. reactivation dry-run（2 端点）→ 监控或 contacts 相关
`reactivation/dry-run-samples`(809)、`dry-run-feedback`(835)。

---

## 3. 该留（**保留**为骨架/首页/工具页）

这些是薄页面渲染或核心工具，留在 admin.py 更合理（强行外迁反而拆散首页体验）：

`/`(1018 dashboard 首页)、`/set_lang`(506)、`/set_ui_mode`(512)、`/help`(1573)、`/training`(1579)、
`/developer`×3(913/933/947)、`/health`(2353 健康探针)、`/export`(2406)、`/import` 页+post(2423/2440)、
各纯页面渲染（`/analytics`/`/cases`/`/logs`+stream/`/audit`+export/`/diff`/`/templates`/`/personas`/
`/ai-studio`/`/learner`/`/episodic_memory` 页）——**页面壳可留，其 API 外迁**。

> 取舍原则：**API（数据）外迁到主题模块；薄页面壳（仅 templates.TemplateResponse）可留**，
> 因为页面壳依赖 `_enrich_context` + 大量模板变量，外迁性价比低。

---

## 4. 重定 E1 终态目标

**放弃机械的 < 1500 行**，改为**结构性终态**：

> admin.py = **app 工厂 + 中间件 + 鉴权闭包 + `_admin_ctx` + 全部 register_* 调用 +
> startup/shutdown + 薄页面壳**；所有**业务 API 按主题进独立路由模块**。

预计终态 admin.py **~2000–2500 行**（骨架 1200–1500 + 薄页面壳 ~600–900）。
G1–G8 全部抽完可再减 ~1500–1800 行业务 API。

---

## 5. 建议批次顺序（低风险 → 高价值）

1. ~~**G1 KB 收尾**~~ ✅ **已完成**（批 5K+5L，21 端点；`import` bug 已标注不动）。
2. **G2 监控/报表**（← 当前下一步）：只读聚合、内聚，新建 monitoring_routes（评估 event_tracker 是否入 ctx）。
3. **G3+G5 客服工作台 + identity**：cases_routes，含 `_get_test_session`/`_copilot_get_ctx_store`/`_get_cpi` 随迁。
4. **G6 strategy 收尾 / G7 settings 收尾**：并入已有模块。
5. **G4 episodic / G8 reactivation**：小组收尾。

每批沿用既验证流程：**读真实 body + grep 共享依赖 + 三重回归（快照网精确相等 + 重复检测 + 全量）**。

---

## 6. 改进点（盘点得出）

- **ctx 可能需扩 1–2 字段**：`event_tracker`（监控组）、`episodic_memory_store`（episodic 组）——按"用到才加可选字段"原则，不预先膨胀。
- **页面壳 vs API 分离**：明确"数据 API 外迁、薄页面壳留"的判定，避免过度拆分首页。
- **`/api/kb/import` 遗留 bug**：建议单独立项（改名 `import-document` + 前端协同），不混入重构批。
- **快照网已是精确相等**：后续批次能继续自动拦截端点丢失/重复，安全网到位。

---

*本盘点对应 2026-06-01 admin.py（4350 行）。后续每抽一批，更新 §2 清单与行数。*
