"""系统健康巡检 / 告警状态 API（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  GET /api/health-check    一键系统健康巡检（分级问题 + 综合评分）
  GET /api/alert-status    仪表盘告警横幅聚合

依赖经 AdminRouteContext 注入，含本轮新增的 domain_web_pages / domain_dashboard_widgets。
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, Request


def register_health_routes(app, ctx) -> None:
    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    event_tracker = ctx.event_tracker
    _page_auth = ctx.page_auth
    domain_web_pages = ctx.domain_web_pages
    domain_dashboard_widgets = ctx.domain_dashboard_widgets

    @app.get("/api/health-check")
    async def api_health_check(request: Request, _=Depends(_page_auth)):
        """
        一键系统健康巡检：
        返回按严重程度分级的问题列表（critical / warn / info）和整体评分。
        """
        issues = []

        def _issue(level, category, title, detail="", action_url="", action_label=""):
            issues.append({"level": level, "category": category, "title": title,
                           "detail": detail, "action_url": action_url, "action_label": action_label})

        # 1. 模板配置检查（已迁移至 KB，仅做兼容提示）
        tpl = config_manager.get_dynamic_templates_config() or {}
        if not tpl:
            pass  # 话术已统一至知识库"系统话术"分类，templates.yaml 为空不再告警
        else:
            empty_keys = [k for k, v in tpl.items() if not v or (isinstance(v, list) and not any(v))]
            if empty_keys:
                _issue("warn", "模板", f"{len(empty_keys)} 个旧模板键值为空（建议迁移到知识库）",
                       f"空键: {', '.join(empty_keys[:5])}", "/templates", "查看")

        # 2. 通道健康检查（仅对声明了 channel page 的域生效）
        if any(p.get("key") == "ch" for p in domain_web_pages):
            rates = config_manager.get_exchange_rates_config() or {}
            channels = rates.get("channels", {})
            if not channels:
                _issue("critical", "通道", "无任何通道配置", "Bot 无法处理交易", "/channels", "前往配置")
            else:
                active_channels = [n for n, c in channels.items()
                                   if c.get("status") in ("正常", "active", "启用")]
                if not active_channels:
                    _issue("critical", "通道", "所有通道均已停用",
                           f"共 {len(channels)} 个通道，全部处于非启用状态", "/channels", "查看")
                elif len(active_channels) < len(channels):
                    off = [n for n in channels if channels[n].get("status") not in ("正常", "active", "启用")]
                    off_labels = [f"{n}({channels[n].get('status','?')})" for n in off[:3]]
                    _issue("warn", "通道", f"{len(off)} 个通道非正常",
                           f"异常通道: {', '.join(off_labels)}", "/channels", "查看")
                zero_rate = [n for n, c in channels.items()
                             if str(c.get("fee_rate", "0")).replace("%", "").strip() in ("0", "0.0", "")]
                if zero_rate:
                    _issue("warn", "通道", f"{len(zero_rate)} 个通道费率为 0",
                           f"通道: {', '.join(zero_rate[:3])}", "/channels", "查看")

        # 3. 策略检查
        try:
            rs = config_manager.get_strategies_config()
            strategies = rs.get("strategies", {})
            if not strategies:
                _issue("warn", "策略", "无策略配置", "Bot 将使用默认行为", "/strategies", "前往配置")
            else:
                disabled = [sid for sid, s in strategies.items() if s.get("enabled") is False]
                if len(disabled) == len(strategies):
                    _issue("critical", "策略", "所有策略均已禁用",
                           "Bot AI 回复已完全关闭", "/strategies", "查看")
                elif disabled:
                    _issue("info", "策略", f"{len(disabled)} 个策略已禁用",
                           f"禁用策略: {', '.join(disabled[:3])}", "/strategies", "查看")
        except Exception:
            pass

        # 4. 策略效果检查（质量评分）
        if event_tracker:
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker)
                analytics = sa.get_all_strategy_analytics(hours=24)
                low_score = [(sid, a.quality_score) for sid, a in analytics.items()
                             if hasattr(a, "quality_score") and a.quality_score is not None
                             and a.quality_score < 40]
                if low_score:
                    detail = "; ".join(f"{s}:{q:.0f}分" for s, q in low_score[:3])
                    _issue("warn", "效果", f"{len(low_score)} 个策略质量评分低于 40",
                           detail, "/strategy-analytics", "查看分析")
            except Exception:
                pass

        # 5. 审计存储检查
        if audit_store:
            try:
                count = len(audit_store.query(limit=10001))
                if count > 10000:
                    _issue("info", "存储", "审计日志超过 10000 条",
                           f"当前约 {count} 条，建议定期清理或导出", "/audit", "查看")
            except Exception:
                pass

        # 6. 快照检查
        try:
            cfg_dir = config_manager.config_path.parent
            snap_dir = cfg_dir / "snapshots"
            if not snap_dir.exists() or not list(snap_dir.glob("*.yaml")):
                _issue("info", "快照", "暂无配置快照",
                       "建议手动触发一次配置导出以创建基准快照", "/diff", "查看")
        except Exception:
            pass

        # 综合评分：100 - critical×30 - warn×10 - info×2
        score = 100
        for iss in issues:
            score -= {"critical": 30, "warn": 10, "info": 2}.get(iss["level"], 0)
        score = max(0, min(100, score))

        level_summary = {
            "critical": sum(1 for i in issues if i["level"] == "critical"),
            "warn": sum(1 for i in issues if i["level"] == "warn"),
            "info": sum(1 for i in issues if i["level"] == "info"),
        }

        return {
            "score": score,
            "issues": issues,
            "level_summary": level_summary,
            "status": "critical" if level_summary["critical"] > 0
                      else ("warn" if level_summary["warn"] > 0 else "ok"),
        }

    @app.get("/api/alert-status")
    async def api_alert_status(request: Request, _=Depends(_page_auth)):
        """聚合所有系统告警状态，供仪表盘告警横幅使用"""

        def _compute_alerts():
            alerts = []

            # 1. 通道健康告警（仅声明了 channel_health widget 的域）
            if any(w.get("key") == "channel_health" for w in domain_dashboard_widgets):
                rates_data = config_manager.get_exchange_rates_config() or {}
                channels = rates_data.get("channels", {})
                if channels:
                    from src.utils.channel_health import compute_health_scores
                    health = compute_health_scores(channels, event_tracker)
                    critical_channels = [h for h in health if h["grade"] == "critical"]
                    warning_channels = [h for h in health if h["grade"] == "warning"]
                    if critical_channels:
                        names = "、".join(h["display_name"] for h in critical_channels[:3])
                        alerts.append({
                            "level": "critical",
                            "type": "channel",
                            "title": f"{len(critical_channels)} 个通道异常",
                            "body": f"异常通道：{names}。请立即检查通道配置和状态。",
                            "action_url": "/channels",
                            "action_label": "查看通道",
                        })
                    elif warning_channels:
                        names = "、".join(h["display_name"] for h in warning_channels[:3])
                        alerts.append({
                            "level": "warn",
                            "type": "channel",
                            "title": f"{len(warning_channels)} 个通道警告",
                            "body": f"警告通道：{names}，健康评分偏低。",
                            "action_url": "/channels",
                            "action_label": "查看通道",
                        })

            # 2. 策略质量告警
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker) if event_tracker else None
                if sa:
                    summary = sa.summarize(hours=24)
                    bad = [s for s in summary if s.get("quality_score", 100) < 40]
                    if bad and len(bad) == len(summary):
                        alerts.append({
                            "level": "critical",
                            "type": "strategy",
                            "title": "所有策略质量评分过低",
                            "body": f"{len(bad)} 个策略质量评分均低于 40 分，AI 效果可能严重下降。",
                            "action_url": "/strategy-analytics",
                            "action_label": "查看分析",
                        })
                    elif bad:
                        strats = "、".join(s["strategy_id"] for s in bad[:3])
                        alerts.append({
                            "level": "warn",
                            "type": "strategy",
                            "title": f"{len(bad)} 个策略质量偏低",
                            "body": f"策略 {strats} 质量评分低于 40 分，建议优化。",
                            "action_url": "/strategy-analytics",
                            "action_label": "查看分析",
                        })
            except Exception:
                pass

            highest_level = "ok"
            if any(a["level"] == "critical" for a in alerts):
                highest_level = "critical"
            elif any(a["level"] == "warn" for a in alerts):
                highest_level = "warn"

            return {
                "alerts": alerts,
                "highest_level": highest_level,
                "alert_count": len(alerts),
            }

        return await asyncio.to_thread(_compute_alerts)
