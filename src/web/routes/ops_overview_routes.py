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
from typing import Optional

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

    @app.get("/api/admin/realtime-voice-trend")
    async def api_realtime_voice_trend(request: Request, days: int = 7):
        """E 线：近 N 天实时语音接通率/健康率按日聚合（供看板 sparkline）。

        未开启 ``realtime_voice.trend_log`` → enabled:false + 空序列。
        """
        api_auth(request)
        cfg = getattr(config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False):
            return {"ok": True, "enabled": False, "days": []}
        try:
            from src.ai.realtime_voice_trend_store import get_realtime_voice_trend_store
            store = get_realtime_voice_trend_store()
            if store is None:
                return {"ok": True, "enabled": False, "days": []}
            span = int(days or 7)
            try:
                store.prune()
            except Exception:
                logger.debug("rtv_trend prune 失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": True, "days": store.daily(days=span)}
        except Exception:
            logger.debug("realtime-voice-trend 读取失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False, "days": []}

    @app.get("/api/admin/ai-safety-overview")
    async def api_ai_safety_overview(request: Request, days: int = 7):
        """AI 安全/质量总览：复用 draft_audit_log 聚合草稿处置动作 + 风险分级，回答
        「AI 自动发的靠不靠谱（采纳/改写/弃用率 + 拦截）」+「风险边缘量」。纯读、无新存储。

        无 inbox_store / 无 ai_safety_summary → enabled:false（前端隐藏卡）。
        """
        api_auth(request)
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "ai_safety_summary"):
            return {"ok": True, "enabled": False}
        try:
            span = int(days or 7)
            now = time.time()
            window = span * 86400
            cur = inbox.ai_safety_summary(since_ts=now - window, include_trend=True)
            # 环比：上一个等长窗口 [now-2w, now-w)（半开区间，不重叠）。
            prev = inbox.ai_safety_summary(since_ts=now - 2 * window, until_ts=now - window)
            delta = {
                k: round((cur.get(k, 0) or 0) - (prev.get(k, 0) or 0), 3)
                for k in ("adopt_rate", "edit_rate", "reject_rate", "autosend", "blocked", "reviewed")
            }
            blocked_top = []
            if hasattr(inbox, "top_blocked_conversations"):
                try:
                    blocked_top = inbox.top_blocked_conversations(since_ts=now - window, limit=8)
                except Exception:
                    logger.debug("top_blocked_conversations 读取失败（已忽略）", exc_info=True)
            drilldown = {}
            if hasattr(inbox, "deep_link_stats"):
                try:
                    drilldown = inbox.deep_link_stats(source="ai_safety", since_ts=now - window)
                except Exception:
                    logger.debug("deep_link_stats 读取失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": True, "days": span,
                    "prev": prev, "delta": delta, "blocked_top": blocked_top,
                    "drilldown": drilldown, **cur}
        except Exception:
            logger.debug("ai-safety-overview 读取失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False}

    # E3：AI 安全看板下钻等轻量 UI 遥测事件的落点（观测「看板有没有人真去用」）。
    # 同源会话鉴权即可（低风险计数写）；event 白名单防表被任意写入撑大。
    _UI_EVENTS = {"deep_link_opened"}

    @app.post("/api/admin/ui-event")
    async def api_ui_event(request: Request):
        api_auth(request)
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "record_ui_event"):
            return {"ok": False, "enabled": False}
        try:
            body = await request.json()
        except Exception:
            body = {}
        event = str((body or {}).get("event") or "")
        if event not in _UI_EVENTS:
            return {"ok": False, "error": "unknown_event"}
        try:
            inbox.record_ui_event(
                event,
                source=str((body or {}).get("source") or ""),
                conversation_id=str((body or {}).get("conversation_id") or ""),
            )
        except Exception:
            logger.debug("record_ui_event 失败（已忽略）", exc_info=True)
        return {"ok": True}

    @app.get("/api/admin/ai-quality-calibrate")
    async def api_ai_quality_calibrate(
        request: Request, days: int = 30,
        adopt_min: Optional[float] = None, adopt_severe: Optional[float] = None,
        reject_max: Optional[float] = None, reject_severe: Optional[float] = None,
        high_risk_min: Optional[int] = None, high_risk_spike: Optional[int] = None,
        min_samples: Optional[int] = None,
    ):
        """F2b 阈值校准：用真实近 ``days`` 天历史滚动 window_days 窗口，复刻 AI 质量告警评估，
        反推「按当前（或 query what-if 覆盖）阈值会告警几次 / 分布如何」——上线前定阈值再开哨兵。

        阈值：配置 ``inbox.ai_quality_alert`` 打底，query 参数（adopt_min/reject_max/… 任选）
        覆盖做 what-if。纯读，缺 store/方法 → enabled:false。
        """
        api_auth(request)
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "ai_quality_daily_series"):
            return {"ok": True, "enabled": False}
        from src.utils.ai_quality_alert import (
            DEFAULT_AI_QUALITY_THRESHOLDS, calibrate_ai_quality,
        )
        cfg = getattr(config_manager, "config", None) or {}
        aq = ((((cfg.get("inbox") or {}).get("ai_quality_alert")) or {})
              if isinstance(cfg, dict) else {})
        # 配置阈值打底 + query what-if 覆盖（None 忽略）。
        thresholds = {k: aq[k] for k in DEFAULT_AI_QUALITY_THRESHOLDS
                      if k in aq and aq[k] is not None}
        for key, val in (("adopt_min", adopt_min), ("adopt_severe", adopt_severe),
                         ("reject_max", reject_max), ("reject_severe", reject_severe),
                         ("high_risk_min", high_risk_min), ("high_risk_spike", high_risk_spike),
                         ("min_samples", min_samples)):
            if val is not None:
                thresholds[key] = val
        window_days = int(aq.get("window_days", 7) or 7)
        try:
            series = inbox.ai_quality_daily_series(days=int(days or 30))
            report = calibrate_ai_quality(series, thresholds, window_days=window_days)
        except Exception:
            logger.debug("ai-quality-calibrate 计算失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False}
        return {"ok": True, "enabled": True, "days": int(days or 30),
                "configured_enabled": bool(aq.get("enabled", False)),
                "thresholds": {**DEFAULT_AI_QUALITY_THRESHOLDS, **thresholds},
                **report}

    @app.post("/api/admin/ai-quality-thresholds")
    async def api_ai_quality_thresholds(request: Request,
                                        _=Depends(api_write("manage_ops"))):
        """F2b++：把 what-if 校准满意的阈值一键写入 ``config.local.yaml`` overlay（治理化、
        保主配置注释、可回滚、即时生效），补上「定完阈值还得手动抄进配置」的手工缝。

        body: ``{thresholds:{adopt_min,…}, enable?:bool}``。阈值经
        :func:`sanitize_ai_quality_thresholds` 白名单+强制类型+越界丢弃（防注入任意键）；
        逐键过 ``ConfigManager.set_overlay_flag``（与能力开关同机制）。``enable`` 给定时同步
        开/关哨兵主开关。返回 ``{ok, applied:[{key,value}], enabled}``。
        """
        if not hasattr(config_manager, "set_overlay_flag"):
            return {"ok": False, "message": "配置写入能力不可用（请升级 ConfigManager）"}
        from src.utils.ai_quality_alert import sanitize_ai_quality_thresholds
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        clean = sanitize_ai_quality_thresholds(body.get("thresholds"))
        applied = []
        for key, val in clean.items():
            ok, _msg = config_manager.set_overlay_flag(
                f"inbox.ai_quality_alert.{key}", val)
            if ok:
                applied.append({"key": key, "value": val})
        enabled = None
        if "enable" in body:
            enabled = bool(body.get("enable"))
            config_manager.set_overlay_flag("inbox.ai_quality_alert.enabled", enabled)
        if audit_store is not None:
            try:
                actor = request.session.get("username", "api")
                audit_store.log(actor, "ai_quality_thresholds", "ops", "",
                                f"applied={len(applied)} enable={enabled}")
            except Exception:
                logger.debug("ai_quality_thresholds 审计写入失败（已忽略）", exc_info=True)
        return {"ok": True, "applied": applied, "enabled": enabled}

    @app.get("/api/admin/realtime-voice-alert-calibrate")
    async def api_realtime_voice_alert_calibrate(
        request: Request,
        min_attempts: Optional[int] = None,
        min_health_probes: Optional[int] = None,
        health_ok_rate_warn: Optional[float] = None,
        health_ok_rate_fail: Optional[float] = None,
        connect_rate_warn: Optional[float] = None,
        connect_rate_fail: Optional[float] = None,
    ):
        """D 线：实时语音告警阈值 what-if——读进程 ``RealtimeVoiceStats`` + 配置阈值，
        复刻 watchdog 评估，看「按此阈值现在/历史会告警吗」。纯读；功能未开 → enabled:false。
        """
        api_auth(request)
        cfg = getattr(config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False):
            return {"ok": True, "enabled": False, "reason": "feature_disabled"}
        from src.ai.realtime_voice_stats import get_realtime_voice_stats
        from src.utils.realtime_voice_alert import (
            DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS,
            calibrate_realtime_voice_alert,
        )
        alert_cfg = (rtv.get("alert") or {}) if isinstance(rtv, dict) else {}
        thresholds = {k: alert_cfg[k] for k in DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS
                        if k in alert_cfg and alert_cfg[k] is not None}
        for key, val in (
            ("min_attempts", min_attempts),
            ("min_health_probes", min_health_probes),
            ("health_ok_rate_warn", health_ok_rate_warn),
            ("health_ok_rate_fail", health_ok_rate_fail),
            ("connect_rate_warn", connect_rate_warn),
            ("connect_rate_fail", connect_rate_fail),
        ):
            if val is not None:
                thresholds[key] = val
        try:
            stats = get_realtime_voice_stats().dump()
            daily = None
            trend_enabled = False
            try:
                from src.ai.realtime_voice_trend_store import get_realtime_voice_trend_store
                trend_store = get_realtime_voice_trend_store()
                if trend_store is not None:
                    trend_enabled = True
                    try:
                        trend_store.prune()
                    except Exception:
                        pass
                    daily = trend_store.daily_for_calibrate(days=7)
            except Exception:
                pass
            report = calibrate_realtime_voice_alert(
                stats, thresholds, daily=daily if daily else None)
        except Exception:
            logger.debug("realtime-voice-alert-calibrate 计算失败（已忽略）", exc_info=True)
            return {"ok": True, "enabled": False}
        return {
            "ok": True,
            "enabled": True,
            "configured_enabled": bool(alert_cfg.get("enabled", False)),
            "trend_enabled": trend_enabled,
            "thresholds": {**DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS, **thresholds},
            **report,
        }

    @app.post("/api/admin/realtime-voice-alert-thresholds")
    async def api_realtime_voice_alert_thresholds(request: Request,
                                                  _=Depends(api_write("manage_ops"))):
        """D 线++：what-if 满意后一键写入 ``config.local.yaml`` overlay（``realtime_voice.alert.*``）。"""
        if not hasattr(config_manager, "set_overlay_flag"):
            return {"ok": False, "message": "配置写入能力不可用（请升级 ConfigManager）"}
        from src.utils.realtime_voice_alert import sanitize_realtime_voice_alert_thresholds
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        clean = sanitize_realtime_voice_alert_thresholds(body.get("thresholds"))
        applied = []
        for key, val in clean.items():
            ok, _msg = config_manager.set_overlay_flag(
                f"realtime_voice.alert.{key}", val)
            if ok:
                applied.append({"key": key, "value": val})
        enabled = None
        if "enable" in body:
            enabled = bool(body.get("enable"))
            config_manager.set_overlay_flag("realtime_voice.alert.enabled", enabled)
        if audit_store is not None:
            try:
                actor = request.session.get("username", "api")
                audit_store.log(actor, "realtime_voice_alert_thresholds", "ops", "",
                                f"applied={len(applied)} enable={enabled}")
            except Exception:
                logger.debug("realtime_voice_alert_thresholds 审计写入失败（已忽略）", exc_info=True)
        return {"ok": True, "applied": applied, "enabled": enabled}

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
