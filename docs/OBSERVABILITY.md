# Messenger RPA 可观测性指南（P3/P4）

## 1. Prometheus 指标端点

主程序启动后访问：

```
GET http://<host>:18787/api/messenger-rpa/metrics
Authorization: Bearer <admin token>
Content-Type: text/plain  (Prometheus exposition format)
```

在 `prometheus.yml` 配置 scrape：

```yaml
scrape_configs:
  - job_name: messenger_rpa
    scrape_interval: 15s
    metrics_path: /api/messenger-rpa/metrics
    authorization:
      type: Bearer
      credentials: admin
    static_configs:
      - targets: ['127.0.0.1:18787']
```

## 2. 核心指标一览

| 指标 | 类型 | 含义 |
|------|------|------|
| `messenger_rpa_service_running` | gauge | RPA 主循环是否活跃（0/1） |
| `messenger_rpa_notif_running` | gauge | 通知监听 task 是否活跃 |
| `messenger_rpa_sla_running` | gauge | 审批 SLA loop 是否活跃 |
| `messenger_rpa_run_duration_seconds_bucket` | histogram | 端到端 run 耗时分布 |
| `messenger_rpa_phase_duration_seconds_bucket{phase=...}` | histogram | 分阶段耗时（inbox_vision/thread_vision/llm） |
| `messenger_rpa_runs_total{outcome=...}` | counter | run 结果（ok/error/risk_blocked/no_peer/…） |
| `messenger_rpa_caption_source_total{source=...}` | counter | 图片 caption 来源（prefetch/sync/timeout/error） |
| `messenger_rpa_risk_status` | gauge | 账号风控状态（0=normal, 1=warn, 2=blocked） |
| `messenger_rpa_risk_hit_count` | gauge | 连续命中次数 |
| `messenger_rpa_risk_blocked_until_ts` | gauge | 若 blocked，pause 到期时间戳 |
| `messenger_rpa_pace_ratio` | gauge | 当前小时发送量 / 历史同 hour 中位数 |
| `messenger_rpa_pace_decision` | gauge | 节奏决策（0 allow / 1 throttle / 2 deny / -1 err） |
| `messenger_rpa_chat_credit_distribution{bucket=...}` | gauge | chat 信用分分布（bucket=100/80_99/60_79/40_59/20_39/0_19） |
| `messenger_rpa_chat_credit_low_total` | gauge | 信用 < 40 的 chat 数 |
| `messenger_rpa_approvals_pending` | gauge | 审批 pending 数 |
| `messenger_rpa_approvals_overdue` | gauge | 超过 SLA 的审批数 |
| `messenger_rpa_approvals_oldest_age_seconds` | gauge | 最老 pending 审批的年龄 |
| `messenger_rpa_sla_alerts_sent_total` | counter | SLA 告警已推送次数 |
| `messenger_rpa_llm_total_cost_usd` | counter | 累计 LLM 成本（USD，P6-4） |
| `messenger_rpa_llm_total_calls` | counter | 累计 LLM 调用次数（P6-4） |
| `messenger_rpa_llm_tokens_total{model,tier,account,kind}` | counter | 按桶的 token 累计（prompt/completion，P6-4） |
| `messenger_rpa_llm_cost_usd_total{model,tier,account}` | counter | 按桶成本（P6-4） |

## 3. Grafana dashboard 导入

1. Grafana 菜单 → Dashboards → Import
2. 上传 `docs/grafana_dashboard.json`
3. 数据源选你配置的 Prometheus

包含的面板：
- 概览：服务状态 / 风控 / 节奏 / 今日已发 / 审批待处理 / 低信用 chat 数
- 延迟：Run duration p50/p95/p99 + Phase latency p95
- 事件：Outcomes rate/min + Pace ratio 曲线 + Caption 来源 pie + Credit 分布 pie + SLA 趋势

## 4. 常用 PromQL

```promql
# run 失败率（5 分钟窗）
sum(rate(messenger_rpa_runs_total{outcome!="ok"}[5m])) /
sum(rate(messenger_rpa_runs_total[5m]))

# run p95 latency
histogram_quantile(0.95, sum by(le)(rate(messenger_rpa_run_duration_seconds_bucket[5m])))

# 账号被封比例
messenger_rpa_risk_status == 2

# 节奏超线告警（> 1.5 倍）
messenger_rpa_pace_ratio > 1.5

# 审批积压告警
messenger_rpa_approvals_overdue > 0

# P6-4：每日 LLM 成本（按 24h 窗口增量）
increase(messenger_rpa_llm_total_cost_usd[24h])

# P6-4：单 premium 模型成本突增（1h 窗，按 model/tier 分组）
sum by(model, tier) (
  rate(messenger_rpa_llm_cost_usd_total{tier="premium"}[1h])
) > 0.10   # USD/秒 → 约等于 360 USD/小时，按实际阈值调整

# P6-4：tokens 按 model 分布（用于识别 tier 路由是否生效）
sum by(model) (rate(messenger_rpa_llm_tokens_total[5m]))

# P6-4：每账号单位成本（每条有效回复的均摊 cost）
sum by(account) (rate(messenger_rpa_llm_cost_usd_total[1h])) /
sum by(account) (rate(messenger_rpa_runs_total{outcome="ok"}[1h]))
```

## 5. 建议的 Alertmanager 规则

```yaml
- alert: MessengerRpaRiskBlocked
  expr: messenger_rpa_risk_status == 2
  for: 1m
  labels: {severity: critical}
  annotations:
    summary: "Messenger 账号被 FB 风控锁定"

- alert: MessengerRpaPaceAbnormal
  expr: messenger_rpa_pace_ratio > 1.5
  for: 5m
  labels: {severity: warning}
  annotations:
    summary: "本小时发送量显著高于历史中位数"

- alert: MessengerRpaSLAOverdue
  expr: messenger_rpa_approvals_overdue > 0
  for: 2m
  labels: {severity: warning}

- alert: MessengerRpaErrorRateHigh
  expr: |
    sum(rate(messenger_rpa_runs_total{outcome="error"}[5m])) /
    sum(rate(messenger_rpa_runs_total[5m])) > 0.3
  for: 5m
  labels: {severity: warning}

# ── P6-4：LLM 成本类告警 ─────────────────
- alert: MessengerRpaLlmCostDailyExceed
  expr: increase(messenger_rpa_llm_total_cost_usd[24h]) > 50
  labels: {severity: warning}
  annotations:
    summary: "过去 24h LLM 成本超 $50（请检查 tier 路由是否失效）"

- alert: MessengerRpaLlmCostSpike
  expr: |
    sum(rate(messenger_rpa_llm_cost_usd_total[10m])) >
    3 * sum(rate(messenger_rpa_llm_cost_usd_total[6h] offset 10m))
  for: 10m
  labels: {severity: critical}
  annotations:
    summary: "LLM 成本 10 分钟内飙升到历史 6h 均值的 3×，可能有异常放大调用"

- alert: MessengerRpaPremiumTierOveruse
  expr: |
    sum(rate(messenger_rpa_llm_calls_total{tier="premium"}[1h])) /
    sum(rate(messenger_rpa_llm_calls_total[1h])) > 0.30
  for: 30m
  labels: {severity: warning}
  annotations:
    summary: "premium tier 占比 >30%（_classify_ai_tier 阈值可能需收紧）"
```

## 6. 运维 API

| 端点 | 作用 |
|------|------|
| `GET /api/messenger-rpa/status` | 总览（含 risk/sla/pace/credit） |
| `GET /api/messenger-rpa/replays?limit=50` | 失败 run 回放包列表 |
| `POST /api/messenger-rpa/replays/rerun` | 脱机重跑某 zip 的 LLM，对比新旧 reply |
| `GET /api/messenger-rpa/credits` | 所有 tracked chat 的信用分 |
| `POST /api/messenger-rpa/credits/{chat_key}/reset` | 重置某 chat 信用 |
| `GET /api/messenger-rpa/variants/stats` | A/B persona 指标 |
| `GET /api/messenger-rpa/templates` | 快捷模板 |
