"""统一草稿/审批路由（Phase B / B2）。

API 端点（register_drafts_routes — main.py 调用）：
  GET  /api/drafts                          ?status=pending&platform=&limit=50
  GET  /api/drafts/stats                    — 按平台×状态计数
  GET  /api/drafts/risk-summary             — 待处理草稿按 autopilot_level 分布（B2）
  GET  /api/drafts/audit                    — 草稿处置审计日志（B2；主管专属）
  GET  /api/drafts/{draft_id}               — 单条草稿
  POST /api/drafts/{draft_id}/resolve       — 带 L4 拦截 + 审计的统一处置（B2）
  POST /api/drafts/{draft_id}/force-override — 主管强制放行 L4 草稿（B2）
  POST /api/drafts/bulk-autosend            — 批量触发所有 L2 草稿自动发送（B2）

页面路由（register_drafts_page_routes — admin.py 调用）：
  GET  /workspace/drafts         — 草稿审批工作台（坐席/主管均可，L4 需主管放行）
  GET  /workspace/draft-audit    — 审计日志页（主管专属）

依赖 app.state.draft_service（main.py 注入）。未注入时端点返回 503。
"""

from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request

# 主管角色集（与 unified_inbox_routes 保持一致）
_SUPERVISOR_ROLES = {"master", "admin"}


def _get_draft_service(request: Request):
    svc = getattr(request.app.state, "draft_service", None)
    if svc is None:
        raise HTTPException(503, "草稿服务未启用")
    return svc


def _session_role(request: Request) -> str:
    """从 session 读 role（与 unified_inbox_routes._session_agent 对齐）。"""
    try:
        sess = request.session  # may raise if no SessionMiddleware
    except (AttributeError, AssertionError):
        sess = {}
    if not sess:
        sess = request.scope.get("session", {})
    return str(sess.get("role") or "")


def _session_agent_id(request: Request) -> str:
    try:
        sess = request.session
    except (AttributeError, AssertionError):
        sess = {}
    if not sess:
        sess = request.scope.get("session", {})
    uid = sess.get("user_id") or sess.get("username") or ""
    return str(uid)


def _is_supervisor(request: Request) -> bool:
    return _session_role(request) in _SUPERVISOR_ROLES


def register_drafts_routes(app, *, api_auth):
    """挂载统一草稿路由（B2 增强版）。"""

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

    @app.get("/api/drafts/risk-summary")
    async def api_drafts_risk_summary(request: Request, _=Depends(api_auth)):
        """L0–L4 分布统计（供仪表盘风险看板轮询）。"""
        svc = _get_draft_service(request)
        return {"ok": True, **svc.risk_summary()}

    @app.get("/api/drafts/audit")
    async def api_drafts_audit(
        request: Request,
        draft_id: str = "",
        agent_id: str = "",
        days: int = 7,
        limit: int = 200,
        _=Depends(api_auth),
    ):
        """草稿处置审计日志（主管专属）。可按 draft_id / agent_id / 天数过滤。"""
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限")
        svc = _get_draft_service(request)
        since_ts = time.time() - max(1, min(90, int(days or 7))) * 86400
        items = svc.list_audit(
            draft_id=draft_id or "",
            agent_id=agent_id or "",
            since_ts=since_ts,
            limit=max(1, min(500, int(limit or 200))),
        )
        return {"ok": True, "items": items, "total": len(items)}

    @app.get("/api/drafts/{draft_id}")
    async def api_drafts_get(request: Request, draft_id: str, _=Depends(api_auth)):
        svc = _get_draft_service(request)
        draft = svc.get_draft(draft_id)
        if draft is None:
            raise HTTPException(404, "草稿不存在")
        return {"ok": True, "draft": draft}

    @app.post("/api/drafts/{draft_id}/resolve")
    async def api_drafts_resolve(request: Request, draft_id: str, _=Depends(api_auth)):
        """带 L4 拦截 + 敏感词强制升级 + 审计的统一处置（B2）。

        Body: {action, text?, by?}
        action: approve / reject / edit_send / cancel / autosend（L2 自动路径）
        """
        svc = _get_draft_service(request)
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        text = str(body.get("text") or "")
        by = str(body.get("by") or "") or _session_agent_id(request)
        result = svc.resolve_with_audit(draft_id, action, text=text, by=by)
        if not result.get("ok"):
            code = int(result.get("code") or 400)
            raise HTTPException(code, result.get("error") or "处置失败")
        return result

    @app.post("/api/drafts/{draft_id}/force-override")
    async def api_drafts_force_override(
        request: Request, draft_id: str, _=Depends(api_auth),
    ):
        """主管强制放行 L4 草稿（force_override=True）。主管专属。

        Body: {action?, text?, reason?}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限才能强制放行 L4 草稿")
        svc = _get_draft_service(request)
        body = await request.json()
        action = str(body.get("action") or "approve").strip().lower()
        text = str(body.get("text") or "")
        by = _session_agent_id(request) or str(body.get("by") or "")
        result = svc.resolve_with_audit(
            draft_id, action, text=text, by=by, force_override=True,
        )
        if not result.get("ok"):
            code = int(result.get("code") or 400)
            raise HTTPException(code, result.get("error") or "强制放行失败")
        return result

    @app.post("/api/drafts/bulk-autosend")
    async def api_drafts_bulk_autosend(
        request: Request, _=Depends(api_auth),
    ):
        """批量触发所有 L2（低风险 + auto_ai）草稿自动发送。

        适用场景：定时任务 / 坐席手动触发"一键自动发所有 L2"。
        返回 {ok, sent, errors}。
        """
        svc = _get_draft_service(request)
        by = _session_agent_id(request) or "system"
        drafts = svc.list_drafts(status="pending", limit=200)
        sent, errors = 0, 0
        for d in drafts:
            if d.get("autopilot_level") != "L2":
                continue
            result = svc.resolve_with_audit(
                d["draft_id"], "autosend", by=by,
            )
            if result.get("ok"):
                sent += 1
            else:
                errors += 1
        return {"ok": True, "sent": sent, "errors": errors}


# ── 页面路由（需 templates + page_auth，由 admin.py create_app 调用） ──────

def register_drafts_page_routes(
    app,
    *,
    page_auth,
    templates,
    config_manager=None,
):
    """挂载草稿审批工作台页面路由（需 Jinja2 templates + page_auth）。

    与 register_drafts_routes（API 路由）分离注册：
    - API 路由在 main.py 里 app 创建后追加（不依赖 templates）
    - 页面路由在 admin.py create_app 内调用（需 templates 和 page_auth）
    """
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _ctx(request: Request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": (
                sess.get("display_name") or sess.get("username") or ""
            ),
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    @app.get("/workspace/drafts", response_class=HTMLResponse)
    async def workspace_drafts_page(
        request: Request, _=Depends(page_auth),
    ):
        """草稿审批工作台（坐席/主管均可进；L4 需主管才能 force-override）。"""
        return templates.TemplateResponse(request, "draft_review.html", _ctx(request))

    @app.get("/workspace/draft-audit", response_class=HTMLResponse)
    async def workspace_draft_audit_page(
        request: Request, _=Depends(page_auth),
    ):
        """草稿处置审计日志页（主管专属；非主管重定向到草稿工作台）。"""
        if not _is_supervisor(request):
            return RedirectResponse(url="/workspace/drafts", status_code=302)
        return templates.TemplateResponse(
            request, "draft_audit_page.html", _ctx(request)
        )
