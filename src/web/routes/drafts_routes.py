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
from src.web.web_i18n import tr

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
        raise HTTPException(503, tr(request, "err.svc.draft_service_disabled"))
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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        worker = getattr(request.app.state, "autosend_worker", None)
        if worker is None:
            return {"ok": True, "worker": None, "note": tr(request, "err.draft.autosend_worker_off")}
        snap = worker.status_snapshot()
        try:
            from src.inbox.voice_autosend import metrics_snapshot as _vms
            snap["voice"] = _vms()  # 全自动语音：sent/fallback/last_reason/last_duration_ms
        except Exception:
            pass
        try:
            from src.inbox.image_autosend import metrics_snapshot as _ims
            snap["image"] = _ims()  # 全自动发图：sent/fallback/last_reason/last_kind
        except Exception:
            pass
        try:
            # 统一草稿引擎规则栈生效观测（记忆/情感/陪伴/慢思考/守卫/重试命中）
            from src.monitoring.metrics_store import get_metrics_store
            snap["draft_pipeline"] = get_metrics_store().get_inbox_draft_metrics()
        except Exception:
            pass
        return {"ok": True, "worker": snap}

    @app.get("/api/drafts/pipeline-metrics")
    async def api_drafts_pipeline_metrics(
        request: Request, window_sec: int = 3600, _=Depends(api_auth),
    ):
        """统一草稿引擎规则栈的**只读聚合指标**（命中率/分位延迟/规则触发计数）。

        与 ``autosend-status`` 的区别：本端点**只需 API token、不强制主管会话**。
        返回的全部是非 PII 聚合数（无消息内容、无用户标识，见
        ``MetricsStore.get_inbox_draft_metrics``），故对持令牌的运营工具开放是安全的，
        用于 ``scripts/suggest_draft_thresholds`` / CI 闭合阈值校准回路
        （此前该脚本只能打主管会话端点，纯 token 拿不到指标）。

        ``window_sec``（60–86400，默认 3600）控制返回的 ``window`` 滑窗大小——与
        ``health_watchdog._check_draft_quality`` 评估告警所用窗口对齐，便于校准脚本按
        「watchdog 视角」与「稳态累计」两个口径同时观测。
        """
        out: dict = {"ok": True}
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ws = max(60, min(86400, int(window_sec or 3600)))
            out["draft_pipeline"] = get_metrics_store().get_inbox_draft_metrics(window_sec=ws)
        except Exception:
            out["draft_pipeline"] = {}
        return out

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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
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

        # Q2: 若有 draft_id，附带草稿质量评分（已存储在 reply_drafts）
        quality_info: dict = {}
        if draft_id:
            try:
                inbox_s = getattr(request.app.state, "inbox_store", None)
                if inbox_s is not None:
                    qr = inbox_s.get_draft_quality(draft_id)
                    if qr and qr["quality_score"] >= 0:
                        from src.inbox.quality import quality_to_badge
                        quality_info = {
                            "quality_score": qr["quality_score"],
                            "quality_breakdown": qr["breakdown"],
                            "quality_badge": quality_to_badge(qr["quality_score"]),
                        }
            except Exception:
                pass

        # Q3: 记录 KB 推荐事件（用于命中率监控）
        if kb_matches:
            try:
                inbox_s2 = getattr(request.app.state, "inbox_store", None)
                if inbox_s2 is not None:
                    import uuid as _uuid2
                    _agent = _session_agent_id(request)
                    for km in kb_matches:
                        _rec_id = _uuid2.uuid4().hex[:12]
                        km["_rec_id"] = _rec_id  # 返回给前端，用于点击时回调
                        inbox_s2.record_kb_recommendation(
                            rec_id=_rec_id,
                            entry_id=str(km.get("entry_id") or ""),
                            entry_title=str(km.get("title") or ""),
                            conversation_id=str(conversation_id or ""),
                            agent_id=str(_agent or ""),
                        )
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
            **quality_info,  # Q2: quality_score, quality_breakdown, quality_badge
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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
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
            raise HTTPException(404, tr(request, "err.draft.not_found"))

        draft_text = str(draft.get("draft_text") or "")
        peer_text = str(draft.get("peer_text") or "")
        if not draft_text.strip():
            return {"ok": False, "error": tr(request, "err.draft.text_empty_no_translate"), "draft_id": draft_id}

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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        svc = _get_draft_service(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        action = str(body.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, tr(request, "err.draft.bad_action"))
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
            raise HTTPException(404, tr(request, "err.draft.not_found"))
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
            raise HTTPException(403, tr(request, "err.perm.supervisor_force_l4"))
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

    @app.post("/api/drafts/expire-stale")
    async def api_drafts_expire_stale(request: Request, _=Depends(api_auth)):
        """治理：作废搁置过久的 pending 草稿（转 cancelled + 审计，不删行）。主管专属。

        Body（均可选）：
          max_age_hours: int=168   超此时长仍 pending 即作废
          levels: list=["L3","L4"] 仅这些等级；[] = 全部
          groups_only: bool=true   仅群/频道会话（防误伤 1:1 私聊待审）
          dry_run: bool=false      true = 只预览命中数与清单，不写库
        返回 {ok, dry_run, count, drafts:[{draft_id,autopilot_level,conversation_id,age_hours}]}。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = getattr(request.app.state, "inbox_store", None)
        if store is None or not hasattr(store, "expire_stale_pending_drafts"):
            raise HTTPException(503, tr(request, "err.svc.draft_service_disabled"))
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            max_age = float(body.get("max_age_hours", 168) or 168)
        except (TypeError, ValueError):
            max_age = 168.0
        levels = body.get("levels")
        if levels is None:
            levels = ["L3", "L4"]
        levels = [str(x) for x in (levels or []) if str(x)]
        groups_only = bool(body.get("groups_only", True))
        dry_run = bool(body.get("dry_run", False))
        by = _session_agent_id(request) or "system"
        victims = store.expire_stale_pending_drafts(
            max_age_hours=max_age,
            levels=levels or None,
            groups_only=groups_only,
            agent_id=by,
            dry_run=dry_run,
        )
        return {
            "ok": True,
            "dry_run": dry_run,
            "count": len(victims),
            "drafts": victims,
        }



# ── J3：数据导出 API（独立注册函数，由 admin.py + main.py 共同调用）──────────

def register_metrics_route(app, *, api_auth):
    """L1：注册 GET /api/workspace/metrics（系统指标，主管专属）。

    format=json（默认）→ JSON 对象
    format=prometheus   → Prometheus text format（# HELP / # TYPE / metric lines）
    """
    import io
    from fastapi import Depends
    from fastapi.responses import PlainTextResponse

    @app.get("/api/workspace/metrics")
    async def api_workspace_metrics(
        request: Request,
        format: str = "json",
        _=Depends(api_auth),
    ):
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        # ── 聚合各子系统指标 ──────────────────────────────────────
        metrics: dict = {"ts": time.time()}

        # AutosendWorker
        try:
            w = getattr(request.app.state, "autosend_worker", None)
            if w is not None:
                snap = w.status_snapshot()
                metrics["autosend"] = snap
            else:
                metrics["autosend"] = {"running": False}
        except Exception:
            metrics["autosend"] = {"running": False}

        # SLAWatcher (K1/K2)
        try:
            sw = getattr(request.app.state, "sla_watcher", None)
            if sw is not None:
                metrics["sla_watcher"] = sw.status_snapshot()
            else:
                metrics["sla_watcher"] = {"running": False}
        except Exception:
            metrics["sla_watcher"] = {"running": False}

        # P3：AutoClaimWorker（auto_assign 自动认领执行端）
        try:
            acw = getattr(request.app.state, "auto_claim_worker", None)
            metrics["auto_claim"] = (acw.status_snapshot()
                                     if acw is not None else {"running": False})
        except Exception:
            metrics["auto_claim"] = {"running": False}

        # WebhookNotifier (L2)
        try:
            whn = getattr(request.app.state, "webhook_notifier", None)
            if whn is not None:
                metrics["webhook"] = whn.status_snapshot()
            else:
                metrics["webhook"] = {"running": False}
        except Exception:
            metrics["webhook"] = {"running": False}

        # ScheduledReporter (N2)
        try:
            rpt = getattr(request.app.state, "scheduled_reporter", None)
            metrics["scheduled_reporter"] = rpt.status_snapshot() if rpt is not None else {"running": False}
        except Exception:
            metrics["scheduled_reporter"] = {"running": False}

        # InboxStore 草稿统计
        try:
            inbox = getattr(request.app.state, "inbox_store", None)
            if inbox is not None:
                try:
                    metrics["inbox_dedup"] = inbox.dedup_stats()
                except Exception:
                    pass
                svc = getattr(request.app.state, "draft_service", None)
                if svc is not None:
                    all_drafts = svc.list_drafts(status="pending", limit=1000)
                    by_level: dict = {}
                    for d in all_drafts:
                        lv = str(d.get("autopilot_level") or "?")
                        by_level[lv] = by_level.get(lv, 0) + 1
                    metrics["drafts"] = {
                        "pending_total": len(all_drafts),
                        "by_level": by_level,
                    }
                # EventBus 订阅者数
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    metrics["event_bus"] = {
                        "subscriber_count": get_event_bus().subscriber_count,
                        "history_size": len(get_event_bus().recent_events(50)),
                    }
                except Exception:
                    pass
        except Exception:
            pass

        # P57：翻译引擎用量（调用/成功率/延迟/降级）
        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            metrics["translation_engines"] = get_translation_engine_stats().dump()
        except Exception:
            pass

        # P1-4：出向翻译漏斗（覆盖率/auto 解析失败率/降级率/按语言分布）
        try:
            from src.ai.outbound_translation_stats import get_outbound_translation_stats
            metrics["outbound_translation"] = get_outbound_translation_stats().dump()
        except Exception:
            pass

        # V：语音克隆合成的「语言纠正」观测（合成语言随文本语种，防中文声纹念英文；纠正率/按语种分布）
        try:
            from src.ai.voice_synth_stats import get_voice_synth_stats
            metrics["voice_synth_language"] = get_voice_synth_stats().dump()
        except Exception:
            pass

        # 音频情绪识别（SER）观测：从声学语气听出的情绪分布 + 模型可用性（软降级次数）
        try:
            from src.ai.speech_emotion_stats import get_speech_emotion_stats
            metrics["speech_emotion"] = get_speech_emotion_stats().dump()
        except Exception:
            pass

        # 深度人设观测：巩固/画像/内部梗/经历/未收尾话题/回指 累计（真人感"长出来"的证据）
        try:
            from src.companion.deep_persona_stats import get_deep_persona_stats
            _dpm = get_deep_persona_stats().dump()
            # G1：合入 embedder 命中率/延迟（语义召回"值不值"的量化）
            try:
                from src.companion.deep_persona_runtime import embedder_stats
                _dpm["embedder"] = embedder_stats()
            except Exception:
                pass
            # 趋势快照（默认关 trend_log）：机会式 upsert 当天累计 → 供 7 天 sparkline / AB
            try:
                from src.companion.deep_persona_runtime import trend_log_enabled
                if trend_log_enabled():
                    from src.companion.deep_persona_trend import (
                        get_deep_persona_trend, flatten_stats_for_trend)
                    _tr = get_deep_persona_trend("config/deep_persona.db")
                    if _tr is not None:
                        _tr.upsert_today(flatten_stats_for_trend(_dpm))
                        _dpm["trend_7d"] = _tr.read_recent(7)
            except Exception:
                pass
            metrics["deep_persona"] = _dpm
        except Exception:
            pass

        # 实时语音通话观测（发起/接通率/时长/挂断原因/主机健康/显存生命周期）
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            metrics["realtime_voice"] = get_realtime_voice_stats().dump()
        except Exception:
            pass

        # P58：通用 provider 用量（OCR/ASR 等多模态后端）
        try:
            from src.ai.provider_stats import all_provider_stats
            ap = all_provider_stats()
            if ap:
                metrics["providers"] = ap
        except Exception:
            pass

        # 前端「哑按钮」运行时错误观测（dead-click 守卫 beacon 累计；哪页哪函数点崩、多频）
        try:
            from src.web.frontend_error_stats import get_frontend_error_stats
            metrics["frontend_errors"] = get_frontend_error_stats().dump()
        except Exception:
            pass

        # 会话 peer 身份「惰性解析/自愈补名」观测（数字号 healed 了多少 / 缓存命中 / 取不到）
        try:
            from src.web.peer_identity_stats import get_peer_identity_stats
            metrics["peer_identity"] = get_peer_identity_stats().dump()
        except Exception:
            pass

        fmt = str(format or "json").lower()

        if fmt == "prometheus":
            buf = io.StringIO()

            def _gauge(name: str, value, help_text: str = "", labels: str = "") -> None:
                if help_text:
                    buf.write(f"# HELP {name} {help_text}\n")
                buf.write(f"# TYPE {name} gauge\n")
                lbl = f"{{{labels}}}" if labels else ""
                buf.write(f"{name}{lbl} {value}\n")

            _gauge("ws_autosend_running",
                   1 if metrics["autosend"].get("running") else 0,
                   "AutosendWorker is running")
            _gauge("ws_autosend_total_sent",
                   metrics["autosend"].get("total_sent", 0),
                   "Total L2 drafts auto-sent")
            _gauge("ws_autosend_total_errors",
                   metrics["autosend"].get("total_errors", 0),
                   "Total autosend errors")
            _gauge("ws_autosend_circuit_open",
                   1 if metrics["autosend"].get("circuit_open") else 0,
                   "AutosendWorker circuit breaker open")

            _gauge("ws_sla_watcher_running",
                   1 if metrics["sla_watcher"].get("running") else 0,
                   "SLAWatcher is running")
            _gauge("ws_sla_breach_events_total",
                   metrics["sla_watcher"].get("total_breach_events", 0),
                   "Total SLA breach events published")
            _gauge("ws_sla_reassigned_total",
                   metrics["sla_watcher"].get("total_reassigned", 0),
                   "Total drafts auto-reassigned")
            _gauge("ws_sla_expired_total",
                   metrics["sla_watcher"].get("total_expired", 0),
                   "Total stale pending drafts auto-expired")
            _gauge("ws_sla_quiesced_total",
                   metrics["sla_watcher"].get("quiesced_count", 0),
                   "Total drafts quiesced (stale, alert suppressed)")

            ac = metrics.get("auto_claim", {})
            _gauge("ws_auto_claim_running",
                   1 if ac.get("running") else 0,
                   "AutoClaimWorker is running")
            _gauge("ws_auto_claim_total",
                   ac.get("total_claimed", 0),
                   "Total conversations auto-claimed")
            _gauge("ws_auto_claim_lang_matched_total",
                   ac.get("total_lang_matched", 0),
                   "Auto-claims where agent language matched conversation")

            _gauge("ws_webhook_total_sent",
                   metrics["webhook"].get("total_sent", 0),
                   "Total webhook notifications sent")
            _gauge("ws_webhook_total_errors",
                   metrics["webhook"].get("total_errors", 0),
                   "Total webhook send errors")

            drafts = metrics.get("drafts", {})
            _gauge("ws_drafts_pending_total",
                   drafts.get("pending_total", 0),
                   "Total pending drafts")
            for lv, cnt in (drafts.get("by_level") or {}).items():
                _gauge("ws_drafts_pending_by_level",
                       cnt,
                       labels=f'level="{lv}"')

            eb = metrics.get("event_bus", {})
            _gauge("ws_sse_subscribers",
                   eb.get("subscriber_count", 0),
                   "Active SSE subscriber count")

            # P1-4：出向翻译漏斗（覆盖率/auto 失败/降级/按语言）以 counter 形式输出
            try:
                from src.ai.outbound_translation_stats import get_outbound_translation_stats
                buf.write(get_outbound_translation_stats().dump_prom())
            except Exception:
                pass

            # 实时语音通话观测（发起/接通/时长/主机健康/显存生命周期）
            try:
                from src.ai.realtime_voice_stats import get_realtime_voice_stats
                buf.write(get_realtime_voice_stats().dump_prom())
            except Exception:
                pass

            # 前端「哑按钮」运行时错误（by page / by fn / by type）
            try:
                from src.web.frontend_error_stats import get_frontend_error_stats
                buf.write(get_frontend_error_stats().dump_prom())
            except Exception:
                pass

            # 会话 peer 身份自愈补名（by source × outcome）
            try:
                from src.web.peer_identity_stats import get_peer_identity_stats
                buf.write(get_peer_identity_stats().dump_prom())
            except Exception:
                pass

            return PlainTextResponse(buf.getvalue(), media_type="text/plain; version=0.0.4")

        return {"ok": True, **metrics}


def register_telemetry_route(app, *, api_auth):
    """前端「哑按钮」运行时错误上报（任意登录用户可写，不限主管）。

    POST /api/telemetry/frontend-error  body: {page, fn, type}
    dead-click 守卫（unified_inbox + _rpa_shared_scripts）捕获 ReferenceError 后 beacon 到此，
    经 FrontendErrorStats 累计，读出走 /api/workspace/metrics.frontend_errors（主管专属）。
    只收计数用的三个消毒字段，绝不落原文/堆栈；任何异常都吞掉返回 ok，绝不影响前端。
    """
    from fastapi import Depends

    @app.post("/api/telemetry/frontend-error")
    async def api_frontend_error(request: Request, _=Depends(api_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            try:
                from src.web.frontend_error_stats import get_frontend_error_stats
                get_frontend_error_stats().record(
                    page=str(body.get("page") or ""),
                    fn=str(body.get("fn") or ""),
                    etype=str(body.get("type") or ""),
                )
            except Exception:
                pass
        return {"ok": True}


def register_glossary_route(app, *, api_auth):
    """P59：术语库管理控制台 API（主管专属）。

    GET  /api/workspace/glossary           → 合并视图（terms/protect + 来源标记 + version）
    POST /api/workspace/glossary           → 增删改覆盖层 {op, term?, translation?, word?}
                                             op ∈ upsert_term|remove_term|add_protect|remove_protect
    覆盖层落 config/glossary_overrides.yaml，重建术语库并热更新到 translation_service。
    """
    from fastapi import Depends

    def _build_view(request: Request):
        from src.ai.translation_glossary import build_glossary
        store = getattr(request.app.state, "glossary_store", None)
        config = getattr(request.app.state, "glossary_config", None) or {}
        domain_files = getattr(request.app.state, "glossary_domain_files", None) or []
        overrides = store.load() if store is not None else {"terms": {}, "protect": []}
        merged = build_glossary(config, domain_files=domain_files, overrides=overrides)
        ov_terms = set((overrides.get("terms") or {}).keys())
        ov_protect = set(overrides.get("protect") or [])
        try:
            from src.ai.glossary_hits import get_glossary_hits
            hits = get_glossary_hits()
        except Exception:
            hits = None
        terms = [
            {"term": k, "translation": v,
             "source": "console" if k in ov_terms else "base",
             "editable": k in ov_terms,
             "hits": hits.term_hits(k) if hits else 0}
            for k, v in sorted(merged.terms.items())
        ]
        protect = [
            {"word": w, "source": "console" if w in ov_protect else "base",
             "editable": w in ov_protect,
             "hits": hits.protect_hits(w) if hits else 0}
            for w in merged.protect
        ]
        hd = hits.dump() if hits else {"total_term_hits": 0, "total_protect_hits": 0}
        return {
            "ok": True,
            "version": merged.version,
            "enabled": not merged.empty() or True,
            "terms": terms,
            "protect": protect,
            "counts": {"terms": len(terms), "protect": len(protect),
                       "console_terms": len(ov_terms), "console_protect": len(ov_protect),
                       "term_hits": hd.get("total_term_hits", 0),
                       "protect_hits": hd.get("total_protect_hits", 0)},
            "has_store": store is not None,
        }

    def _export_csv(request: Request) -> str:
        import csv
        import io
        view = _build_view(request)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["type", "key", "value"])
        for t in view["terms"]:
            w.writerow(["term", t["term"], t["translation"]])
        for p in view["protect"]:
            w.writerow(["protect", p["word"], ""])
        return buf.getvalue()

    def _import_csv(store, text: str) -> dict:
        import csv
        import io
        added_terms = 0
        added_protect = 0
        reader = csv.reader(io.StringIO(text))
        for i, row in enumerate(reader):
            if not row:
                continue
            kind = (row[0] or "").strip().lower()
            if i == 0 and kind == "type":
                continue  # 跳过表头
            key = (row[1] if len(row) > 1 else "").strip()
            val = (row[2] if len(row) > 2 else "").strip()
            try:
                if kind == "term" and key and val:
                    store.upsert_term(key, val)
                    added_terms += 1
                elif kind == "protect" and key:
                    store.add_protect(key)
                    added_protect += 1
            except ValueError:
                continue
        return {"added_terms": added_terms, "added_protect": added_protect}

    def _rebuild_and_apply(request: Request):
        from src.ai.translation_glossary import build_glossary
        store = getattr(request.app.state, "glossary_store", None)
        config = getattr(request.app.state, "glossary_config", None) or {}
        domain_files = getattr(request.app.state, "glossary_domain_files", None) or []
        overrides = store.load() if store is not None else {"terms": {}, "protect": []}
        gl = build_glossary(config, domain_files=domain_files, overrides=overrides)
        svc = getattr(request.app.state, "translation_service", None)
        if svc is not None and hasattr(svc, "update_glossary"):
            svc.update_glossary(gl.terms, gl.protect, gl.version)
        return gl

    @app.get("/api/workspace/glossary")
    async def api_workspace_glossary_get(request: Request, format: str = "json", _=Depends(api_auth)):
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        if str(format or "").lower() == "csv":
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                _export_csv(request),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=glossary.csv"},
            )
        return _build_view(request)

    @app.post("/api/workspace/glossary")
    async def api_workspace_glossary_edit(request: Request, _=Depends(api_auth)):
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = getattr(request.app.state, "glossary_store", None)
        if store is None:
            return {"ok": False, "message": "术语库未初始化（翻译服务未启用）"}
        body = await request.json()
        op = str(body.get("op") or "").strip()
        try:
            if op == "upsert_term":
                store.upsert_term(body.get("term"), body.get("translation"))
            elif op == "remove_term":
                store.remove_term(body.get("term"))
            elif op == "add_protect":
                store.add_protect(body.get("word"))
            elif op == "remove_protect":
                store.remove_protect(body.get("word"))
            elif op == "import_csv":
                imp = _import_csv(store, str(body.get("csv") or ""))
            else:
                return {"ok": False, "message": f"未知操作: {op}"}
        except ValueError as ex:
            return {"ok": False, "message": str(ex)}
        gl = _rebuild_and_apply(request)
        view = _build_view(request)
        view["applied_version"] = gl.version
        if op == "import_csv":
            view["imported"] = imp
        return view


def register_trend_route(app, *, api_auth):
    """O1：注册 GET /api/workspace/trend（CSAT/审批率趋势图数据，主管专属）。"""
    from fastapi import Depends
    import time as _time

    @app.get("/api/workspace/trend")
    async def api_workspace_trend(
        request: Request,
        days: int = 7,
        bucket: str = "day",
        _=Depends(api_auth),
    ):
        """O1：返回 CSAT + L3/L4 占比的时间序列趋势数据（主管专属）。

        days:   7（默认，近一周）| 30（近一月）| 90（近季度）
        bucket: day（默认，每天一个数据点）| week（每周）
        返回：{csat_trend: [...], level_trend: [...], delta: {...}}
        delta 包含本周期 vs 上期 CSAT 均值变化量。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        days = max(1, min(90, int(days)))
        bucket_sec = 604800 if bucket == "week" else 86400
        now = _time.time()
        since_ts = now - days * 86400
        prev_since_ts = now - days * 2 * 86400  # 对比上一周期

        csat_trend = inbox.get_csat_trend(since_ts=since_ts, bucket_sec=bucket_sec)
        level_trend = inbox.get_draft_level_trend(since_ts=since_ts, bucket_sec=bucket_sec)

        # delta：当期 vs 上期 CSAT 均值差
        curr_csat_rows = inbox.get_csat_trend(since_ts=since_ts)
        prev_csat_rows = inbox.get_csat_trend(since_ts=prev_since_ts, bucket_sec=bucket_sec)
        prev_csat_rows_filtered = [r for r in prev_csat_rows if r["bucket_ts"] < since_ts]

        def _avg(rows):
            vals = [r["avg_csat"] for r in rows if r["avg_csat"] is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        curr_avg = _avg(curr_csat_rows)
        prev_avg = _avg(prev_csat_rows_filtered)
        delta_csat = round(curr_avg - prev_avg, 2) if curr_avg is not None and prev_avg is not None else None

        return {
            "ok": True,
            "days": days,
            "csat_trend": csat_trend,
            "level_trend": level_trend,
            "delta": {
                "csat_current": curr_avg,
                "csat_previous": prev_avg,
                "csat_delta": delta_csat,
                "direction": (
                    "up" if delta_csat and delta_csat > 0.05
                    else "down" if delta_csat and delta_csat < -0.05
                    else "stable"
                ),
            },
        }


def register_ab_testing_route(app, *, api_auth):
    """S1：注册 A/B 测试管理 API（主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/ab-tests")
    async def api_list_ab_tests(
        request: Request,
        status: str = "",
        _=Depends(api_auth),
    ):
        """S1：列出所有 A/B 测试（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        tests = ab.list_tests(status=status)
        return {"ok": True, "tests": tests, "count": len(tests)}

    @app.post("/api/workspace/ab-tests")
    async def api_create_ab_test(
        request: Request,
        _=Depends(api_auth),
    ):
        """S1：创建新 A/B 测试（主管专属）。

        Body: {name, intent_filter, template_a_id, template_b_id, description?, min_sample?}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        name = str(body.get("name") or "").strip()
        intent_filter = str(body.get("intent_filter") or "").strip()
        tpl_a = str(body.get("template_a_id") or "").strip()
        tpl_b = str(body.get("template_b_id") or "").strip()
        if not name or not tpl_a or not tpl_b:
            raise HTTPException(400, tr(request, "err.draft.ab_fields_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        test_id = ab.create_test(
            name=name,
            intent_filter=intent_filter,
            template_a_id=tpl_a,
            template_b_id=tpl_b,
            description=str(body.get("description") or ""),
            min_sample=int(body.get("min_sample") or 30),
            created_by=_session_agent_id(request),
        )
        return {"ok": True, "test_id": test_id}

    @app.get("/api/workspace/ab-tests/{test_id}/results")
    async def api_ab_test_results(
        request: Request,
        test_id: str,
        _=Depends(api_auth),
    ):
        """S1：获取 A/B 测试详细结果（含显著性检验，主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        results = ab.get_results(test_id)
        if "error" in results:
            raise HTTPException(404, results["error"])
        return {"ok": True, **results}

    @app.post("/api/workspace/ab-tests/{test_id}/stop")
    async def api_stop_ab_test(
        request: Request,
        test_id: str,
        _=Depends(api_auth),
    ):
        """S1：手动停止 A/B 测试（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.ab_testing import ABTestingStore
        ab = ABTestingStore(inbox)
        ok = ab.stop_test(test_id, reason="manual_api")
        if not ok:
            raise HTTPException(404, tr(request, "err.draft.test_not_found", id=test_id))
        return {"ok": True, "test_id": test_id, "status": "stopped"}


def register_trace_route(app, *, api_auth):
    """S3：注册 /api/workspace/trace/{trace_id}（全链路时间线查询）。"""
    from fastapi import Depends

    @app.get("/api/workspace/trace/{trace_id}")
    async def api_trace_timeline(
        request: Request,
        trace_id: str,
        _=Depends(api_auth),
    ):
        """S3：重建指定 trace_id 的完整调用链时间线。

        调用链：ingest → draft_created → audit → survey_scheduled
        主管和普通坐席均可访问（用于自助排查生产问题）。
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        from src.inbox.tracer import TraceTimeline
        tl = TraceTimeline(inbox)
        result = tl.build(trace_id)
        if not result.get("found"):
            raise HTTPException(404, tr(request, "err.draft.trace_not_found", id=trace_id))
        return {"ok": True, **result}

    @app.get("/api/workspace/trace")
    async def api_recent_traces(
        request: Request,
        limit: int = 20,
        platform: str = "",
        _=Depends(api_auth),
    ):
        """S3：列出最近的 trace_id（主管专属）。

        返回最近 limit 条对话的 trace_id + 基本信息，便于主管选取追踪。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        try:
            with inbox._lock:
                q = """SELECT conversation_id, trace_id, platform, msg_count, updated_at,
                              last_intent, last_emotion
                       FROM conversation_meta
                       WHERE trace_id != ''"""
                params = []
                if platform:
                    q += " AND platform=?"
                    params.append(platform)
                q += " ORDER BY updated_at DESC LIMIT ?"
                params.append(max(1, min(100, limit)))
                rows = inbox._conn.execute(q, params).fetchall()
            return {
                "ok": True,
                "traces": [dict(r) for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            raise HTTPException(500, str(e))


def register_anomaly_route(app, *, api_auth):
    """S2：注册 /api/workspace/anomaly（异常检测状态查询，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/anomaly")
    async def api_anomaly_check(
        request: Request,
        _=Depends(api_auth),
    ):
        """S2：即时运行异常检测并返回结果（主管专属）。

        不触发告警；仅返回当前检测结果供主管查看。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        from src.inbox.anomaly import AnomalyDetector
        cfg = getattr(request.app.state, "cfg", {}) or {}
        detector = AnomalyDetector(inbox, cfg)
        results = detector.run_full_check()
        anomaly_dicts = [r.to_dict() for r in results]
        anomalies = [d for d in anomaly_dicts if d["is_anomaly"]]

        return {
            "ok": True,
            "enabled": detector.is_enabled(),
            "sensitivity": detector._sensitivity(),
            "baseline_days": detector._baseline_days(),
            "metrics_checked": len(results),
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "all_metrics": anomaly_dicts,
        }


def register_workload_route(app, *, api_auth):
    """R2：注册 /api/workspace/workload（坐席工作负荷均衡，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/workload")
    async def api_agent_workload(
        request: Request,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """R2：返回坐席工作负荷（主管专属）。

        ?agent_id=xxx → 单坐席详情
        不带参数 → 所有在线坐席负荷列表（用于仪表板均衡视图）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        cfg = getattr(request.app.state, "cfg", {}) or {}
        max_cap = int((cfg.get("workspace") or {}).get("max_concurrent_convs") or 0)

        if agent_id:
            wl = inbox.get_agent_workload(agent_id.strip())
            if max_cap > 0:
                wl["overloaded"] = wl["active_convs"] >= max_cap
            return {"ok": True, "workload": wl, "max_cap": max_cap}

        workloads = inbox.list_agent_workloads(max_load_cap=max_cap)
        overloaded = [w for w in workloads if w.get("overloaded")]
        return {
            "ok": True,
            "workloads": workloads,
            "max_cap": max_cap,
            "total_agents": len(workloads),
            "overloaded_count": len(overloaded),
            "lightest_agent": inbox.get_lightest_agent(max_load_cap=max_cap),
        }


def register_kb_stats_route(app, *, api_auth):
    """Q2+Q3：注册 KB 命中率统计 + 质量评分分布（主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/kb-stats")
    async def api_kb_stats(
        request: Request,
        days: int = 7,
        _=Depends(api_auth),
    ):
        """Q3：返回 KB 条目推荐/点击/使用统计（主管专属）。

        Query: ?days=7（过去 N 天，默认 7 天）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        _days = max(1, min(90, int(days or 7)))
        since_ts = time.time() - _days * 86400
        stats = inbox.get_kb_hit_stats(since_ts=since_ts, top_n=30)
        # 额外提供低命中率列表（命中率<30%且推荐>=3次）
        low_hit = sorted(
            [s for s in stats if s["recommended"] >= 3 and s["hit_rate"] < 30],
            key=lambda x: x["hit_rate"],
        )[:10]
        return {
            "ok": True,
            "days": _days,
            "entries": stats,
            "low_hit_entries": low_hit,
        }

    @app.post("/api/workspace/kb-click")
    async def api_kb_click(
        request: Request,
        _=Depends(api_auth),
    ):
        """Q3：记录坐席点击了某次 KB 推荐（client-side tracking）。

        Body: {rec_id, used_in_draft?, draft_id?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        rec_id = str(body.get("rec_id") or "").strip()
        if not rec_id:
            raise HTTPException(400, tr(request, "err.draft.rec_id_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        inbox.click_kb_recommendation(
            rec_id=rec_id,
            used_in_draft=bool(body.get("used_in_draft")),
            draft_id=str(body.get("draft_id") or ""),
        )
        return {"ok": True, "rec_id": rec_id}

    @app.get("/api/workspace/quality-stats")
    async def api_quality_stats(
        request: Request,
        days: int = 7,
        _=Depends(api_auth),
    ):
        """Q2：草稿质量分分布统计（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        _days = max(1, min(90, int(days or 7)))
        since_ts = time.time() - _days * 86400
        stats = inbox.list_draft_quality_stats(since_ts=since_ts)
        return {"ok": True, "days": _days, **stats}


def register_workspace_route(app, *, api_auth):
    """P3：注册 /api/workspace/workspaces（多租户工作区 CRUD，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/workspaces")
    async def api_list_workspaces(request: Request, _=Depends(api_auth)):
        """P3：列出所有工作区（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        workspaces = inbox.list_workspaces()
        # 为每个工作区追加统计
        for ws in workspaces:
            try:
                ws["stats"] = inbox.get_workspace_stats(ws["workspace_id"])
            except Exception:
                ws["stats"] = {}
        # 当前工作区（从 session 读，默认 default）
        try:
            current_ws = request.scope.get("session", {}).get("workspace_id", "default")
        except Exception:
            current_ws = "default"
        return {"ok": True, "workspaces": workspaces, "current": current_ws}

    @app.post("/api/workspace/workspaces")
    async def api_upsert_workspace(request: Request, _=Depends(api_auth)):
        """P3：创建或更新工作区配置（主管专属）。

        Body: {workspace_id, display_name, config}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        ws_id = str(body.get("workspace_id") or "").strip()
        if not ws_id:
            raise HTTPException(400, tr(request, "err.draft.workspace_id_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        inbox.upsert_workspace(
            ws_id,
            display_name=str(body.get("display_name") or ""),
            config=body.get("config") or {},
        )
        stats = inbox.get_workspace_stats(ws_id)
        return {"ok": True, "workspace_id": ws_id, "stats": stats}

    @app.get("/api/workspace/workspaces/{workspace_id}/stats")
    async def api_workspace_stats(request: Request, workspace_id: str, _=Depends(api_auth)):
        """P3：返回指定工作区统计（主管专属）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        return {"ok": True, **inbox.get_workspace_stats(workspace_id)}


def register_kb_archive_route(app, *, api_auth):
    """P2：注册 POST /api/workspace/kb-archive（优质回复存入知识库，主管专属）。"""
    from fastapi import Depends

    @app.post("/api/workspace/kb-archive")
    async def api_workspace_kb_archive(
        request: Request,
        _=Depends(api_auth),
    ):
        """P2：将一条已审批草稿的回复文本一键归档进知识库。

        Request body: {
            "draft_id":   str,       # 草稿 ID
            "title":      str,       # KB 条目标题（必填）
            "category":   str,       # 分类（可选，默认"客服回复"）
            "triggers":   list[str], # 关键词触发器（可选）
            "scenario":   str,       # 适用场景描述（可选）
            "language":   str,       # 语言（可选，默认 zh）
        }

        主管专属；坐席可"推荐归档"，触发主管审核（future）。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))

        draft_id = str(body.get("draft_id") or "").strip()
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, tr(request, "err.draft.title_required"))

        kb = getattr(request.app.state, "kb_store", None)
        if kb is None:
            raise HTTPException(503, tr(request, "err.svc.kb_not_ready"))

        # 获取草稿内容（final_text 优先，回退到 draft_text）
        draft_text = ""
        peer_text = ""
        conversation_id = ""
        intent = ""

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is not None and draft_id:
            try:
                draft = inbox.get_draft(draft_id)
                if draft is not None:
                    draft_text = str(draft.get("final_text") or draft.get("draft_text") or "")
                    peer_text = str(draft.get("peer_text") or "")
                    conversation_id = str(draft.get("conversation_id") or "")
            except Exception:
                pass

            # 从 conversation_meta 获取意图标签
            if conversation_id:
                try:
                    meta = inbox.get_conv_meta(conversation_id)
                    if meta:
                        intent = str(meta.get("last_intent") or "")
                except Exception:
                    pass

        # 触发器：优先用请求中的，回退到意图标签
        triggers = list(body.get("triggers") or [])
        if not triggers and intent:
            triggers = [intent]

        # 构建 KB 条目
        agent_id = _session_agent_id(request)
        entry_data = {
            "category": str(body.get("category") or "客服回复"),
            "title": title,
            "triggers": triggers,
            "scenario": str(body.get("scenario") or (f"适用场景: {peer_text[:100]}" if peer_text else "")),
            "steps": "",
            "principles": "",
            "example_reply_zh": draft_text,
            "forbidden": "",
            "enabled": 1,
            "reply_mode": "direct",
            "use_count": 0,
            "rating": 0.0,
        }

        try:
            entry_id = kb.add_entry(entry_data)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.draft.kb_write_failed", err=e))

        # 写审计（便于溯源）
        if inbox is not None and draft_id:
            try:
                inbox.record_draft_audit(
                    draft_id,
                    autopilot_level="",
                    action="kb_archived",
                    agent_id=agent_id,
                    reason=f"KB entry_id={entry_id}, title={title[:40]}",
                    conversation_id=conversation_id,
                )
            except Exception:
                pass

        return {
            "ok": True,
            "entry_id": entry_id,
            "title": title,
            "draft_id": draft_id,
        }


def register_my_perf_route(app, *, api_auth):
    """O3：注册 GET /api/workspace/my-perf（坐席自助绩效查询，无需主管权限）。"""
    from fastapi import Depends
    import time as _time

    @app.get("/api/workspace/my-perf")
    async def api_workspace_my_perf(
        request: Request,
        days: int = 7,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """O3：坐席自助绩效查询。

        无需主管权限；
        agent_id: 可选，主管可指定其他坐席；坐席只能查自己。
        days: 1 / 7（默认）/ 30 / 90
        返回：{agent_id, total, approved, rejected, autosend, avg_csat, timeline, rank}
        """
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        days = max(1, min(90, int(days)))
        now = _time.time()
        since_ts = now - days * 86400

        # 当前登录坐席 ID
        current_uid = _session_agent_id(request)
        is_sup = _is_supervisor(request)

        # 权限：非主管只能查自己
        target_id = str(agent_id or "").strip()
        if not target_id:
            target_id = current_uid
        elif not is_sup and target_id != current_uid:
            raise HTTPException(403, tr(request, "err.perm.agent_self_only"))

        # 个人绩效
        perf_list = inbox.get_agent_perf(since_ts=since_ts, agent_id=target_id)
        perf = perf_list[0] if perf_list else {
            "agent_id": target_id, "total": 0, "approved": 0,
            "rejected": 0, "autosend": 0, "avg_csat": None,
        }

        # 趋势（每天一个点）
        timeline = inbox.get_agent_perf_timeline(
            since_ts=since_ts,
            agent_id=target_id,
            bucket_sec=86400,
        )

        # 排名：在全部坐席中的 CSAT 排名
        all_perf = inbox.get_agent_perf(since_ts=since_ts)
        all_sorted = sorted(
            [p for p in all_perf if p.get("total", 0) > 0],
            key=lambda x: float(x.get("avg_csat") or -1),
            reverse=True,
        )
        rank = next(
            (i + 1 for i, p in enumerate(all_sorted) if p.get("agent_id") == target_id),
            None,
        )
        total_agents = len(all_sorted)

        # 近期处置记录（最近 10 条）
        recent_decisions = [
            r for r in inbox.list_draft_audit(limit=200)
            if str(r.get("agent_id") or "") == target_id
        ][:10]

        return {
            "ok": True,
            "agent_id": target_id,
            "days": days,
            "perf": perf,
            "timeline": timeline,
            "rank": rank,
            "total_agents": total_agents,
            "recent_decisions": recent_decisions,
        }


def register_leaderboard_route(app, *, api_auth):
    """N3：注册 GET /api/workspace/leaderboard（CSAT 坐席排行榜，主管专属）。"""
    from fastapi import Depends

    @app.get("/api/workspace/leaderboard")
    async def api_workspace_leaderboard(
        request: Request,
        period: str = "weekly",
        limit: int = 20,
        _=Depends(api_auth),
    ):
        """N3：坐席 CSAT 排行榜（主管专属）。

        period: daily（过去 24h）| weekly（过去 7d）| monthly（过去 30d）
        limit: 最多返回 N 名坐席（默认 20）
        返回按 avg_csat DESC, total DESC 排序的坐席列表，含排名 + 徽章。
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        import time as _time
        period_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(str(period), 7)
        since_ts = _time.time() - period_days * 86400

        perf = inbox.get_agent_perf(since_ts=since_ts)
        # 过滤出有 avg_csat 的坐席 + 排序
        ranked = sorted(
            [p for p in perf if p.get("total", 0) > 0],
            key=lambda x: (
                float(x.get("avg_csat") or -1),
                int(x.get("total") or 0),
            ),
            reverse=True,
        )
        ranked = ranked[:max(1, int(limit))]

        # 加排名 + 徽章
        _BADGES = {1: "🏆", 2: "🥈", 3: "🥉"}
        result = []
        for i, p in enumerate(ranked, 1):
            csat = p.get("avg_csat")
            p["rank"] = i
            p["badge"] = _BADGES.get(i, "")
            p["csat_stars"] = (
                "⭐" * int(round(csat)) + "☆" * (5 - int(round(csat)))
                if csat is not None else "—"
            )
            result.append(p)

        return {
            "ok": True,
            "period": period,
            "since_ts": since_ts,
            "updated_at": _time.time(),
            "leaderboard": result,
        }


def register_broadcast_route(app, *, api_auth):
    """M2：注册 POST /api/workspace/broadcast（主管广播事件到 EventBus，触发 Webhook）。"""
    from fastapi import Depends

    @app.post("/api/workspace/broadcast")
    async def api_workspace_broadcast(
        request: Request,
        _=Depends(api_auth),
    ):
        """M2：广播任意事件到 EventBus（主管专属，用于简报推送等）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, tr(request, "err.req.bad_body"))
        event_type = str(body.get("type") or "").strip()
        if not event_type:
            raise HTTPException(400, tr(request, "err.draft.type_required"))
        data = body.get("data") or {}
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish(event_type, data)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.draft.eventbus_failed", err=e))
        return {"ok": True, "type": event_type}


def register_report_route(app, *, api_auth):
    """M2：注册 GET /api/workspace/report（工作日报/周报，主管专属）。"""
    from fastapi import Depends
    from fastapi.responses import PlainTextResponse

    @app.get("/api/workspace/report")
    async def api_workspace_report(
        request: Request,
        period: str = "daily",
        format: str = "json",
        _=Depends(api_auth),
    ):
        """M2：工作日报/周报 API（主管专属）。

        period: daily（过去 24h，默认）| weekly（过去 7 天）
        format: json（默认）| text（Webhook 推送格式）| html（仪表盘嵌入）
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))

        from src.inbox.report_generator import ReportGenerator
        gen = ReportGenerator(
            inbox_store=inbox,
            draft_service=getattr(request.app.state, "draft_service", None),
            app_state=request.app.state,
        )
        report_data = gen.generate(period=period)

        fmt = str(format or "json").lower()
        if fmt == "text":
            return PlainTextResponse(gen.format_text(report_data))
        if fmt == "html":
            from fastapi.responses import HTMLResponse
            return HTMLResponse(gen.format_html(report_data))
        return {"ok": True, **report_data}


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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))

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
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
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
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 14))) * 86400
        timeline = store.get_agent_perf_timeline(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "timeline": timeline, "days": int(days)}

    @app.get("/api/workspace/agent-copilot-stats")
    async def api_agent_copilot_stats(
        request: Request,
        days: int = 14,
        agent_id: str = "",
        _=Depends(api_auth),
    ):
        """P54：Copilot 采纳率与质量回放（主管专属）。"""
        if not _is_supervisor(request):
            from fastapi import HTTPException
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        store = _get_store(request)
        since = _time.time() - max(1, min(90, int(days or 14))) * 86400
        stats = store.get_copilot_stats(since_ts=since, agent_id=agent_id or "")
        return {"ok": True, "days": int(days), **stats}

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
