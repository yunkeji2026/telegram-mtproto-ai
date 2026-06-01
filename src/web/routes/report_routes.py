"""运营日报 / 周报 API 路由（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  GET /api/report/daily     K4 运营日报（KB/质量/策略/AB/翻译/风险/未命中 + 告警 + text 摘要）
  GET /api/report/weekly    F4 运营周报（7 天聚合 + 环比）

依赖经 AdminRouteContext 注入；周报后台推送循环 _weekly_report_loop 仍留 admin.py
（属后台任务而非路由）。
"""

from __future__ import annotations

import time

from fastapi import Request


def register_report_routes(app, ctx) -> None:
    config_manager = ctx.config_manager
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _kb_store = ctx.kb_store

    def _get_strategy_tracker():
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "strategy_tracker", None)
        return None

    @app.get("/api/report/daily")
    async def api_daily_report(request: Request, hours: int = 24):
        """
        K4: 生成运营日报 — 聚合所有核心指标 + 智能异常识别 + 趋势对比。
        返回结构化 JSON + 人类可读的 text 摘要。
        """
        _api_auth(request)
        report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "hours": hours}
        alerts = []

        # 1. KB 命中率
        try:
            qa = _kb_store.get_query_analytics(hours=hours)
            hit_pct = qa.get("totals", {}).get("hit_pct", 0)
            total_queries = qa.get("totals", {}).get("total", 0)
            weak_pct = qa.get("totals", {}).get("weak_pct", 0)
            report["kb"] = {
                "total_queries": total_queries,
                "hit_pct": hit_pct,
                "weak_pct": weak_pct,
                "avg_score": qa.get("totals", {}).get("avg_score", 0),
            }
            if hit_pct < 70:
                alerts.append(f"知识库命中率仅 {hit_pct}%，建议补充知识条目")
            if weak_pct > 30:
                alerts.append(f"弱命中占比 {weak_pct}%，建议优化触发词或拆分条目")
        except Exception:
            report["kb"] = {}

        # 2. 回复质量
        try:
            rq = _kb_store.get_reply_quality_stats(days=1)
            sat_rate = rq.get("satisfaction_rate", 0)
            report["quality"] = {
                "satisfaction_rate": sat_rate,
                "positive": rq.get("positive", 0),
                "negative": rq.get("negative", 0),
            }
            if sat_rate < 60:
                alerts.append(f"回复满意度仅 {sat_rate}%，需要优化回复策略")
        except Exception:
            report["quality"] = {}

        # 3. 策略效果
        tracker = _get_strategy_tracker()
        if tracker:
            try:
                tracker.mark_no_follow_up()
                summary = tracker.strategy_summary(hours)
                total_msgs = tracker.total_events(hours)
                from src.utils.strategy_advisor import analyze
                rs = config_manager.get_strategies_config()
                advisor = analyze(summary, rs.get("strategies", {}))
                report["strategy"] = {
                    "total_messages": total_msgs,
                    "best_strategy": advisor.get("best"),
                    "worst_strategy": advisor.get("worst"),
                    "scores": advisor.get("scores", {}),
                }
                worst = advisor.get("worst")
                if worst and advisor["scores"].get(worst, 100) < 40:
                    alerts.append(f"策略 {worst} 质量评分低于 40，建议调整")
            except Exception:
                report["strategy"] = {}

        # 4. A/B 测试状态
        try:
            rs = config_manager.get_strategies_config()
            ab_tests = rs.get("ab_tests", {})
            active = sum(1 for ab in ab_tests.values()
                         if isinstance(ab, dict) and ab.get("enabled"))
            concluded = sum(1 for ab in ab_tests.values()
                           if isinstance(ab, dict) and ab.get("concluded"))
            report["ab_tests"] = {
                "active": active, "concluded": concluded, "total": len(ab_tests),
            }
        except Exception:
            report["ab_tests"] = {}

        # 5. 翻译覆盖
        try:
            entries = _kb_store.list_entries(enabled_only=True)
            total_entries = len(entries)
            target_langs = ["en", "ur", "pt", "ar"]
            gaps = 0
            for e in entries:
                full = _kb_store.get_entry(e["id"])
                trans = (full or {}).get("translations", {})
                if any(l not in trans for l in target_langs):
                    gaps += 1
            coverage = round((total_entries - gaps) / max(total_entries, 1) * 100)
            report["translation"] = {
                "total_entries": total_entries,
                "coverage_pct": coverage,
                "gap_count": gaps,
            }
            if coverage < 50:
                alerts.append(f"翻译覆盖率仅 {coverage}%，影响多语言用户体验")
        except Exception:
            report["translation"] = {}

        # 6. at_risk 用户
        try:
            ctx_store = None
            if telegram_client:
                sm = getattr(telegram_client, "skill_manager", None)
                if sm:
                    ctx_store = getattr(sm, "_context_store", None)
            if ctx_store:
                risk_count = sum(
                    1 for c in ctx_store._cache.values()
                    if isinstance(c.get("_user_profile"), dict)
                    and c["_user_profile"].get("at_risk")
                )
                report["user_risk"] = {"at_risk_count": risk_count}
                if risk_count > 5:
                    alerts.append(f"{risk_count} 个用户满意度极低，可能流失")
        except Exception:
            report["user_risk"] = {}

        # 7. 未命中热词
        try:
            misses = _kb_store.get_miss_stats(top_k=5)
            report["top_misses"] = [
                {"query": m["query"][:50], "count": m["cnt"]}
                for m in misses if not m["query"].startswith("[TRANSLATE:")
            ]
        except Exception:
            report["top_misses"] = []

        report["alerts"] = alerts

        # 生成人类可读摘要
        lines = [f"📊 运营日报（过去 {hours} 小时）", ""]
        kb = report.get("kb", {})
        if kb:
            lines.append(f"知识库: 查询 {kb.get('total_queries', 0)} 次, "
                         f"命中率 {kb.get('hit_pct', 0)}%, "
                         f"平均分 {kb.get('avg_score', 0):.2f}")
        quality = report.get("quality", {})
        if quality:
            lines.append(f"回复质量: 满意度 {quality.get('satisfaction_rate', 0)}% "
                         f"(+{quality.get('positive', 0)}/-{quality.get('negative', 0)})")
        strat = report.get("strategy", {})
        if strat:
            lines.append(f"策略: 共 {strat.get('total_messages', 0)} 条消息, "
                         f"最优={strat.get('best_strategy', '-')}")
        trans = report.get("translation", {})
        if trans:
            lines.append(f"翻译: 覆盖率 {trans.get('coverage_pct', 0)}% "
                         f"({trans.get('gap_count', 0)} 条待翻译)")
        risk = report.get("user_risk", {})
        if risk:
            lines.append(f"用户: {risk.get('at_risk_count', 0)} 人处于流失风险")
        if alerts:
            lines.append("")
            lines.append("⚠ 告警:")
            for a in alerts:
                lines.append(f"  • {a}")

        report["text_summary"] = "\n".join(lines)
        return report

    @app.get("/api/report/weekly")
    async def api_weekly_report(request: Request):
        """F4: 7 天聚合周报 — 复用日报逻辑 + 环比趋势"""
        _api_auth(request)
        report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "type": "weekly"}

        # 本周数据（168h）
        try:
            with _kb_store._conn() as c:
                now_ts = time.time()
                this_week = now_ts - 168 * 3600
                last_week = this_week - 168 * 3600
                tw_total = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (this_week,)
                ).fetchone()[0]
                tw_hits = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (this_week,)
                ).fetchone()[0]
                lw_total = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND ts < ?",
                    (last_week, this_week)
                ).fetchone()[0]
                lw_hits = c.execute(
                    "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND ts < ? AND hit=1",
                    (last_week, this_week)
                ).fetchone()[0]
            tw_rate = round(tw_hits / max(tw_total, 1) * 100, 1)
            lw_rate = round(lw_hits / max(lw_total, 1) * 100, 1)
            report["kb"] = {
                "this_week": {"queries": tw_total, "hits": tw_hits, "hit_rate": tw_rate},
                "last_week": {"queries": lw_total, "hits": lw_hits, "hit_rate": lw_rate},
                "trend": round(tw_rate - lw_rate, 1),
            }
        except Exception:
            report["kb"] = {}

        # 反馈趋势
        try:
            with _kb_store._conn() as c:
                tw_pos = c.execute(
                    "SELECT COUNT(*) FROM kb_feedback WHERE score > 0 "
                    "AND created_at >= datetime(?, 'unixepoch')", (this_week,)
                ).fetchone()[0]
                tw_neg = c.execute(
                    "SELECT COUNT(*) FROM kb_feedback WHERE score < 0 "
                    "AND created_at >= datetime(?, 'unixepoch')", (this_week,)
                ).fetchone()[0]
            report["feedback"] = {
                "positive": tw_pos, "negative": tw_neg,
                "satisfaction": round(tw_pos / max(tw_pos + tw_neg, 1) * 100, 1),
            }
        except Exception:
            report["feedback"] = {}

        # 生成可读摘要
        lines = ["📊 运营周报", ""]
        kb = report.get("kb", {})
        if kb:
            tw = kb.get("this_week", {})
            lines.append(f"本周 KB: {tw.get('queries', 0)} 次查询, 命中率 {tw.get('hit_rate', 0)}%")
            trend = kb.get("trend", 0)
            lines.append(f"环比: {'📈 +' if trend > 0 else '📉 '}{trend}%")
        fb = report.get("feedback", {})
        if fb:
            lines.append(f"反馈: 好评 {fb.get('positive', 0)}, 差评 {fb.get('negative', 0)}, "
                         f"满意度 {fb.get('satisfaction', 0)}%")
        report["text_summary"] = "\n".join(lines)
        return report
