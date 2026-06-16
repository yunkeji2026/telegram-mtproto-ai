"""统一收件箱——坐席协作注解 / @mention 智能路由路由域（巨石拆分 slice 24）。

把 ``register_unified_inbox_routes`` 巨型闭包中相邻的「坐席协作」两段子域整体外移为
``register_collab_mention_routes(app, *, api_auth, config_manager)``，由主 register 在
**原位置**调用：

- Phase 48 @mention 智能路由：``conv/{id}/mention-suggestions``
- Phase 25 坐席协作注解：``conv/{id}/notes`` (GET/POST) + ``conv/{id}/notes/{note_id}``
  (PATCH/DELETE)

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 24 端点契约断言）。

依赖全部朝下：services._inbox_store、auth.(_session_agent/_user_store_from_config)、
context.(_conv_relationship_context/_mention_context_for_conv)；MentionRouter /
AgentCoordinator / event_bus / relationship_stage 均为 handler 内局部 import。
闭包级依赖仅 config_manager，经子注册显式入参传入（零闭包私有 helper）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _session_agent, _user_store_from_config
from src.web.routes.unified_inbox_context import (
    _conv_relationship_context,
    _mention_context_for_conv,
)
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def register_collab_mention_routes(app, *, api_auth, config_manager) -> None:
    """挂载 @mention 智能路由 + 坐席协作注解 CRUD 端点。"""

    # ─── Phase 25: 坐席协作注解 ─────────────────────────────────────────

    # ─── Phase 48: @mention 智能路由 ─────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/mention-suggestions")
    async def api_conv_mention_suggestions(
        conversation_id: str,
        request: Request,
        q: str = "",
        limit: int = 8,
    ):
        """P48：按关系阶段 + 负荷 + QA 推荐 @ 坐席。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "suggestions": [], "auto_cc": []}
        from src.inbox.mention_router import MentionRouter
        from src.workspace.agent_coordinator import AgentCoordinator

        mctx = _mention_context_for_conv(request, conversation_id, store)
        coord = AgentCoordinator.from_request(request, config_manager)
        users = []
        us = _user_store_from_config(config_manager)
        if us is not None:
            try:
                users = us.list_users()
            except Exception:
                pass
        router = MentionRouter.from_store(
            store, presence=coord.list_presence(), users=users,
        )
        me = _session_agent(request)["agent_id"]
        result = router.suggest(
            stage=mctx["stage"],
            stage_label=mctx["stage_label"],
            churn_level=mctx["churn_level"],
            claim_agent_id=mctx["claim_agent_id"],
            overdue_chain=mctx["overdue_chain"],
            exclude_agent_id=me,
            query=q,
            limit=limit,
        )
        return {"ok": True, "conversation_id": conversation_id, **result, "context": mctx}

    @app.get("/api/workspace/conv/{conversation_id}/notes")
    async def api_conv_notes_list(conversation_id: str, request: Request, limit: int = 50):
        """V1：获取会话内部注解列表（坐席可见，客户不可见）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        notes = store.list_conv_notes(conversation_id, limit=limit)
        return {"ok": True, "notes": notes, "count": len(notes)}

    @app.post("/api/workspace/conv/{conversation_id}/notes")
    async def api_conv_notes_add(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：在会话中添加内部注解。

        Body: {body: str, mentions: [agent_id, ...] (可选)}
        """
        body_data = await request.json()
        text = str(body_data.get("body", "")).strip()
        if not text:
            raise HTTPException(422, "body 不能为空")
        mentions = [str(m) for m in (body_data.get("mentions") or []) if str(m).strip()]
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        # P48：高流失 + 高阶段自动抄送主管（若尚未 @）
        auto_cc_applied: List[Dict[str, Any]] = []
        try:
            from src.inbox.mention_router import MentionRouter
            from src.workspace.agent_coordinator import AgentCoordinator
            mctx = _mention_context_for_conv(request, conversation_id, store)
            users = []
            us = _user_store_from_config(config_manager)
            if us is not None:
                users = us.list_users()
            coord = AgentCoordinator.from_request(request, config_manager)
            router = MentionRouter.from_store(
                store, presence=coord.list_presence(), users=users,
            )
            sugg = router.suggest(
                stage=mctx["stage"],
                stage_label=mctx["stage_label"],
                churn_level=mctx["churn_level"],
                claim_agent_id=mctx["claim_agent_id"],
                overdue_chain=mctx["overdue_chain"],
            )
            mention_set = set(mentions)
            for cc in sugg.get("auto_cc") or []:
                cc_id = str(cc.get("agent_id") or "")
                if cc_id and cc_id not in mention_set:
                    mentions.append(cc_id)
                    mention_set.add(cc_id)
                    auto_cc_applied.append(cc)
        except Exception:
            pass
        # 从 session 取当前坐席身份
        agent_id = str(request.session.get("user_name") or request.session.get("username") or "")
        agent_name = str(request.session.get("display_name") or agent_id)
        try:
            note = store.add_conv_note(
                conversation_id, text,
                agent_id=agent_id, agent_name=agent_name, mentions=mentions,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        # P25：通过事件总线广播注解事件（@提及 → SSE 通知目标坐席）
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            # P45：@mention 时附带协作上下文（阶段 + 推荐话题）
            collab_hint = ""
            try:
                ctx = _conv_relationship_context(request, conversation_id, store)
                from src.inbox.relationship_stage import compute_relationship_stage
                rel = compute_relationship_stage(
                    exchange_count=ctx["exchange_count"],
                    intimacy_score=ctx["intimacy_score"],
                )
                collab_hint = rel.get("stage_label") or ""
            except Exception:
                pass
            get_event_bus().publish("conv_note", {
                **note,
                "conversation_id": conversation_id,
                "stage_label": collab_hint,
                "ts": _t.time(),
            })
        except Exception:
            pass
        return {"ok": True, "note": note, "auto_cc": auto_cc_applied}

    @app.patch("/api/workspace/conv/{conversation_id}/notes/{note_id}")
    async def api_conv_notes_edit(
        conversation_id: str, note_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：编辑注解内容。"""
        body_data = await request.json()
        text = str(body_data.get("body", "")).strip()
        if not text:
            raise HTTPException(422, "body 不能为空")
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        agent_id = str(request.session.get("user_name") or "")
        ok = store.edit_conv_note(note_id, text, agent_id=agent_id)
        if not ok:
            raise HTTPException(404, "注解不存在")
        return {"ok": True, "note_id": note_id}

    @app.delete("/api/workspace/conv/{conversation_id}/notes/{note_id}")
    async def api_conv_notes_delete(
        conversation_id: str, note_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：删除注解。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        agent_id = str(request.session.get("user_name") or "")
        ok = store.delete_conv_note(note_id, agent_id=agent_id)
        if not ok:
            raise HTTPException(404, "注解不存在")
        return {"ok": True, "note_id": note_id}
