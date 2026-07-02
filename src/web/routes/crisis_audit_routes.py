"""R9b 危机事件审计 API 路由（从 R9 的 CrisisEventStore 暴露给值守人员）。

把 R9 落库的危机事件变成真人**可操作**的工作台：列未处理危机、标记已处置。
依赖经 AdminRouteContext 注入；读用 api_auth，写（标记处置）用 manage_ops 权限
（与"确认/指派运维事件"同级）。行为与既有审计类路由一致。
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from src.web.web_i18n import tr


def register_crisis_audit_routes(app, ctx) -> None:
    """挂载 /api/crisis-events* 到 app。"""
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _sm(request):
        sm = getattr(telegram_client, "skill_manager", None) if telegram_client else None
        if not sm:
            raise HTTPException(status_code=503, detail=tr(request, "err.epi.bot_not_ready_sm"))
        return sm

    @app.get("/api/crisis-events")
    async def api_crisis_events_list(
        request: Request,
        only_unhandled: bool = False,
        prefix: str = "",
        limit: int = 50,
    ):
        """危机事件列表（默认按时间倒序；only_unhandled=true 仅看未处理）。"""
        _api_auth(request)
        sm = _sm(request)
        lim = max(1, min(int(limit or 50), 500))
        items = sm.crisis_list_for_admin(
            limit=lim, only_unhandled=bool(only_unhandled), user_prefix=prefix[:120],
        )
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "unhandled_total": sm.crisis_count_for_admin(only_unhandled=True),
        }

    @app.post("/api/crisis-events/{event_id}/handle")
    async def api_crisis_event_handle(event_id: int, request: Request):
        """标记某危机事件为已人工处理（记录处理人 + 备注）。需 manage_ops 权限。"""
        _api_write("manage_ops")(request)
        sm = _sm(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        note = str(body.get("note", "") or "")[:500]
        handled_by = str(
            request.session.get("username")
            or request.session.get("role")
            or "admin"
        )
        ok = sm.crisis_mark_handled_for_admin(
            int(event_id), handled_by=handled_by, note=note,
        )
        if not ok:
            raise HTTPException(status_code=404, detail=tr(request, "err.ca.event_not_found"))
        return {"ok": True, "handled": int(event_id)}
