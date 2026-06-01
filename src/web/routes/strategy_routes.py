"""策略 / 策略分析 / A-B 测试路由 — 从 admin.py 抽出（Phase E1，批 3）。

首个采用 AdminRouteContext 的批次：依赖通过 ctx 传入，避免 kwargs 穿线。

端点（与抽出前逐行一致）：
  GET  /strategies                              PUT /api/strategies/{strategy_id}
  PUT  /api/strategies/mapping                  GET /api/strategies
  GET  /strategy-analytics                      GET /api/strategy-analytics
  GET  /api/strategy-analytics/compare          GET /api/strategy-analytics/{strategy_id}/hourly
  GET  /api/model-summary                       GET /api/user-segments
  PUT  /api/ab-tests/{intent}                   GET /api/ab-tests/evaluate
  GET  /api/strategy-history/{strategy_id}
"""

from __future__ import annotations

import yaml
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse


def register_strategy_routes(app, ctx):
    from src.web.admin import templates

    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    telegram_client = ctx.telegram_client
    _page_auth = ctx.page_auth
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write
    _auto_snapshot = ctx.auto_snapshot
    _get_intent_display_names = ctx.get_intent_display_names

    def _get_strategy_tracker():
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "strategy_tracker", None)
        return None

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies_page(request: Request, _=Depends(_page_auth)):
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        intent_map = rs.get("intent_strategy_map", {})
        return templates.TemplateResponse(request, "strategies.html", {
            "strategies": strategies, "intent_map": intent_map,
            "intent_display_names": _get_intent_display_names(),
        })

    @app.put("/api/strategies/{strategy_id}")
    async def api_update_strategy(strategy_id: str, request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if strategy_id not in strategies:
            raise HTTPException(404, f"Strategy '{strategy_id}' not found")
        snap_content = yaml.dump(rs, allow_unicode=True, default_flow_style=False)
        for key in ("temperature", "max_tokens", "context_rounds",
                    "reply_probability", "enabled", "skip_ai"):
            if key in body:
                strategies[strategy_id][key] = body[key]
        rs["strategies"] = strategies
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        actor = request.session.get("username", "api")
        _auto_snapshot("reply_strategies", snap_content, actor)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log(actor, "update_strategy", strategy_id, "", str(body)[:100])
        return {"ok": True, "strategy_id": strategy_id}

    @app.put("/api/strategies/mapping")
    async def api_update_mapping(request: Request, _=Depends(_api_write("edit_strategy"))):
        body = await request.json()
        intent = body.get("intent")
        sid = body.get("strategy_id")
        if not intent or not sid:
            raise HTTPException(400, "Missing intent or strategy_id")
        rs = config_manager.get_strategies_config()
        strategies = rs.get("strategies", {})
        if sid not in strategies:
            raise HTTPException(404, f"Strategy '{sid}' not found")
        rs.setdefault("intent_strategy_map", {})[intent] = sid
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log("web_admin", "update_mapping", intent, "", sid)
        return {"ok": True, "intent": intent, "strategy_id": sid}

    @app.get("/api/strategies")
    async def api_get_strategies(request: Request, _=Depends(_api_auth)):
        rs = config_manager.get_strategies_config()
        return {
            "strategies": rs.get("strategies", {}),
            "intent_strategy_map": rs.get("intent_strategy_map", {}),
        }

    @app.get("/strategy-analytics", response_class=HTMLResponse)
    async def strategy_analytics_page(request: Request, _=Depends(_page_auth),
                                      hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        summary = tracker.strategy_summary(hours) if tracker else []
        matrix = tracker.intent_strategy_matrix(hours) if tracker else []
        total = tracker.total_events(hours) if tracker else 0
        if tracker:
            tracker.mark_no_follow_up()

        rs = config_manager.get_strategies_config()
        strategies_cfg = rs.get("strategies", {})

        from src.utils.strategy_advisor import analyze, suggest_param_adjustments, compute_quality_score_breakdown
        advisor = analyze(summary, strategies_cfg) if summary else {
            "scores": {}, "advisories": [], "best": None, "worst": None}
        for s in summary:
            s["quality_score"] = advisor["scores"].get(s["strategy_id"], 0)
            s["score_breakdown"] = compute_quality_score_breakdown(s)
            s["model_id"] = strategies_cfg.get(s["strategy_id"], {}).get("model", "")

        param_suggestions = suggest_param_adjustments(summary, strategies_cfg) if summary else []
        ab_tests = rs.get("ab_tests", {})
        autopilot = rs.get("autopilot", {})
        session_stats = tracker.session_stats(hours) if tracker else {}
        model_summary = tracker.model_summary(hours) if tracker else []
        model_matrix = tracker.model_strategy_matrix(hours) if tracker else []
        user_segments = tracker.user_segment_analysis(hours) if tracker else {}

        return templates.TemplateResponse(request, "strategy_analytics.html", {
            "summary": summary, "matrix": matrix,
            "total": total, "hours": hours,
            "advisor": advisor, "ab_tests": ab_tests,
            "param_suggestions": param_suggestions,
            "autopilot": autopilot, "session_stats": session_stats,
            "model_summary": model_summary, "model_matrix": model_matrix,
            "user_segments": user_segments,
        })

    @app.get("/api/strategy-analytics")
    async def api_strategy_analytics(request: Request, _=Depends(_api_auth),
                                     hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"summary": [], "matrix": [], "total": 0}
        tracker.mark_no_follow_up()
        summary = tracker.strategy_summary(hours)
        rs = config_manager.get_strategies_config()
        strategies_cfg = rs.get("strategies", {})
        from src.utils.strategy_advisor import analyze, suggest_param_adjustments
        advisor = analyze(summary, strategies_cfg)
        return {
            "summary": summary,
            "matrix": tracker.intent_strategy_matrix(hours),
            "total": tracker.total_events(hours),
            "hours": hours,
            "advisor": advisor,
            "param_suggestions": suggest_param_adjustments(summary, strategies_cfg),
            "session_stats": tracker.session_stats(hours),
            "model_summary": tracker.model_summary(hours),
            "model_matrix": tracker.model_strategy_matrix(hours),
            "user_segments": tracker.user_segment_analysis(hours),
        }

    @app.get("/api/strategy-analytics/compare")
    async def api_strategy_compare(request: Request, _=Depends(_api_auth),
                                   hours: int = Query(24, ge=1, le=720)):
        """B2: Compare current period vs previous period of same length."""
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"current": [], "previous": [], "changes": {}}
        tracker.mark_no_follow_up()
        from src.utils.strategy_advisor import compute_quality_score_breakdown
        current = tracker.strategy_summary(hours, offset_hours=0)
        previous = tracker.strategy_summary(hours, offset_hours=hours)
        # compute changes per strategy
        prev_map = {s["strategy_id"]: s for s in previous}
        changes = {}
        for s in current:
            sid = s["strategy_id"]
            s["score_breakdown"] = compute_quality_score_breakdown(s)
            s["quality_score"] = s["score_breakdown"]["total"]
            p = prev_map.get(sid)
            if p:
                p["score_breakdown"] = compute_quality_score_breakdown(p)
                p["quality_score"] = p["score_breakdown"]["total"]
                changes[sid] = {
                    "total_delta": s["total"] - p["total"],
                    "avg_ms_delta": s["avg_ms"] - p["avg_ms"],
                    "follow_up_delta": round(s["follow_up_rate"] - p["follow_up_rate"], 1),
                    "silence_delta": round(s["silence_rate"] - p["silence_rate"], 1),
                    "score_delta": round(s["quality_score"] - p["quality_score"], 1),
                }
            else:
                changes[sid] = {"total_delta": s["total"], "avg_ms_delta": 0,
                                "follow_up_delta": 0, "silence_delta": 0, "score_delta": 0}
        return {"current": current, "previous": previous, "changes": changes, "hours": hours}

    @app.get("/api/strategy-analytics/{strategy_id}/hourly")
    async def api_strategy_hourly(strategy_id: str, request: Request,
                                  _=Depends(_api_auth),
                                  hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"data": []}
        return {"data": tracker.strategy_hourly(strategy_id, hours)}

    @app.get("/api/model-summary")
    async def api_model_summary(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"model_summary": [], "model_matrix": []}
        return {
            "model_summary": tracker.model_summary(hours),
            "model_matrix": tracker.model_strategy_matrix(hours),
        }

    @app.get("/api/user-segments")
    async def api_user_segments(request: Request, _=Depends(_api_auth),
                                hours: int = Query(24, ge=1, le=720)):
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"segments": {}}
        return {"segments": tracker.user_segment_analysis(hours)}

    @app.put("/api/ab-tests/{intent}")
    async def api_update_ab_test(intent: str, request: Request,
                                 _=Depends(_api_write("edit_strategy"))):
        """创建/更新/关闭某个意图的 A/B 灰度测试"""
        body = await request.json()
        rs = config_manager.get_strategies_config()
        ab = rs.setdefault("ab_tests", {})
        if body.get("delete"):
            ab.pop(intent, None)
        else:
            ab[intent] = {
                "enabled": body.get("enabled", True),
                "variants": body.get("variants", []),
            }
        rs["ab_tests"] = ab
        ok, msg = config_manager.save_strategies(rs)
        if not ok:
            raise HTTPException(500, msg)
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_refresh_strategies"):
                sm._refresh_strategies()
        if audit_store:
            audit_store.log(request.session.get("username", "web_admin"),
                            "update_ab_test", intent, "", str(body)[:200])
        return {"ok": True, "intent": intent}

    @app.get("/api/ab-tests/evaluate")
    async def api_ab_evaluate(request: Request, hours: int = 24):
        """L3: 评估所有活跃 A/B 测试，返回结论（胜者/继续/数据不足）"""
        _api_auth(request)
        tracker = _get_strategy_tracker()
        if not tracker:
            return {"results": [], "error": "策略追踪器未就绪"}
        tracker.mark_no_follow_up()
        summary = tracker.strategy_summary(min(hours, 168))
        rs = config_manager.get_strategies_config()
        ab_tests = rs.get("ab_tests", {})
        strategies_cfg = rs.get("strategies", {})
        from src.utils.strategy_advisor import evaluate_ab_tests
        results = evaluate_ab_tests(ab_tests, summary, strategies_cfg)
        return {"results": results, "ab_tests": ab_tests}

    @app.get("/api/strategy-history/{strategy_id}")
    async def api_strategy_history(strategy_id: str, request: Request, _=Depends(_page_auth),
                                   limit: int = 20):
        """返回某个策略最近 N 次参数变更记录（来自 audit_store）"""
        if not audit_store:
            return {"history": [], "strategy_id": strategy_id}
        all_entries = audit_store.query(limit=2000)
        # 筛选与该策略相关的操作记录
        history = [
            e for e in all_entries
            if strategy_id in str(e.get("target", "")) or strategy_id in str(e.get("new_val", ""))
            if "strategy" in str(e.get("action", "")).lower()
        ]
        return {"history": history[:limit], "strategy_id": strategy_id}
