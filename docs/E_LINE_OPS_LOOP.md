# E 线 · 运营闭环（Operations Loop）

> 把 C 线（商业化：计费/用量）+ D 线（可观测性：健康/可靠性）+ P 线（ROI）的既有能力
> 串成一个**端到端运营闭环**：一页总览 → 异常落表为事件 → 确认/指派 → 恢复自动关闭 → 过期清理。
>
> **本文以代码为准**（参见 CLAUDE.md「文档落后于代码」教训）。涉及文件均在 `feat-e-line-ops-loop` 分支。

## 1. 一句话

`HealthWatchdog` 周期巡检健康与计费 → 异常写入 `ops_incidents` 表并经 EventBus（D3 通道）外发 →
主管在 `/admin/ops` 运营总览页确认/指派 → 恢复时自动 resolve → 超保留期自动清理。

## 2. 组成（E1–E4）

| 子项 | 能力 | 关键文件 |
|------|------|----------|
| **E1 运营总览** | ROI/计费/健康/可靠性 聚成「老板单页」+ 趋势 sparkline | `src/utils/ops_overview.py`、`src/web/routes/ops_overview_routes.py`、`src/web/templates/ops_overview.html` |
| **E2 事件闭环** | health_alert 落表为可 ack/指派的运维事件 | `src/inbox/store.py`（`ops_incidents`）、`src/inbox/health_watchdog.py` |
| **E3 计费异常** | 超席位/超额 → `billing_alert` 事件 + 计费类事件，走 D3 通道 | `src/utils/ops_overview.py`（`billing_anomalies`）、`src/inbox/health_watchdog.py`、`src/inbox/webhook_notifier.py` |
| **E4 收敛** | 路由基线、单测、全量回归 | `tests/test_ops_overview.py`、`tests/test_ops_incidents.py`、`tests/test_admin_route_inventory.py` |

## 3. 数据流

```mermaid
flowchart TB
  WD["HealthWatchdog._tick (周期)"]
  WD -->|collect_health| HC["健康红绿灯"]
  WD -->|compute_statement + billing_anomalies| BC["计费异常"]
  HC -->|red/yellow 变化| OPEN_H["store.open_or_update_incident(kind=health)"]
  BC -->|有异常| OPEN_B["store.open_or_update_incident(kind=billing)"]
  HC --> EB["EventBus.publish(health_alert)"]
  BC --> EBB["EventBus.publish(billing_alert)"]
  EB --> WN["WebhookNotifier → Telegram/WA/Messenger/JSON"]
  EB --> SSE["SSE / 通知队列（前端告警铃）"]
  EBB --> WN
  EBB --> SSE
  OPEN_H --> TBL[("ops_incidents")]
  OPEN_B --> TBL
  PAGE["/admin/ops 运营总览页"] -->|GET /api/admin/ops-overview| AGG["assemble_ops_overview"]
  PAGE -->|GET /api/admin/incidents| TBL
  PAGE -->|POST ack (manage_ops)| ACK["store.ack_incident + audit_store.log"]
  WD -->|健康/计费恢复| RES["store.resolve_open_incidents(kind=...)"]
  WD -->|每日节流| PURGE["store.purge_resolved_incidents(retention)"]
  RES --> TBL
  PURGE --> TBL
```

## 4. 路由

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/admin/ops` | `page_auth` | 运营总览页（注入 `can_manage_ops` 决定 ack 按钮显隐） |
| GET | `/api/admin/ops-overview?days=&month=&hours=` | `api_auth` | 聚合 ROI/计费/健康/可靠性 + 未关闭事件数 + 计费异常 |
| GET | `/api/admin/incidents?status=&limit=` | `api_auth` | 运维事件列表 + `suggested_assignee`（最闲在线坐席） |
| POST | `/api/admin/incidents/{id}/ack` | `api_write("manage_ops")` | 确认/指派事件，写审计 |

> `/api/admin/*` 同源会话 cookie 即可访问（与 dashboard 直接 fetch 一致）；写操作受全局 CSRF 中间件保护。

## 5. `ops_incidents` 表

| 列 | 说明 |
|----|------|
| `kind` | `health` / `billing`，去重与恢复按 kind 隔离 |
| `signature` | 去重键（健康=异常组件签名；计费=异常码集合） |
| `light` | `red` / `yellow` |
| `summary_json` / `problems_json` | 摘要与问题明细 |
| `status` | `open` / `acked` / `resolved` |
| `assigned_to` | 指派处理人 |
| `opened_ts` / `updated_ts` / `acked_ts` / `resolved_ts` | 生命周期时间戳 |

核心方法（`src/inbox/store.py`）：`open_or_update_incident`（按 `(kind,signature)` 去重）、
`resolve_open_incidents(kind=)`、`ack_incident`、`list_incidents(status=,kind=)`、
`count_open_incidents`、`purge_resolved_incidents(older_than_ts)`。

## 6. 事件类型（D3 通道）

`health_alert`、`billing_alert` 均已登记于：
- `webhook_notifier._EVENT_ALIASES`（webhook 订阅名）+ `_build_message`（消息格式化）
- `realtime_routes._SSE_EVENT_TYPES` / `_NOTIF_EVENT_TYPES`（SSE 推送 + 通知队列）

外发需在「告警渠道」给某条 webhook 订阅对应事件。

## 7. 配置（`config.yaml::health_watchdog`）

```yaml
health_watchdog:
  enabled: true
  interval_sec: 300              # 健康巡检周期
  queue_threshold: 200           # 草稿队列积压阈值
  alert_on_warn: false           # 黄灯是否告警
  billing_interval_sec: 3600     # E3 计费巡检周期（比健康稀疏）
  incident_retention_days: 30    # 已关闭事件保留天数（每日清理；<=0 关闭）
```

## 8. RBAC

- 新增写权限位 `manage_ops`（master + admin，见 `src/utils/web_user_store.py::WRITE_PERMISSIONS`）。
- ack 接口硬性校验 `manage_ops`；总览页对无权角色隐藏「确认」按钮（前端体感，后端仍是硬闸）。
- ack 成功写一条审计：`audit_store.log(actor, "ack_incident", "incident:{id}", "", assigned_to)`。

## 9. 设计取舍

- **纯函数聚合**：`assemble_ops_overview` / `billing_anomalies` 不碰 IO，路由层喂入各 builder 结果，便于单测。
- **kind 隔离恢复**：计费并入事件后，健康恢复绝不误清计费事件，反之亦然。
- **piggyback watchdog**：计费巡检 + 过期清理都挂在既有周期任务上、各自独立节流，不引入新 worker。
- **复用 D3 投递**：计费异常不造新通道，复用 webhook/SSE/通知三处既有 plumbing。

## 10. G 线 · 运营智能化（看板 → 洞察）

E/F 线把数据「摆出来」；G 线把数据「读懂」，给出可执行结论。三块纯函数集中在
`src/utils/ops_intel.py`（不碰 IO，路由层喂数据）：

| 能力 | 函数 | 接入点 | 价值 |
| --- | --- | --- | --- |
| **G1 根因建议** | `incident_advice(problems)` | `/api/admin/incidents` 每条带 `advice`；总览页事件行「💡 建议」 | 把「告警」升级成「告警 + 该怎么办」 |
| **G2 趋势异动** | `detect_trend_anomaly(values, drop_last=)` | `/api/admin/ops-overview` 返回 `anomalies`；总览页趋势标题旁「↑/↓ N% 异动」徽章 | 主动点名突增突降，不靠人盯折线 |
| **G3 运营周报** | `build_ops_report(...)` + store `get_incident_stats(since)` | `/api/admin/ops-report?days=7`；总览页「📰 运营周报」区 | 一段话摘要：事件数/MTTR/自动化省时/转化/可靠性 |

设计要点：

- **根因映射**：按问题 `id`（`db`/`ai`/`license`/`channels`/`worker_*`/`queue` + 计费 `over_seats`/`message_overage`）查表给「可能根因 + 处置建议」，`worker_*` 走前缀，未知项有兜底文案。
- **半桶去噪**：日/时趋势的末桶是「当前未走完」时段，直接比会误报「↓ 异动」。`detect_trend_anomaly(drop_last=True)` 丢弃半桶、以「最后一个已完结桶」为候选点，既消噪又仍能抓昨天/上一小时的突变。
- **MTTR**：`get_incident_stats` 用 `resolved_ts - opened_ts` 在 SQL 侧聚合已解决事件平均解决时长，周报换算成小时展示。

## 11. H 线 · 运营自动化闭环

G 线让系统「读懂」，H 线让系统「主动报」——把周报从「按需登录看板拉取」升级成「定时自动外发到主管 IM」。

### H1 运营周报自动外发

- **触发**：`HealthWatchdog._maybe_weekly_report` 挂在既有巡检周期上（与计费巡检/事件清理并列），按 `weekly_interval_sec`（默认 7 天）节流。`_last_weekly_ts` 初始化为「启动时刻」→ 首份周报在运行满一个周期后才发，**避免每次重启刷屏**。默认 `weekly_report_enabled: false`（遵循「新子系统默认关」约定）。
- **装配**：watchdog 无 request，故周报以「运维 + 自动化 + 计费」为主：
  - 事件统计 `store.get_incident_stats(since)` / 上周 `get_incident_stats(prev_since, until_ts=since)`；
  - 自动化价值 `ops_intel.automation_value(get_automation_roi_stats(...))`（与 ROI 看板同口径：节省人力 = AI 应答数 × 每条秒数，读 `workspace.roi.sec_per_reply`）；
  - 计费 `_compute_statement()`（与 E3 计费巡检共用）；
  - 环比 `ops_intel.weekly_compare(cur, prev)` → 嵌入 `build_ops_report(compare=...)`，headline 追加「环比上周：事件 ±N 起、AI 占比 ±Npp」。
  - ROI 的「经营/首响」段依赖 request（`_daily_report_rows`），watchdog 取不到，`business` 段从缺（`build_ops_report` 优雅降级）。
- **外发**：`EventBus.publish("ops_report", report)`，复用 D3 三处既有 plumbing：
  - `webhook_notifier._EVENT_ALIASES["ops_report"]` + `_build_message`（把 headline + 环比渲染成 IM 文本，带 `/admin/ops` 链接）；
  - `realtime_routes._SSE_EVENT_TYPES` / `_NOTIF_EVENT_TYPES`（SSE + 站内通知）。
  - 外发需在「告警渠道」给某条 webhook 订阅 `ops_report` 事件。

```yaml
health_watchdog:
  weekly_report_enabled: false   # 开后每周自动外发运营周报
  weekly_interval_sec: 604800    # 周报周期（秒，默认 7 天）
```

### H2 根因建议直达 + 一键动作

把 G1 的「文字建议」升级成「点过去 / 点一下就解决」：

- **直达链接**：`incident_advice` 每条 advice 带 `link`，指向能直接动手的后台页（均为已存在路由）：
  `ai→/settings`、`channels→/workspace/setup`、`queue→/workspace/drafts`、
  `over_seats|message_overage→/workspace/usage`、`db|license|worker_*→/admin/ops`。
- **一键动作**：advice 在「安全可自动化」时带 `fix={key,label,target}`。当前覆盖
  **熔断中的 autosend worker**（`worker_autosend` 且 `status=warn`）→「重置熔断」。
  fail（未运行）需进程级处理，故不给一键钮，避免误操作。
- **端点**（均 `manage_ops` + 审计留痕）：
  - `POST /api/admin/workers/{worker_id}/reset-circuit` —— 调 `AutosendWorker.reset_circuit()`（闭合熔断、清连续错误计数），返回 `was_open`。
  - `POST /api/admin/health/recheck` —— 调 `HealthWatchdog.recheck()`（复用 `_evaluate_health`，即时开/关事件）；无 watchdog 时退化为只读 `collect_health`。
- **闭环体感**：修复 → 点「重置熔断 / 立即重新巡检」→ 事件即时 resolved，无需等下个巡检周期。
  前端一键钮/巡检钮均受 `can_manage_ops` 控制可见性（后端仍是硬闸）。
