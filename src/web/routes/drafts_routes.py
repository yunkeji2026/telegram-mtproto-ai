"""统一草稿/审批路由（Phase B）。

端点：
  GET  /api/drafts                 ?status=pending&platform=&limit=50  — 跨平台统一草稿列表
  GET  /api/drafts/stats           — 按平台×状态计数
  GET  /api/drafts/{draft_id}      — 单条草稿
  POST /api/drafts/{draft_id}/resolve  {action, text?, by?} — 统一处置（派发回各平台）

依赖 app.state.draft_service（main.py 注入）。未注入时端点返回 503。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request


def _get_draft_service(request: Request):
    svc = getattr(request.app.state, "draft_service", None)
    if svc is None:
        raise HTTPException(503, "草稿服务未启用")
    return svc


def register_drafts_routes(app, *, api_auth):
    """挂载统一草稿路由。api_auth 为可调用鉴权依赖（与其它路由一致）。"""

    @app.get("/api/drafts")
    async def api_drafts_list(
        request: Request,
        status: str = "pending",
        platform: str = "",
        limit: int = 50,
        _=Depends(api_auth),
    ):
        svc = _get_draft_service(request)
        limit = max(1, min(200, int(limit or 50)))
        drafts = svc.list_drafts(status=status or "", platform=platform or "", limit=limit)
        return {"ok": True, "count": len(drafts), "drafts": drafts}

    @app.get("/api/drafts/stats")
    async def api_drafts_stats(request: Request, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        return {"ok": True, "stats": svc.stats()}

    @app.get("/api/drafts/{draft_id}")
    async def api_drafts_get(request: Request, draft_id: str, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        draft = svc.get_draft(draft_id)
        if draft is None:
            raise HTTPException(404, "草稿不存在")
        return {"ok": True, "draft": draft}

    @app.post("/api/drafts/{draft_id}/resolve")
    async def api_drafts_resolve(request: Request, draft_id: str, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        text = str(body.get("text") or "")
        by = str(body.get("by") or "")
        result = svc.resolve(draft_id, action, text=text, by=by)
        if not result.get("ok"):
            raise HTTPException(int(result.get("code") or 400), result.get("error") or "处置失败")
        return result
