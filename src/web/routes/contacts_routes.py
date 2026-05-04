"""Contacts / Journey / Merge Review Web REST 路由。

按既有 routes/ 约定导出 `register_contacts_routes`，由 admin.py 按需挂载。
当前文件不做页面模板渲染——下阶段 W3 再做 `contacts.html` / `merge_reviews.html`。

端点清单：
- GET  /api/contacts                      列表（分页）
- GET  /api/contacts/{id}                 Contact 详情 + journey + 所有 channel_identity
- GET  /api/contacts/{id}/timeline        journey_events 时间线
- GET  /api/merge-reviews                 pending 合并审核队列
- POST /api/merge-reviews/{id}/approve    通过（触发 relink + 标 resolved）
- POST /api/merge-reviews/{id}/reject     拒绝（标 resolved，不动 ci）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

_OPS_TPL_DIR = Path(__file__).resolve().parent.parent / "templates" / "ops"

_NAV_LINKS = [
    ("/ops/contacts", "联系人"),
    ("/ops/merge-reviews", "合并审核"),
    ("/ops/mobile-handoffs", "Mobile 交接单"),
]


def _make_nav_html(active: str = "") -> str:
    links = "".join(
        f'<a href="{h}" {"class=\"active\"" if h == active else ""}>'
        f"{label}</a>"
        for h, label in _NAV_LINKS
    )
    return f'<div class="nav">{links}</div>'


def _load_ops_html(name: str, active: str = "") -> str:
    """从 templates/ops/ 加载静态 HTML。
    active 为当前页 href，注入统一导航栏（替换 <!-- NAV_INJECT --> 占位符）。
    """
    p = _OPS_TPL_DIR / name
    try:
        html = p.read_text(encoding="utf-8")
        return html.replace("<!-- NAV_INJECT -->", _make_nav_html(active), 1)
    except FileNotFoundError:
        return f"<h1>Ops UI missing: {name}</h1>"

from src.contacts.merge import MergeService
from src.contacts.store import ContactStore

# 可选依赖：intimacy / reactivation。注入时才挂载对应 endpoint。
try:
    from src.skills.intimacy_engine import IntimacyEngine
except ImportError:
    IntimacyEngine = None  # type: ignore

try:
    from src.skills.reactivation_scheduler import ReactivationScheduler
except ImportError:
    ReactivationScheduler = None  # type: ignore

logger = logging.getLogger(__name__)


def _journey_to_dict(journey) -> Dict[str, Any]:
    return {
        "journey_id": journey.journey_id,
        "contact_id": journey.contact_id,
        "persona_id": journey.persona_id,
        "funnel_stage": journey.funnel_stage,
        "intimacy_score": journey.intimacy_score,
        "engagement_score": journey.engagement_score,
        "readiness_score": journey.readiness_score,
        "intimacy_updated_at": journey.intimacy_updated_at,
        "snapshot_refreshed_at": journey.snapshot_refreshed_at,
        "created_at": journey.created_at,
        "updated_at": journey.updated_at,
    }


def register_contacts_routes(
    app,
    *,
    api_auth,
    contacts_store: ContactStore,
    merge_service: MergeService,
    audit_store=None,
    intimacy_engine=None,
    reactivation_scheduler=None,
    gateway=None,
    account_limiter=None,
    mobile_bridge=None,
) -> None:
    """在 FastAPI app 上挂载 contacts 相关的 REST endpoint。

    参数：
      app            — FastAPI 实例
      api_auth       — 鉴权 Depends callable（由 admin.py 统一提供）
      contacts_store — ContactStore 实例（建议由上层做 singleton）
      merge_service  — MergeService 实例
      audit_store    — 可选，若提供则记录敏感操作（approve/reject）
    """

    @app.get("/api/contacts")
    async def list_contacts(
        limit: int = 50,
        offset: int = 0,
        expand: str = "",
        q: str = "",
        _=Depends(api_auth),
    ):
        """
        expand=journey 时，item 含 funnel_stage / intimacy_score 字段——
        消除 UI 的 N+1 请求。
        q 时按 contact_id / 姓名 / canonical_id 搜索。
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows, total = contacts_store.search_contacts(q=q.strip(), limit=limit, offset=offset)
        include_journey = "journey" in (expand or "").split(",")
        items: list = []
        for c in rows:
            d = c.to_dict()
            if include_journey:
                j = contacts_store.get_journey_by_contact(c.contact_id)
                if j:
                    d["funnel_stage"] = j.funnel_stage
                    d["intimacy_score"] = j.intimacy_score
                    d["journey_id"] = j.journey_id
            items.append(d)
        return {
            "total": total,
            "q": q.strip(),
            "items": items,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/contacts/{contact_id}")
    async def get_contact(contact_id: str, _=Depends(api_auth)):
        c = contacts_store.get_contact(contact_id)
        if not c:
            raise HTTPException(status_code=404, detail="contact_not_found")
        journey = contacts_store.get_journey_by_contact(contact_id)
        cis = contacts_store.list_channel_identities_of(contact_id)
        return {
            "contact": c.to_dict(),
            "journey": _journey_to_dict(journey) if journey else None,
            "channel_identities": [ci.to_dict() for ci in cis],
        }

    @app.get("/api/contacts/{contact_id}/timeline")
    async def timeline(
        contact_id: str,
        limit: int = 100,
        _=Depends(api_auth),
    ):
        journey = contacts_store.get_journey_by_contact(contact_id)
        if not journey:
            raise HTTPException(status_code=404, detail="journey_not_found")
        limit = max(1, min(int(limit), 500))
        events = contacts_store.list_events(journey.journey_id, limit=limit)
        return {
            "journey_id": journey.journey_id,
            "funnel_stage": journey.funnel_stage,
            "events": events,
        }

    @app.get("/api/merge-reviews")
    async def list_reviews(limit: int = 100, _=Depends(api_auth)):
        limit = max(1, min(int(limit), 200))
        items = contacts_store.list_pending_reviews(limit=limit)
        # 丰富一下信息：返回候选 ci + target contact 的基本字段，便于 UI 展示
        enriched = []
        for r in items:
            ci = contacts_store.get_channel_identity(r["candidate_ci_id"])
            tgt = contacts_store.get_contact(r["target_contact_id"])
            enriched.append({
                **r,
                "candidate_ci": ci.to_dict() if ci else None,
                "target_contact": tgt.to_dict() if tgt else None,
            })
        return {"items": enriched}

    @app.post("/api/merge-reviews/{review_id}/approve")
    async def approve_review(review_id: str, request: Request, _=Depends(api_auth)):
        user = _extract_user(request)
        ok = merge_service.approve_review(review_id, resolved_by=user)
        if audit_store and ok:
            _safe_audit(audit_store, user, "merge_review_approve", review_id)
        if not ok:
            raise HTTPException(status_code=400, detail="approve_failed_or_already_resolved")
        return {"ok": True}

    @app.post("/api/merge-reviews/{review_id}/reject")
    async def reject_review(review_id: str, request: Request, _=Depends(api_auth)):
        user = _extract_user(request)
        ok = merge_service.reject_review(review_id, resolved_by=user)
        if audit_store and ok:
            _safe_audit(audit_store, user, "merge_review_reject", review_id)
        if not ok:
            raise HTTPException(status_code=400, detail="reject_failed_or_already_resolved")
        return {"ok": True}

    # ── 漏斗统计 & Journey 详情 ───────────────────────────
    @app.get("/api/funnel/stats")
    async def funnel_stats(_=Depends(api_auth)):
        return {
            "total_contacts": contacts_store.count_contacts(),
            "by_stage": contacts_store.count_journeys_by_stage(),
            "by_channel": contacts_store.count_channel_identities_by_channel(),
        }

    @app.get("/api/journeys/{journey_id}")
    async def journey_detail(journey_id: str, _=Depends(api_auth)):
        j = contacts_store.get_journey(journey_id)
        if not j:
            raise HTTPException(status_code=404, detail="journey_not_found")
        return {"journey": _journey_to_dict(j)}

    # ── 可选：intimacy 重算 ───────────────────────────────
    if intimacy_engine is not None:
        @app.post("/api/journeys/{journey_id}/intimacy/refresh")
        async def refresh_intimacy(journey_id: str, _=Depends(api_auth)):
            j = contacts_store.get_journey(journey_id)
            if not j:
                raise HTTPException(status_code=404, detail="journey_not_found")
            bd = intimacy_engine.refresh_journey_intimacy(journey_id)
            return {"journey_id": journey_id, "intimacy": bd.to_dict()}

    # ── 可选：reactivation 候选列表 ───────────────────────
    if reactivation_scheduler is not None:
        @app.get("/api/reactivation/candidates")
        async def list_reactivation(_=Depends(api_auth)):
            cands = reactivation_scheduler.list_candidates()
            return {
                "items": [{
                    "journey_id": c.journey_id,
                    "contact_id": c.contact_id,
                    "funnel_stage": c.funnel_stage,
                    "intimacy_score": c.intimacy_score,
                    "silent_days": c.silent_days,
                    "last_reactivation_ts": c.last_reactivation_ts,
                } for c in cands],
            }

        @app.post("/api/reactivation/{journey_id}/mark-sent")
        async def mark_reactivation(journey_id: str, request: Request,
                                     _=Depends(api_auth)):
            user = _extract_user(request)
            reactivation_scheduler.mark_sent(journey_id, note=f"by:{user or 'system'}")
            return {"ok": True}

    # ── 健康检查（feature flag 开时各子服务是否就绪） ─
    @app.get("/api/contacts/health")
    async def contacts_health(_=Depends(api_auth)):
        return {
            "ok": True,
            "services": {
                "contacts_store": contacts_store is not None,
                "merge_service": merge_service is not None,
                "intimacy_engine": intimacy_engine is not None,
                "reactivation_scheduler": reactivation_scheduler is not None,
                "gateway": gateway is not None,
                "account_limiter": account_limiter is not None,
            },
        }

    # ── 账号限额 ──────────────────────────────────────
    if account_limiter is not None:
        @app.get("/api/accounts/{account_id}/limit")
        async def get_limit(account_id: str, _=Depends(api_auth)):
            return account_limiter.get_counts(account_id)

        @app.post("/api/accounts/{account_id}/limit/reset")
        async def reset_limit(account_id: str, request: Request,
                               _=Depends(api_auth)):
            user = _extract_user(request)
            account_limiter.reset(account_id)
            if audit_store:
                _safe_audit(audit_store, user, "account_limit_reset", account_id)
            return {"ok": True}

    # ── 引流预览（dry_run） ──────────────────────────────
    if gateway is not None:
        @app.get("/api/handoff/preview")
        async def preview_handoff(
            messenger_ci_id: str,
            latest_in_text: str = "",
            tone: str = "",
            language_override: str = "",
            _=Depends(api_auth),
        ):
            r = gateway.maybe_issue_handoff(
                messenger_ci_id=messenger_ci_id,
                latest_in_text=latest_in_text,
                tone=tone,
                language_override=language_override,
                dry_run=True,
            )
            return {
                "success": r.success,
                "reason": r.reason,
                "text": r.text,
                "script_id": r.script_id,
                "language": r.language,
                "readiness_score": r.readiness_score,
                "remaining_today": r.remaining_today,
                "warn_hits": r.warn_hits,
                "details": r.details,
            }

    # ── 最小 Ops UI（纯静态 HTML + fetch，不走 Jinja2） ───
    @app.get("/ops/contacts", response_class=HTMLResponse)
    async def ops_contacts_page(_=Depends(api_auth)):
        return HTMLResponse(_load_ops_html("contacts.html", active="/ops/contacts"))

    @app.get("/ops/merge-reviews", response_class=HTMLResponse)
    async def ops_merge_reviews_page(_=Depends(api_auth)):
        return HTMLResponse(_load_ops_html("merge_reviews.html", active="/ops/merge-reviews"))

    # ── Mobile Bridge 路由（仅 mobile_bridge 注入时挂载） ────────────
    if mobile_bridge is not None:
        import asyncio as _asyncio
        import functools as _functools

        @app.get("/api/mobile-bridge/health")
        async def mobile_bridge_health(_=Depends(api_auth)):
            """Bridge 状态：同步计数、watermark、最近错误、dead_letter。"""
            return await _asyncio.to_thread(mobile_bridge.status)

        @app.get("/api/mobile-handoffs/summary")
        async def mobile_handoffs_summary(_=Depends(api_auth)):
            """各 state 的 handoff 计数（供 UI tab 徽章使用）。"""
            counts = await _asyncio.to_thread(mobile_bridge.count_by_state)
            total = sum(counts.values())
            return {"by_state": counts, "total": total}

        @app.get("/api/mobile-handoffs")
        async def list_mobile_handoffs(
            state: str = "",
            canonical_id: str = "",
            limit: int = 50,
            offset: int = 0,
            _=Depends(api_auth),
        ):
            """从 openclaw.db 实时查询 handoff 列表（只读）。"""
            limit = max(1, min(int(limit), 200))
            offset = max(0, int(offset))
            rows = await _asyncio.to_thread(
                _functools.partial(
                    mobile_bridge.list_mobile_handoffs,
                    state=state, canonical_id=canonical_id,
                    limit=limit, offset=offset,
                )
            )
            return {"items": rows, "count": len(rows)}

        @app.get("/api/mobile-handoffs/{handoff_id}")
        async def get_mobile_handoff(handoff_id: str, _=Depends(api_auth)):
            """查单条 handoff（来自 openclaw.db）。"""
            row = await _asyncio.to_thread(mobile_bridge.get_mobile_handoff, handoff_id)
            if not row:
                raise HTTPException(status_code=404, detail="handoff_not_found")
            return row

        @app.post("/api/mobile-handoffs/{handoff_id}/acknowledge")
        async def mobile_ack(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台确认接单 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_acknowledge, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_acknowledge", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "acknowledge", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.post("/api/mobile-handoffs/{handoff_id}/complete")
        async def mobile_complete(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台标记完成 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_complete, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_complete", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "complete", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.post("/api/mobile-handoffs/{handoff_id}/reject")
        async def mobile_reject(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台拒绝 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_reject, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_reject", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "reject", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.get("/ops/mobile-handoffs", response_class=HTMLResponse)
        async def ops_mobile_handoffs_page(_=Depends(api_auth)):
            return HTMLResponse(_load_ops_html("mobile_handoffs.html", active="/ops/mobile-handoffs"))

        @app.get("/api/mobile-bridge/writeback-queue")
        async def list_writeback_queue(
            status: str = "dead_letter",
            limit: int = 50,
            _=Depends(api_auth),
        ):
            """列出 writeback 队列特定状态的条目（默认 dead_letter）。"""
            limit = max(1, min(int(limit), 200))
            rows = await _asyncio.to_thread(
                _functools.partial(mobile_bridge.list_writeback_queue, status=status, limit=limit)
            )
            return {"items": rows, "count": len(rows), "status": status}

        @app.post("/api/mobile-bridge/writeback-queue/{item_id}/retry")
        async def retry_dead_letter(item_id: int, _=Depends(api_auth)):
            """\u5c06 dead_letter 条目重置为 pending，下次轮询时自动重试。"""
            ok = await _asyncio.to_thread(
                _functools.partial(mobile_bridge.retry_dead_letter, item_id)
            )
            if not ok:
                raise HTTPException(status_code=404, detail="item_not_found_or_not_dead_letter")
            return {"ok": True, "item_id": item_id, "new_status": "pending"}


def _extract_user(request: Request) -> str:
    """从 request.state 拿登录用户名，缺失时回退空串。"""
    for attr in ("user_id", "username", "user"):
        val = getattr(request.state, attr, None)
        if val:
            return str(val)
    return ""


def _safe_audit(audit_store, user_id: str, action: str, target: str) -> None:
    try:
        audit_store.log(user_id or "system", action, target=target)
    except Exception as e:
        logger.debug("audit log skipped: %s", e)

