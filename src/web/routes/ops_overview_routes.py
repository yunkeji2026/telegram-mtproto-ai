"""E 线运营总览 + 运维事件闭环路由。

- ``GET  /api/admin/ops-overview`` —— 把 ROI / 计费 / 运行时健康 / 运维可靠性 聚成
  「老板单页」payload（复用各自 builder，纯装配在 :mod:`src.utils.ops_overview`）。
- ``GET  /api/admin/incidents`` —— 列运维事件（health_alert 落表，E2）。
- ``POST /api/admin/incidents/{id}/ack`` —— 主管确认/指派一条事件。
- ``GET  /admin/ops`` —— 总览页面（同源会话鉴权，前端 fetch 上面的 API）。

鉴权沿用既有约定：``/api/admin/*`` 用 ``ctx.api_auth``（同源会话亦可，见
dashboard.html 直接 fetch），页面用 ``ctx.page_auth``。
"""

from __future__ import annotations

import logging
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


def _reliability_payload(request: Request, hours: int):
    """复刻 /api/admin/reliability 的装配逻辑（复用其 helper）。"""
    from src.utils.reliability import build_reliability
    from src.web.routes.runtime_health_routes import (
        _recent_health_alerts,
        _worker_snapshots,
    )

    hours = 24 if int(hours or 24) <= 24 else 168
    inbox = getattr(request.app.state, "inbox_store", None)
    timeline = []
    if inbox is not None and hasattr(inbox, "get_reliability_timeline"):
        since = time.time() - hours * 3600
        bucket = 3600 if hours <= 24 else 86400
        try:
            timeline = inbox.get_reliability_timeline(since, bucket_sec=bucket)
        except Exception:
            logger.debug("可靠性时间线读取失败（已忽略）", exc_info=True)
    return build_reliability(
        worker_snapshots=_worker_snapshots(request),
        timeline=timeline,
        recent_alerts=_recent_health_alerts(),
        window_hours=hours,
    )


def _suggest_assignee(request: Request, config_manager):
    """为运维事件挑选「值班建议处理人」：最闲的在线坐席（复用 AssignmentService）。

    事件无会话/语言上下文，故 conv=None，纯按在线 + 负载最轻选人；仅作 ack 预填提示，
    人可在弹窗覆盖。AssignmentService 是纯决策，不写任何认领。
    """
    inbox = getattr(request.app.state, "inbox_store", None)
    if inbox is None:
        return None
    try:
        from src.workspace.assignment import AssignmentService
        config = getattr(config_manager, "config", None) or {}
        svc = AssignmentService.from_config(config)
        presence = inbox.list_agent_presence() if hasattr(inbox, "list_agent_presence") else []
        claims = inbox.list_conversation_claims() if hasattr(inbox, "list_conversation_claims") else []
        return svc.suggest(presence=presence, claims=claims, conv=None)
    except Exception:
        logger.debug("值班建议计算失败（已忽略）", exc_info=True)
        return None


def register_ops_overview_routes(app, ctx) -> None:
    api_auth = ctx.api_auth
    api_write = ctx.api_write
    page_auth = ctx.page_auth
    templates = ctx.templates
    config_manager = ctx.config_manager
    audit_store = ctx.audit_store

    @app.get("/api/admin/ops-overview")
    async def api_ops_overview(request: Request, days: int = 7, month: str = "", hours: int = 24):
        """运营总览：业务 ROI + 计费 + 运行时健康 + 运维可靠性 + 事件，一页聚合。"""
        api_auth(request)
        from src.inbox.health_watchdog import collect_health
        from src.utils.ops_overview import assemble_ops_overview

        roi = billing = health = reliability = None
        auto_claim = None

        try:
            from src.web.routes.unified_inbox_roi import build_roi_summary
            roi = build_roi_summary(request, config_manager, span=int(days or 7))
        except Exception:
            logger.debug("ROI 聚合失败（已忽略）", exc_info=True)
        try:
            from src.web.routes.unified_inbox_usage_routes import build_billing_statement
            billing = build_billing_statement(request, month or "")
        except Exception:
            logger.debug("计费聚合失败（已忽略）", exc_info=True)
        try:
            health = collect_health(request.app, config_manager)
        except Exception:
            logger.debug("健康聚合失败（已忽略）", exc_info=True)
        try:
            reliability = _reliability_payload(request, hours)
        except Exception:
            logger.debug("可靠性聚合失败（已忽略）", exc_info=True)

        inbox = getattr(request.app.state, "inbox_store", None)
        open_incidents = 0
        if inbox is not None and hasattr(inbox, "count_open_incidents"):
            try:
                open_incidents = inbox.count_open_incidents()
            except Exception:
                logger.debug("未关闭事件计数失败（已忽略）", exc_info=True)
        if inbox is not None and hasattr(inbox, "get_auto_claim_stats"):
            try:
                auto_claim = inbox.get_auto_claim_stats(time.time() - int(days or 7) * 86400)
            except Exception:
                logger.debug("自动认领统计失败（已忽略）", exc_info=True)

        companion = None
        try:
            from src.web.routes.companion_capability_routes import gather_companion_advice
            cfg = getattr(config_manager, "config", None)
            if isinstance(cfg, dict):
                companion = gather_companion_advice(request.app.state, cfg)
        except Exception:
            logger.debug("陪伴能力配置体检聚合失败（已忽略）", exc_info=True)

        overview = assemble_ops_overview(
            roi=roi, billing=billing, health=health, reliability=reliability,
            auto_claim=auto_claim, open_incidents=open_incidents,
            companion=companion,
        )
        # G2：趋势异动标注（AI 发送量 / 处置量）。
        from src.utils.ops_intel import detect_trend_anomaly
        ai_trend = (((roi or {}).get("automation") or {}).get("trend")) or []
        rel_trend = ((reliability or {}).get("trend")) or []
        # 末桶为「当前未走完」时段（今天 / 当前小时），drop_last 丢弃以免半桶误报。
        overview["anomalies"] = {
            "ai_sent": detect_trend_anomaly([p.get("ai") for p in ai_trend], drop_last=True),
            "dispositions": detect_trend_anomaly([p.get("total") for p in rel_trend], drop_last=True),
        }
        return overview

    @app.get("/api/admin/ops-report")
    async def api_ops_report(request: Request, days: int = 7):
        """G3 运营周报：近 N 天事件统计(MTTR) + 自动化/业务/可靠性/计费 摘要。"""
        api_auth(request)
        from src.utils.ops_intel import build_ops_report

        span = int(days or 7)
        inbox = getattr(request.app.state, "inbox_store", None)
        incident_stats = None
        if inbox is not None and hasattr(inbox, "get_incident_stats"):
            try:
                incident_stats = inbox.get_incident_stats(time.time() - span * 86400)
            except Exception:
                logger.debug("事件统计失败（已忽略）", exc_info=True)
        roi = reliability = billing = None
        try:
            from src.web.routes.unified_inbox_roi import build_roi_summary
            roi = build_roi_summary(request, config_manager, span=span)
        except Exception:
            logger.debug("ROI 周报段失败（已忽略）", exc_info=True)
        try:
            reliability = _reliability_payload(request, span * 24)
        except Exception:
            logger.debug("可靠性周报段失败（已忽略）", exc_info=True)
        try:
            from src.web.routes.unified_inbox_usage_routes import build_billing_statement
            billing = build_billing_statement(request, "")
        except Exception:
            logger.debug("计费周报段失败（已忽略）", exc_info=True)
        return build_ops_report(days=span, incident_stats=incident_stats,
                                roi=roi, reliability=reliability, billing=billing)

    @app.get("/api/admin/incidents")
    async def api_list_incidents(request: Request, status: str = "", kind: str = "",
                                 limit: int = 50, before_id: int = 0):
        """列运维事件（E2）。status∈open|acked|resolved；kind∈health|billing；
        before_id 游标分页回看历史。返回 next_cursor（无更多则 None）。"""
        api_auth(request)
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "list_incidents"):
            return {"ok": True, "incidents": [], "open": 0,
                    "suggested_assignee": None, "next_cursor": None}
        from src.utils.ops_intel import incident_advice
        lim = int(limit or 50)
        items = inbox.list_incidents(status=status or "", kind=kind or "",
                                     limit=lim, before_id=int(before_id or 0))
        for it in items:
            it["advice"] = incident_advice(it.get("problems"))
        open_n = inbox.count_open_incidents() if hasattr(inbox, "count_open_incidents") else 0
        suggested = None
        if open_n:
            suggested = _suggest_assignee(request, config_manager)
        # 取满一页则给下一页游标（最后一条的 id）；不足一页说明到底了。
        next_cursor = items[-1]["id"] if len(items) >= lim and items else None
        return {"ok": True, "incidents": items, "open": open_n,
                "suggested_assignee": suggested, "next_cursor": next_cursor}

    @app.post("/api/admin/incidents/{incident_id}/ack")
    async def api_ack_incident(incident_id: int, request: Request,
                               _=Depends(api_write("manage_ops"))):
        """确认/指派一条运维事件（E2）。body 可选 {assigned_to}。需 manage_ops 写权限。"""
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "ack_incident"):
            return JSONResponse({"ok": False, "error": "store_unavailable"}, status_code=503)
        assigned_to = ""
        try:
            body = await request.json()
            assigned_to = str((body or {}).get("assigned_to") or "")
        except Exception:
            assigned_to = ""
        ok = inbox.ack_incident(int(incident_id), assigned_to=assigned_to)
        # 审计留痕：谁在何时 ack/指派了哪条事件（合规可追溯）。
        if ok and audit_store is not None:
            try:
                actor = request.session.get("username", "api")
                audit_store.log(actor, "ack_incident",
                                f"incident:{int(incident_id)}", "", assigned_to)
            except Exception:
                logger.debug("ack 审计写入失败（已忽略）", exc_info=True)
        return {"ok": bool(ok), "incident_id": int(incident_id)}

    # H2：根因建议的「一键动作」——可重置 worker 标识 → app.state 属性。
    _WORKER_ATTRS = {"autosend": "autosend_worker", "autoclaim": "auto_claim_worker"}

    @app.post("/api/admin/workers/{worker_id}/reset-circuit")
    async def api_reset_worker_circuit(worker_id: str, request: Request,
                                       _=Depends(api_write("manage_ops"))):
        """重置某 worker 的熔断器（H2 一键动作）。需 manage_ops；写审计。"""
        attr = _WORKER_ATTRS.get(str(worker_id or ""))
        worker = getattr(request.app.state, attr, None) if attr else None
        if worker is None or not hasattr(worker, "reset_circuit"):
            return JSONResponse({"ok": False, "error": "worker_unavailable"}, status_code=404)
        was_open = bool(worker.reset_circuit())
        if audit_store is not None:
            try:
                actor = request.session.get("username", "api")
                audit_store.log(actor, "reset_circuit", f"worker:{worker_id}", "",
                                "was_open" if was_open else "noop")
            except Exception:
                logger.debug("reset_circuit 审计写入失败（已忽略）", exc_info=True)
        return {"ok": True, "worker_id": worker_id, "was_open": was_open}

    @app.post("/api/admin/health/recheck")
    async def api_health_recheck(request: Request, _=Depends(api_write("manage_ops"))):
        """立即重巡运行时健康（H2 一键动作）：修复后点一下即可让事件自动开/关。

        有 watchdog 时调 recheck()（会即时落表/恢复事件）；否则退化为只读 collect_health。
        """
        import asyncio

        from src.inbox.health_watchdog import collect_health
        wd = getattr(request.app.state, "health_watchdog", None)
        try:
            if wd is not None and hasattr(wd, "recheck"):
                health = await asyncio.get_event_loop().run_in_executor(None, wd.recheck)
            else:
                health = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: collect_health(request.app, config_manager))
        except Exception:
            logger.debug("健康重巡失败（已忽略）", exc_info=True)
            return JSONResponse({"ok": False, "error": "recheck_failed"}, status_code=500)
        if audit_store is not None:
            try:
                actor = request.session.get("username", "api")
                audit_store.log(actor, "health_recheck", "health", "",
                                str(health.get("light") or ""))
            except Exception:
                logger.debug("health_recheck 审计写入失败（已忽略）", exc_info=True)
        return {"ok": True, "light": health.get("light"), "summary": health.get("summary")}

    @app.get("/api/admin/tts-cost-trend")
    async def api_tts_cost_trend(request: Request, days: int = 7):
        """P4-B：近 N 天 TTS 花费/缓存命中按日聚合（供看板画曲线）。

        未开启成本落库（voice_routing.cost_log.enabled=false）→ 返回 enabled:false + 空序列，
        前端据此隐藏曲线、仅显示当下快照。
        """
        api_auth(request)
        try:
            from src.ai.tts_cost_store import get_tts_cost_store
            store = get_tts_cost_store()
            if store is None:
                return {"ok": True, "enabled": False, "days": []}
            span = int(days or 7)
            try:
                store.prune()   # 顺手清理过期日聚合（主管低频访问，开销可忽略）
            except Exception:
                logger.debug("tts_cost prune 失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": True, "days": store.daily(days=span)}
        except Exception:
            logger.debug("tts-cost-trend 读取失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False, "days": []}

    @app.get("/api/admin/translation-confidence-trend")
    async def api_translation_confidence_trend(request: Request, days: int = 7):
        """S：近 N 天翻译低置信率/切换率按日聚合（供看板画 sparkline）。

        未开启趋势落库（translation.engines.confidence_switch.trend_log=false）→
        返回 enabled:false + 空序列，前端据此隐藏曲线、仅显示当下快照（M 的瞬时值）。
        """
        api_auth(request)
        try:
            from src.ai.translation_trend_store import get_translation_trend_store
            store = get_translation_trend_store()
            if store is None:
                return {"ok": True, "enabled": False, "days": []}
            span = int(days or 7)
            try:
                store.prune()
            except Exception:
                logger.debug("xlate_trend prune 失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": True, "days": store.daily(days=span)}
        except Exception:
            logger.debug("translation-confidence-trend 读取失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False, "days": []}

    @app.get("/admin/ops", response_class=HTMLResponse)
    async def ops_overview_page(request: Request, _=Depends(page_auth)):
        # 计算当前用户是否有 manage_ops 写权限，供模板决定是否渲染「确认」按钮。
        can_manage = False
        try:
            from src.utils.web_user_store import ROLE_MASTER
            role = request.session.get("role", "")
            if not role and ctx.token and request.session.get("auth") == ctx.token:
                role = ROLE_MASTER
            us = ctx.user_store
            can_manage = bool(us and us.can_write(role, "manage_ops"))
        except Exception:
            logger.debug("manage_ops 权限判定失败（已忽略）", exc_info=True)
        return templates.TemplateResponse(
            request, "ops_overview.html",
            {"request": request, "can_manage_ops": can_manage},
        )
