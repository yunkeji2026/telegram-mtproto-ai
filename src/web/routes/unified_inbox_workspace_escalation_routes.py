"""统一收件箱——坐席工作台 SLA告警/坐席身份/升级队列路由域（巨石拆分 slice 13）。

把"SLA 告警源 + 当前坐席身份 + 升级队列（escalations/mine/assign/log）"这一子域，从
``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_escalation_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用。
端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：sla 快照族（unified_inbox_sla）、auth 身份/主管权限、services._inbox_store；
只收 api_auth 一个参数（本域无 page/templates/config 需求）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.web.routes.unified_inbox_auth import (
    _is_supervisor,
    _require_supervisor,
    _session_agent,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.routes.unified_inbox_sla import _escalation_snapshot, _sla_alert_snapshot
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_workspace_escalation_routes(app, *, api_auth) -> None:
    """挂载 SLA 告警 / 坐席身份 / 升级队列端点（/api/workspace/sla-alerts|me|escalations*|escalation*）。"""

    @app.get("/api/workspace/sla-alerts")
    async def api_workspace_sla_alerts(request: Request):
        """SLA 告警源（顶栏徽标轮询 + 严重超时清单下钻）。"""
        api_auth(request)
        return _sla_alert_snapshot(request)

    @app.get("/api/workspace/me")
    async def api_workspace_me(request: Request):
        """当前坐席身份 + 角色能力（前端按 is_supervisor 显隐管理向 UI）。

        附带 C0-3 授权简况（read_only / state / 提示），供全局只读横幅消费。
        """
        api_auth(request)
        a = _session_agent(request)
        lic_brief = None
        try:
            from src.licensing import get_license_manager

            _st = get_license_manager().status()
            lic_brief = {
                "state": _st.state,
                "read_only": _st.read_only,
                "plan": _st.plan,
                "message": "；".join(_st.messages) if _st.read_only else "",
            }
        except Exception:
            logger.debug("授权简况读取失败（已忽略）", exc_info=True)
        demo_on = False
        try:
            from src.utils.demo_seeder import demo_status
            demo_on = bool(demo_status(_inbox_store(request)).get("present"))
        except Exception:
            logger.debug("demo 状态读取失败（已忽略）", exc_info=True)
        return {"ok": True, "agent_id": a["agent_id"],
                "display_name": a["display_name"], "role": a.get("role", ""),
                "is_supervisor": _is_supervisor(request),
                "license": lic_brief, "demo_mode": demo_on}

    @app.get("/api/workspace/escalations")
    async def api_workspace_escalations(request: Request):
        """升级告警源（无人有效处理的严重超时；全局口径，不受个人静默影响）。"""
        api_auth(request)
        return _escalation_snapshot(request)

    @app.get("/api/workspace/escalations/mine")
    async def api_workspace_escalations_mine(
        request: Request, days: int = 7,
    ):
        """我的指派升级列表（当前坐席被指派为责任主管的升级，含接管时延）。
        主管专属；非主管返回空列表（不报 403，前端可安全轮询）。
        """
        api_auth(request)
        if not _is_supervisor(request):
            return {"ok": True, "items": [], "total": 0}
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "items": [], "total": 0}
        agent_id = _session_agent(request)["agent_id"]
        since_ts = time.time() - int(max(1, min(90, days))) * 86400
        items = inbox.list_my_escalations(
            agent_id, since_ts=since_ts, limit=100)
        return {"ok": True, "items": items, "total": len(items)}

    @app.post("/api/workspace/escalation/{esc_id}/assign")
    async def api_workspace_escalation_assign(
        request: Request, esc_id: int,
    ):
        """主管手动将某条升级指派给另一位主管（reassign）。主管专属。
        Body JSON: {"agent_id": "<target_supervisor_agent_id>"}
        """
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json()
        target = str(body.get("agent_id") or "").strip()
        if not target:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="agent_id"))
        ok = inbox.set_escalation_assigned(esc_id, target)
        if not ok:
            raise HTTPException(404, tr(request, "err.ws.escalation_not_found", esc_id=esc_id))
        return {"ok": True, "esc_id": esc_id, "assigned_to": target}

    @app.get("/api/workspace/handoff-brief")
    async def api_workspace_handoff_brief(
        request: Request, conversation_id: str = "", reason: str = "",
    ):
        """M8 结构化转人工简报：客户画像（意图/情绪/风险/CSAT/摘要）+ 最近往来 + 亮点提醒。

        坐席接手前一键拉取，3 秒进入状态。任何已认证坐席可读（读取会话上下文）。
        store 缺失或会话无元数据时优雅降级为空画像（不报错）。
        """
        api_auth(request)
        from src.utils.handoff_brief import build_handoff_brief
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        inbox = _inbox_store(request)
        meta = None
        recent: List[Dict[str, Any]] = []
        if inbox is not None:
            try:
                meta = inbox.get_conv_meta(cid)
            except Exception:
                logger.debug("get_conv_meta 失败（已忽略）", exc_info=True)
            try:
                recent = inbox.list_recent_messages(cid, limit=12)
            except Exception:
                logger.debug("list_recent_messages 失败（已忽略）", exc_info=True)
        return build_handoff_brief(cid, meta, recent, reason=reason)

    @app.get("/api/workspace/escalation-log")
    async def api_workspace_escalation_log(request: Request, days: int = 7):
        """升级历史 + 接管时延（复盘安全网成效）：升级→首个人工接管。主管专属。"""
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "days": 7, "items": [], "stats": {}}
        span = 30 if int(days or 7) >= 30 else 7
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        convs = {str(c.get("conversation_id") or ""): c
                 for c in inbox.list_conversations(limit=500)}
        rows = inbox.escalation_takeovers(since, limit=500)
        taken_n = 0
        dly_sum = 0.0
        reasons: Dict[str, int] = {}
        items: List[Dict[str, Any]] = []
        for r in rows:
            c = convs.get(r["conversation_id"]) or {}
            delay = (int(r["taken_ts"] - r["ts"])
                     if r["taken_ts"] is not None else None)
            if delay is not None:
                taken_n += 1
                dly_sum += delay
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            items.append({
                **r,
                "platform": str(c.get("platform") or ""),
                "name": str(c.get("display_name") or c.get("chat_key")
                            or r["conversation_id"]),
                "takeover_sec": delay,
            })
        total = len(items)
        return {"ok": True, "days": span, "items": items, "stats": {
            "total": total, "taken": taken_n,
            "taken_rate": round(taken_n / total * 100, 1) if total else 0.0,
            "avg_takeover_sec": int(dly_sum / taken_n) if taken_n else 0,
            "reasons": reasons,
        }}

    # ── P0-companion：会话搁置（snooze）——「稍后再看」从待接管/超时队列临时移出 ─────
    @app.post("/api/workspace/conversation/{conversation_id}/snooze")
    async def api_workspace_conversation_snooze(
        request: Request, conversation_id: str,
    ):
        """把会话搁置 N 分钟（或到指定 until_ts）——从「待接管/超时告警」队列临时移出。

        Body JSON: ``{"minutes": 120}`` 或 ``{"until_ts": <epoch 秒>}``。
        到点自动重浮；客户期间再来消息则立即重浮。任何已认证坐席可操作自己在看的会话。
        """
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        body = await request.json()
        if body.get("until_ts") is not None:
            try:
                until_ts = float(body.get("until_ts"))
            except (TypeError, ValueError):
                raise HTTPException(400, tr(request, "err.ws.until_ts_invalid"))
        else:
            try:
                minutes = float(body.get("minutes") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, tr(request, "err.ws.minutes_invalid"))
            if minutes <= 0:
                raise HTTPException(400, tr(request, "err.ws.minutes_must_be_positive"))
            until_ts = time.time() + minutes * 60.0
        by = _session_agent(request)["agent_id"]
        snoozed = inbox.set_snooze(cid, until_ts, by=by)
        return {"ok": True, "conversation_id": cid, "snoozed": snoozed,
                "snooze_until": until_ts if snoozed else 0}

    @app.post("/api/workspace/conversation/{conversation_id}/unsnooze")
    async def api_workspace_conversation_unsnooze(
        request: Request, conversation_id: str,
    ):
        """立即取消搁置，会话回到「待接管/超时告警」队列。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="conversation_id"))
        inbox.clear_snooze(cid)
        return {"ok": True, "conversation_id": cid, "snoozed": False}

    @app.get("/api/workspace/snoozed")
    async def api_workspace_snoozed(request: Request):
        """当前搁置中的会话清单（含剩余秒），供「搁置中」视图。任何已认证坐席可读。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "items": [], "total": 0}
        items = inbox.list_snoozed(limit=200)
        return {"ok": True, "items": items, "total": len(items)}
