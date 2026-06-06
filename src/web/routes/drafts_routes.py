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

import logging
import time

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger(__name__)

# 主管角色集（与 unified_inbox_routes 保持一致）
_SUPERVISOR_ROLES = {"master", "admin"}

# J2：意图 → 模板场景映射（与 template_seeds.py 的 scene 枚举对应）
_INTENT_TO_SCENE: dict = {
    "退款": "refund", "退款申请": "refund", "要求退款": "refund",
    "物流查询": "shipping", "催单": "shipping", "物流": "shipping", "到货查询": "shipping",
    "订单查询": "order_inquiry", "查询订单": "order_inquiry", "下单": "order_inquiry",
    "产品咨询": "product_info", "产品问题": "product_info", "询问产品": "product_info",
    "投诉": "complaint", "投诉处理": "complaint", "不满": "complaint",
    "感谢": "closing", "再见": "closing",
    "询问": "order_inquiry",  # 通用问询默认归订单
}


def _intent_to_scene(intent: str) -> str:
    """J2：将 quick_analyze 返回的 intent 映射到模板 scene。"""
    if not intent:
        return ""
    for key, scene in _INTENT_TO_SCENE.items():
        if key in intent:
            return scene
    return ""


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

        # J2：模板智能推荐——按 intent→scene + 客户语言从模板库精准检索
        template_suggestions: list = []
        try:
            inbox_store = getattr(request.app.state, "inbox_store", None)
            if inbox_store is not None and t.strip():
                _intent = str(analysis.get("intent") or "")
                _lang = str(analysis.get("language") or "zh")
                _scene = _intent_to_scene(_intent)
                tpls = inbox_store.list_templates(
                    language=_lang, scene=_scene, limit=3
                )
                if not tpls and _scene:
                    # 同场景、无语言限制 fallback（用于语言不完整的模板库）
                    tpls = inbox_store.list_templates(scene=_scene, limit=3)
                for tpl in tpls[:3]:
                    template_suggestions.append({
                        "id": str(tpl.get("id") or ""),
                        "title": str(tpl.get("title") or ""),
                        "content": str(tpl.get("content") or ""),
                        "scene": str(tpl.get("scene") or ""),
                        "language": str(tpl.get("language") or ""),
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
            "template_suggestions": template_suggestions,  # J2
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

    # ── H1 草稿翻译 + H2 批量处置（必须在 /{draft_id} 之前注册） ──────────────

    @app.post("/api/drafts/{draft_id}/translate")
    async def api_drafts_translate(request: Request, draft_id: str, _=Depends(api_auth)):
        """H1：将草稿 AI 回复文本翻译为客户语言。

        优先使用 web_app.state.translation_service（若已配置），
        降级时返回原文（带 fallback 标记）。
        返回：{ok, translated, source_lang, target_lang, fallback, draft_id}
        """
        svc = _get_draft_service(request)
        draft = svc.get_draft(draft_id)
        if draft is None:
            raise HTTPException(404, "草稿不存在")

        draft_text = str(draft.get("draft_text") or "")
        peer_text = str(draft.get("peer_text") or "")
        if not draft_text.strip():
            return {"ok": False, "error": "草稿文本为空，无需翻译", "draft_id": draft_id}

        # 推断语言：source=中文（草稿），target=客户语言（从 peer_text 检测）
        from src.ai.chat_assistant_service import detect_language
        source_lang = str(draft.get("draft_lang") or "zh") or "zh"
        target_lang = detect_language(peer_text) if peer_text.strip() else "en"
        if target_lang in ("zh", "zh-TW", ""):
            target_lang = "en"  # 中文草稿对中文客户无需翻译，回退英文

        ts_svc = getattr(request.app.state, "translation_service", None)
        if ts_svc is None:
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": draft_text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": True,
                "note": "翻译服务未启用，返回原文",
            }
        try:
            result = await ts_svc.translate(
                draft_text, target_lang=target_lang, source_lang=source_lang, style="chat"
            )
            translated = str(result.translated_text if hasattr(result, "translated_text")
                             else result.get("translated_text", draft_text))
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": translated,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": False,
            }
        except Exception as e:
            logger.debug("草稿翻译失败: %s", e)
            return {
                "ok": True,
                "draft_id": draft_id,
                "translated": draft_text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "fallback": True,
                "note": "翻译暂时不可用，返回原文",
            }

    @app.post("/api/drafts/bulk-resolve")
    async def api_drafts_bulk_resolve(request: Request, _=Depends(api_auth)):
        """H2：批量处置草稿（主管专属）。

        Body: {action: "approve"|"reject", draft_ids: [...], by?}
        返回：{ok, total, succeeded, failed, errors: [...]}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, "批量处置需要主管权限")
        svc = _get_draft_service(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "请求体解析失败")
        action = str(body.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, "action 须为 approve 或 reject")
        draft_ids = list(body.get("draft_ids") or [])
        if not draft_ids:
            return {"ok": True, "total": 0, "succeeded": 0, "failed": 0, "errors": []}
        by = str(body.get("by") or _session_agent_id(request))
        agent_id = _session_agent_id(request)
        succeeded, failed, errors = 0, 0, []
        for did in draft_ids[:50]:  # 单次最多 50 条
            try:
                result = svc.resolve_with_audit(
                    str(did), action, by=by or agent_id or "bulk"
                )
                if result.get("ok"):
                    succeeded += 1
                else:
                    failed += 1
                    errors.append({"draft_id": did, "error": result.get("error", "failed")})
            except Exception as e:
                failed += 1
                errors.append({"draft_id": did, "error": str(e)})
        return {
            "ok": True,
            "total": len(draft_ids),
            "succeeded": succeeded,
            "failed": failed,
            "errors": errors[:10],
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



# ── J3：数据导出 API（独立注册函数，由 admin.py + main.py 共同调用）──────────

def register_export_route(app, *, api_auth):
    """J3：注册 GET /api/workspace/export（CSV 导出，主管专属）。

    admin.py 和 main.py 各调用一次。FastAPI 允许同一路由被重复注册，
    重复注册时不报错，但为避免重复，应在两者之一中只调用一次。
    实际只在 admin.py 中调用，以保持 inventory 测试覆盖。
    """
    import csv
    import datetime
    import io
    from fastapi import Depends
    from fastapi.responses import StreamingResponse

    @app.get("/api/workspace/export")
    async def api_workspace_export(
        request: Request,
        export_type: str = "drafts",
        days: int = 7,
        _=Depends(api_auth),
    ):
        """J3：导出工作台数据为 CSV（主管专属）。

        export_type: drafts | audit | perf
        days: 最近 N 天（1–90），默认 7
        返回 CSV 文件流（BOM-UTF8，兼容 Excel）。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, "数据导出需要主管权限")

        days_int = max(1, min(90, int(days or 7)))
        cutoff_ts = time.time() - days_int * 86400
        etype = str(export_type or "drafts").lower()

        inbox_store = getattr(request.app.state, "inbox_store", None)
        svc = getattr(request.app.state, "draft_service", None)
        buf = io.StringIO()
        w = csv.writer(buf)

        if etype == "audit" and inbox_store is not None:
            w.writerow(["时间", "草稿ID", "坐席ID", "动作", "风险等级", "自动化等级", "原因", "会话ID"])
            logs = inbox_store.list_draft_audit(limit=2000)
            for row in logs:
                if float(row.get("ts") or 0) < cutoff_ts:
                    continue
                ts_str = datetime.datetime.fromtimestamp(
                    float(row.get("ts") or 0)
                ).strftime("%Y-%m-%d %H:%M:%S")
                w.writerow([
                    ts_str, row.get("draft_id", ""), row.get("agent_id", ""),
                    row.get("action", ""), row.get("risk_level", ""),
                    row.get("autopilot_level", ""), row.get("reason", ""),
                    row.get("conversation_id", ""),
                ])

        elif etype == "perf" and inbox_store is not None:
            w.writerow(["坐席ID", "总处理", "批准", "拒绝", "自动发送", "强制放行"])
            perf = inbox_store.get_agent_perf(since_ts=cutoff_ts)
            for row in perf:
                w.writerow([
                    row.get("agent_id", ""),
                    row.get("total", 0), row.get("approved", 0),
                    row.get("rejected", 0), row.get("autosend", 0),
                    row.get("force_override", 0),
                ])

        else:  # drafts（默认）
            w.writerow([
                "草稿ID", "会话ID", "平台", "账号", "状态", "风险等级",
                "自动化等级", "草稿文本（截断）", "客户文本（截断）", "处置人", "创建时间",
            ])
            if svc is not None:
                for d in svc.list_drafts(limit=2000):
                    ca = float(d.get("created_at") or d.get("created_ts") or 0)
                    if ca > 0 and ca < cutoff_ts:
                        continue
                    ts_str = datetime.datetime.fromtimestamp(ca).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ) if ca > 0 else ""
                    w.writerow([
                        d.get("draft_id", ""), d.get("conversation_id", ""),
                        d.get("platform", ""), d.get("account_id", ""),
                        d.get("status", ""), d.get("risk_level", ""),
                        d.get("autopilot_level", ""),
                        str(d.get("draft_text", ""))[:200],
                        str(d.get("peer_text", ""))[:100],
                        d.get("decided_by", ""), ts_str,
                    ])

        buf.seek(0)
        filename = f"ws_{etype}_{datetime.date.today().isoformat()}.csv"
        return StreamingResponse(
            iter(["\ufeff" + buf.read()]),  # BOM：兼容 Excel 直接打开 UTF-8
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


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
