"""统一收件箱——客户 360°时间轴 / 多坐席协作剧本上下文路由域（巨石拆分 slice 25）。

把 ``register_unified_inbox_routes`` 巨型闭包中的客户级聚合视图子域整体外移为
``register_collab_context_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 31 客户 360°时间轴：``contact/{id}/timeline``
- Phase 45 多坐席协作剧本上下文：``contact/{id}/collab-context`` + ``conv/{id}/collab-context``
  （注：``conv`` 级 handler 在进程内复用 ``contact`` 级 handler，二者必须同模块共存）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 25 端点契约断言）。

依赖全部朝下：services.(_inbox_store/_contacts_store)、
context.(_build_contact_relationship_payload/_build_relationship_stage_payload)；剧本引擎/
关系阶段/积分打分器均为 handler 内局部 import。只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Request

from src.web.routes.unified_inbox_context import (
    _build_contact_relationship_payload,
    _build_relationship_stage_payload,
)
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store

logger = logging.getLogger(__name__)


def register_collab_context_routes(app, *, api_auth) -> None:
    """挂载客户 360°时间轴 + 多坐席协作剧本上下文（客户级/会话级）端点。"""

    # ─── Phase 31: 客户 360° 时间轴 ────────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/timeline")
    async def api_contact_timeline(
        contact_id: str, request: Request, limit: int = 100
    ):
        """X1：获取客户完整互动时间轴（消息/注解/归档/摘要，跨会话聚合）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        limit = max(1, min(500, int(limit or 100)))
        events = store.get_contact_timeline(contact_id, limit=limit)
        return {"ok": True, "contact_id": contact_id, "events": events, "count": len(events)}

    # ─── Phase 45: 多坐席协作剧本上下文 ─────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/collab-context")
    async def api_contact_collab_context(contact_id: str, request: Request):
        """EE1：客户级协作上下文（统一阶段 + 积分 + 话题 + 活跃工作链）。"""
        api_auth(request)
        store = _inbox_store(request)
        cs = _contacts_store(request)
        from src.inbox.conversation_script import ConversationScriptEngine
        from src.inbox.relationship_stage import compute_relationship_stage

        intimacy_score = None
        primary_name = ""
        if cs is not None:
            try:
                contact = cs.get_contact(contact_id)
                if contact:
                    primary_name = str(contact.primary_name or "")
                journey = cs.get_journey_by_contact(contact_id)
                if journey is not None:
                    intimacy_score = float(journey.intimacy_score or 0)
            except Exception:
                pass

        # 聚合该客户所有会话消息数
        message_count = 0
        conv_ids: List[str] = []
        if store is not None:
            try:
                rows = store._conn.execute(
                    "SELECT conversation_id FROM conversations WHERE contact_id = ? LIMIT 20",
                    (contact_id,),
                ).fetchall()
                conv_ids = [r["conversation_id"] for r in rows]
                if conv_ids:
                    ph = ",".join("?" * len(conv_ids))
                    message_count = store._conn.execute(
                        f"SELECT COUNT(*) as c FROM messages WHERE conversation_id IN ({ph})",
                        conv_ids,
                    ).fetchone()["c"]
            except Exception:
                pass

        rel_payload = _build_contact_relationship_payload(request, contact_id, store)
        rel = {k: v for k, v in rel_payload.items() if k not in (
            "stage_conflict", "stage_conflict_detail", "contact_updated_by",
        )}
        engine = ConversationScriptEngine()
        topics = engine.suggest_topics(
            rel.get("display_stage") or rel.get("confirmed_stage") or rel.get("stage") or "initial",
            custom_topics=store.list_script_topics() if store else [],
            limit=3,
        ).get("topics", [])

        engagement_raw = store.get_contact_engagement(contact_id) if store else None
        engagement = None
        if engagement_raw:
            from src.inbox.engagement_scorer import EngagementScorer
            ln, _ = EngagementScorer._level_for(int(engagement_raw.get("points") or 0))
            engagement = {**engagement_raw, "level_name": ln}
        active_chains: List[Dict[str, Any]] = []
        recent_notes: List[Dict[str, Any]] = []
        if store is not None and conv_ids:
            for cid in conv_ids[:5]:
                try:
                    for ex in store.get_conv_chain_executions(cid):
                        if ex.get("status") == "running":
                            active_chains.append(ex)
                    for note in store.list_conv_notes(cid, limit=5)[-3:]:
                        recent_notes.append(note)
                except Exception:
                    pass
            recent_notes.sort(key=lambda n: float(n.get("ts") or 0), reverse=True)
            recent_notes = recent_notes[:8]

        return {
            "ok": True,
            "contact_id": contact_id,
            "primary_name": primary_name,
            "relationship": rel,
            "contact_stage": rel_payload.get("contact_stage"),
            "contact_stage_label": rel_payload.get("contact_stage_label"),
            "stage_conflict": rel_payload.get("stage_conflict", False),
            "stage_conflict_detail": rel_payload.get("stage_conflict_detail"),
            "suggested_topics": topics,
            "engagement": engagement,
            "active_chains": active_chains[:10],
            "recent_notes": recent_notes,
            "conversation_ids": conv_ids,
        }

    @app.get("/api/workspace/conv/{conversation_id}/collab-context")
    async def api_conv_collab_context(conversation_id: str, request: Request):
        """EE1：会话级协作条（含 @mention 时附带阶段+话题）。"""
        api_auth(request)
        store = _inbox_store(request)
        rel = _build_relationship_stage_payload(request, conversation_id, store)
        ctx = rel.get("context") or {}
        contact_id = ctx.get("contact_id") or ""
        if contact_id:
            resp = await api_contact_collab_context(contact_id, request)
            resp["conversation_id"] = conversation_id
            resp["relationship"] = {k: v for k, v in rel.items() if k != "context"}
            return resp
        from src.inbox.conversation_script import ConversationScriptEngine
        engine = ConversationScriptEngine()
        topics = engine.suggest_topics(
            rel.get("display_stage") or rel.get("stage") or "initial",
            custom_topics=store.list_script_topics() if store else [],
            limit=3,
        ).get("topics", [])
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "relationship": {k: v for k, v in rel.items() if k != "context"},
            "suggested_topics": topics,
            "recent_notes": store.list_conv_notes(conversation_id, limit=5) if store else [],
        }
