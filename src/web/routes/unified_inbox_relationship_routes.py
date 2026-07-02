"""统一收件箱——关系阶段可视化 / 进阶·降级·回暖 / 客户级对齐·时间轴路由域（巨石拆分 slice 18）。

把"Phase 43 关系阶段进度条 + P46 坐席确认制（confirm/downgrade/reunion）+ P50 客户级关系阶段
（含多会话冲突检测与一键对齐）+ P51 阶段演进时间轴"这一内聚子域，从
``register_unified_inbox_routes`` 巨型闭包中外移为
``register_relationship_stage_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用。
端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：context 关系阶段 payload 构建族（slice 4 已成模块）、auth._agent_from_request、
services._inbox_store；关系阶段算法/事件总线/时间轴富集均为 handler 内局部 import。
只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_context import (
    _build_contact_relationship_payload,
    _build_relationship_stage_payload,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_relationship_stage_routes(app, *, api_auth) -> None:
    """挂载关系阶段相关端点（/api/workspace/conv|contact/{id}/relationship-stage* 与 stage-timeline）。"""

    # ─── Phase 43: 关系阶段可视化 + 进阶提醒 ───────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/relationship-stage")
    async def api_conv_relationship_stage(conversation_id: str, request: Request):
        """DD1/P46：关系阶段进度条 + 待确认进阶（坐席确认制）。"""
        api_auth(request)
        store = _inbox_store(request)
        result = _build_relationship_stage_payload(
            request, conversation_id, store, emit_pending_event=True,
        )
        return {"ok": True, "conversation_id": conversation_id, **result}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/confirm")
    async def api_conv_relationship_stage_confirm(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：坐席确认关系进阶。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "")
        target = str(body.get("stage") or payload.get("pending_stage") or payload.get("computed_stage") or "")
        from src.utils.companion_relationship import STAGE_ORDER
        if target not in STAGE_ORDER:
            raise HTTPException(422, tr(request, "err.ws.invalid_target_stage"))
        if confirmed and STAGE_ORDER.index(target) <= STAGE_ORDER.index(confirmed):
            raise HTTPException(422, tr(request, "err.ws.stage_must_be_higher"))
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str((payload.get("context") or {}).get("contact_id") or "")
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        store.record_draft_audit(
            "", action="stage_confirm", agent_id=agent_id,
            reason=f"{prev_label} → {target}",
            conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            from src.utils.companion_relationship import STAGE_LABEL_ZH
            import time as _t
            get_event_bus().publish("stage_advance", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "confirmed": True,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "previous_stage": confirmed,
                "previous_stage_label": prev_label,
                "stage": target,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/downgrade")
    async def api_conv_relationship_stage_downgrade(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：坐席手动降级关系阶段（附原因）。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json()
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise HTTPException(422, tr(request, "err.ws.field_required", field="reason"))
        from src.inbox.relationship_stage import downgrade_stage_one_level
        from src.utils.companion_relationship import STAGE_ORDER, STAGE_LABEL_ZH
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "initial")
        target = str(body.get("stage") or downgrade_stage_one_level(confirmed))
        if target not in STAGE_ORDER:
            raise HTTPException(422, tr(request, "err.ws.invalid_target_stage"))
        if STAGE_ORDER.index(target) >= STAGE_ORDER.index(confirmed):
            raise HTTPException(422, tr(request, "err.ws.stage_must_be_lower"))
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str((payload.get("context") or {}).get("contact_id") or "")
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        note_body = f"[关系降级] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{reason}"
        store.add_conv_note(
            conversation_id, note_body,
            agent_id=agent_id, agent_name=agent_name,
        )
        store.record_draft_audit(
            "", action="stage_downgrade", agent_id=agent_id,
            reason=note_body, conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("stage_downgrade", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "agent_name": agent_name,
                "previous_stage_label": prev_label,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/reunion")
    async def api_conv_relationship_stage_reunion(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：确认久别重逢 — 将确认阶段同步至亲密度阶段并推荐回暖话题。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        from src.utils.companion_relationship import derive_stage_from_intimacy, STAGE_LABEL_ZH
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        if not payload.get("reunion"):
            raise HTTPException(422, tr(request, "err.ws.no_reunion_signal"))
        ctx = payload.get("context") or {}
        intim = ctx.get("intimacy_score")
        target = derive_stage_from_intimacy(float(intim)) if intim is not None else str(payload.get("computed_stage") or "initial")
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "")
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str(ctx.get("contact_id") or "")
        import time as _t
        reunion_ts = _t.time()
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
            store.set_contact_rel_stage(
                contact_id, target, updated_by=agent_id, reunion_ack_ts=reunion_ts,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        store.ack_rel_reunion(conversation_id, ts=reunion_ts)
        note = str(body.get("note") or "已确认回暖，采用自然问候策略").strip()
        store.add_conv_note(
            conversation_id, f"[关系回暖] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{note}",
            agent_id=agent_id, agent_name=agent_name,
        )
        reunion_reason = (
            f"[关系回暖] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{note}"
        )
        store.record_draft_audit(
            "", action="stage_reunion", agent_id=agent_id,
            reason=reunion_reason, conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("stage_reunion", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "agent_name": agent_name,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "note": note,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.get("/api/workspace/contact/{contact_id}/relationship-stage")
    async def api_contact_relationship_stage(contact_id: str, request: Request):
        """P50：客户级关系阶段（含多会话冲突检测）。"""
        api_auth(request)
        store = _inbox_store(request)
        result = _build_contact_relationship_payload(request, contact_id, store)
        return {"ok": True, "contact_id": contact_id, **result}

    @app.post("/api/workspace/contact/{contact_id}/relationship-stage/sync")
    async def api_contact_relationship_stage_sync(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """P50：一键对齐多会话阶段（to_contact | to_highest）。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        mode = str(body.get("mode") or "to_contact").strip()
        from src.inbox.contact_rel_stage import highest_stage
        from src.utils.companion_relationship import STAGE_ORDER

        contact_rec = store.get_contact_rel_stage(contact_id) or {}
        contact_stage = str(contact_rec.get("confirmed_stage") or "")
        conv_stages = store.list_conv_rel_stages_for_contact(contact_id)
        if mode == "to_highest":
            target = highest_stage([s for s in conv_stages.values() if s] + ([contact_stage] if contact_stage else []))
        else:
            target = contact_stage or highest_stage([s for s in conv_stages.values() if s])
        if not target or target not in STAGE_ORDER:
            raise HTTPException(422, tr(request, "err.ws.no_alignable_stage"))
        agent_id, _ = _agent_from_request(request)
        if not contact_stage:
            store.set_contact_rel_stage(contact_id, target, updated_by=agent_id)
        elif mode == "to_highest" and STAGE_ORDER.index(target) > STAGE_ORDER.index(contact_stage):
            store.set_contact_rel_stage(contact_id, target, updated_by=agent_id)
        synced = store.sync_convs_to_stage(contact_id, target)
        store.record_draft_audit(
            f"contact:{contact_id}", action="stage_sync", agent_id=agent_id,
            reason=f"对齐至 {target}（{mode}，{synced} 会话）",
            conversation_id="",
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            from src.utils.companion_relationship import STAGE_LABEL_ZH
            get_event_bus().publish("stage_sync", {
                "contact_id": contact_id,
                "agent_id": agent_id,
                "target_stage": target,
                "target_stage_label": STAGE_LABEL_ZH.get(target, target),
                "mode": mode,
                "synced": synced,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_contact_relationship_payload(request, contact_id, store)
        return {"ok": True, "contact_id": contact_id, "synced": synced, "target_stage": target, **refreshed}

    @app.get("/api/workspace/contact/{contact_id}/stage-timeline")
    async def api_contact_stage_timeline(
        contact_id: str, request: Request, limit: int = 50,
    ):
        """P51：客户关系阶段演进时间轴（确认/降级/回暖/对齐）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        from src.inbox.stage_timeline import (
            build_contact_stage_summary,
            enrich_stage_audit_row,
        )
        lim = max(1, min(200, int(limit or 50)))
        rows = store.list_contact_stage_audits(contact_id, limit=lim)
        events = [enrich_stage_audit_row(r) for r in rows]
        contact_rec = store.get_contact_rel_stage(contact_id)
        summary = build_contact_stage_summary(events, contact_rec=contact_rec)
        return {
            "ok": True,
            "contact_id": contact_id,
            "events": events,
            "count": len(events),
            "summary": summary,
        }
