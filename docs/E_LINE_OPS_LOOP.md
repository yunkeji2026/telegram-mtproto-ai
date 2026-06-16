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
