"""统一草稿/审批路由（Phase B / B2）。

API 端点（register_drafts_routes — main.py 调用）：
  GET  /api/drafts                          ?status=pending&platform=&limit=50
  GET  /api/drafts/stats                    — 按平台×状态计数
  GET  /api/drafts/risk-summary             — 待处理草稿按 autopilot_level 分布（B2）
  GET  /api/drafts/audit                    — 草稿处置审计日志（B2；主管专属）
  GET  /api/drafts/autosend-status          — AutosendWorker 运行指标（Phase A）
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
    async def api_drafts_risk_summary(
        request: Request, sla_hours: int = 4, _=Depends(api_auth),
    ):
        """L0–L4 分布统计（供仪表盘风险看板轮询）。含 sla_overdue 字段（D1）。"""
        svc = _get_draft_service(request)
        summary = svc.risk_summary()
        # D1：追加 SLA 过期数量（主管可见；非主管返回 -1 表示无权限）
        if _is_supervisor(request):
            threshold_ts = time.time() - max(1, min(72, int(sla_hours or 4))) * 3600
            drafts = svc.list_drafts(status="pending", limit=200)
            sla_overdue = sum(
                1 for d in drafts
                if d.get("autopilot_level") in {"L3", "L4"}
                and float(d.get("created_ts") or 0) > 0
                and float(d.get("created_ts") or 0) < threshold_ts
            )
            summary["sla_overdue"] = sla_overdue
        else:
            summary["sla_overdue"] = -1
        return {"ok": True, **summary}

    @app.get("/api/drafts/autosend-status")
    async def api_drafts_autosend_status(request: Request, _=Depends(api_auth)):
        """AutosendWorker 运行时指标（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限")
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None:
            return {"ok": True, "worker": None, "note": "AutosendWorker 未启用"}
        return {"ok": True, "worker": worker.status_snapshot()}

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

    # ── C2 / D1 端点必须注册在 /{draft_id} 之前，防止被通配路由截获 ──

    @app.get("/api/workspace/copilot")
    async def api_workspace_copilot(
        request: Request,
        text: str = "",
        draft_id: str = "",
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        """AI Copilot：对客户来文做规则层全量分析 + KB 匹配（<10ms，无 LLM）。

        返回：intent, emotion, risk_level, risk_reasons, next_step, kb_matches, language
        用于 draft_review.html 内嵌 AI 洞察面板。
        """
        from src.ai.chat_assistant_service import (
            quick_analyze, _suggestions, detect_language,
            _detect_emotion, _detect_intent, _detect_risk,
        )
        t = str(text or "")
        analysis = quick_analyze(t)
        # F1：附带规则建议文本（最多3条），供前端快捷回复按钮展示
        suggestions: list = []
        if t.strip():
            try:
                lang = analysis.get("language", "zh")
                intent = analysis.get("intent", "")
                emotion = analysis.get("emotion", "平稳")
                risk = analysis.get("risk_level", "low")
                for s in _suggestions(t, lang=lang, intent=intent, emotion=emotion, risk=risk)[:3]:
                    suggestions.append({
                        "style": str(s.style or ""),
                        "title": str(s.title or ""),
                        "text": str(s.text or ""),
                    })
            except Exception:
                pass
        # KB 匹配（可选，kb_store 未挂载时返回空列表）
        kb_matches: list = []
        try:
            kb_store = getattr(request.app.state, "kb_store", None)
            if kb_store is not None and t.strip():
                result = kb_store.search(t, top_k=3)
                raw_entries = (result or {}).get("entries", [])
                for e in raw_entries[:3]:
                    kb_matches.append({
                        "entry_id": str(e.get("entry_id") or e.get("id") or ""),
                        "title": str(e.get("title") or ""),
                        "summary": str(e.get("summary") or e.get("answer") or "")[:120],
                        "score": float(e.get("score") or 0),
                    })
        except Exception:
            pass
        return {
            "ok": True,
            "draft_id": draft_id,
            "conversation_id": conversation_id,
            **analysis,
            "suggestions": suggestions,
            "kb_matches": kb_matches,
        }

    @app.get("/api/drafts/sla-overdue")
    async def api_drafts_sla_overdue(
        request: Request,
        hours: int = 4,
        _=Depends(api_auth),
    ):
        """列出 L3/L4 草稿中超过 SLA 时限（默认 4h）的待审草稿（主管专属）。

        用于顶栏 SLA 角标 + 草稿审批页 SLA 徽章。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限")
        svc = _get_draft_service(request)
        threshold_ts = time.time() - max(1, min(72, int(hours or 4))) * 3600
        drafts = svc.list_drafts(status="pending", limit=200)
        overdue = [
            d for d in drafts
            if d.get("autopilot_level") in {"L3", "L4"}
            and float(d.get("created_ts") or 0) > 0
            and float(d.get("created_ts") or 0) < threshold_ts
        ]
        return {
            "ok": True,
            "count": len(overdue),
            "sla_hours": int(hours),
            "overdue": overdue,
        }

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


# ── C1：坐席绩效 API（不依赖 draft_service，直读 inbox_store） ──────────────

def register_agent_perf_routes(app, *, api_auth, page_auth, templates, config_manager=None):
    """坐席绩效看板：API + 页面路由（admin.py 调用）。

    GET /api/workspace/agent-perf        — 每坐席聚合指标（主管专属）
    GET /api/workspace/agent-perf/timeline — 趋势数据（主管专属）
    GET /workspace/agent-perf            — 绩效看板页面（主管专属）
    """
    import time as _time
    from fastapi import Depends
    from fastapi.responses import HTMLResponse, RedirectResponse

    def _get_store(request):
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            from fastapi import HTTPException
            raise HTTPException(503, "InboxStore 未挂载")
        return store

    def _ctx(request) -> dict:
        try:
            sess = request.session
        except (AttributeError, AssertionError):
            sess = {}
        ctx: dict = {
            "user_name": sess.get("username") or "",
            "user_display_name": sess.get("display_name") or sess.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa["site_name"]
        except Exception:
            pass
        return ctx

    @app.get("/api/workspace/agent-perf")
    async def api_agent_perf(
        request: Request,
        days: int = 30,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """每坐席草稿处置聚合绩效（主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, "需要主管权限")
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 30))) * 86400
        rows = store.get_agent_perf(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "agents": rows, "days": int(days), "total_agents": len(rows)}

    @app.get("/api/workspace/agent-perf/timeline")
    async def api_agent_perf_timeline(
        request: Request,
        days: int = 14,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """坐席绩效趋势（按天分桶；主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, "需要主管权限")
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 14))) * 86400
        timeline = store.get_agent_perf_timeline(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "timeline": timeline, "days": int(days)}

    @app.get("/workspace/agent-perf", response_class=HTMLResponse)
    async def workspace_agent_perf_page(request: Request, _=Depends(page_auth)):
        """坐席绩效看板（主管专属；非主管重定向到工作台）。"""
        if not _is_supervisor(request):
            return RedirectResponse(url="/workspace", status_code=302)
        return templates.TemplateResponse(request, "agent_perf.html", _ctx(request))


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
