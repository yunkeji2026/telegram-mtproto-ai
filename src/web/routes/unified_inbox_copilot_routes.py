"""统一收件箱——情感陪伴剧本引擎 / 客户互动积分 / 坐席 AI 副驾路由域（巨石拆分 slice 19）。

把三段连续且共享依赖面的子域，从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_copilot_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用：

- Phase 40 情感陪伴剧本引擎：``conv/{id}/script-suggestions`` + ``script-topics`` CRUD
- Phase 41 客户互动积分与成就：``contact/{id}/engagement`` (GET/POST)
- Phase 42 坐席 AI 副驾（打字辅助）：``conv/{id}/copilot-prefill`` + ``conv/{id}/reply-suggest``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + copilot 链路专项断言）。

依赖全部朝下：context Copilot 族（slice 4 已成模块：_build_copilot_context /
_maybe_polish_copilot / _record_copilot_impression_if_prefill）、auth._agent_from_request、
services 存储；剧本/积分/副驾引擎均为 handler 内局部 import。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_context import (
    _build_copilot_context,
    _maybe_polish_copilot,
    _record_copilot_impression_if_prefill,
)
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store

logger = logging.getLogger(__name__)


def register_copilot_routes(app, *, api_auth) -> None:
    """挂载剧本引擎 / 互动积分 / AI 副驾端点（script-suggestions、script-topics*、engagement、copilot-prefill、reply-suggest）。"""

    # ─── Phase 40: 情感陪伴剧本引擎 ─────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/script-suggestions")
    async def api_conv_script_suggestions(conversation_id: str, request: Request):
        """CC1：按关系阶段推荐话题切入点（内置 + 自定义）。"""
        api_auth(request)
        store = _inbox_store(request)
        from src.inbox.conversation_script import ConversationScriptEngine

        message_count = 0
        last_msg_text = ""
        intimacy_score: Optional[float] = None
        exchange_count = 0
        reunion = False

        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
                message_count = store._conn.execute(
                    "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()["c"]
                if rows:
                    for r in rows:
                        if r["direction"] in ("in", "inbound"):
                            last_msg_text = str(r["text"] or "")
                            break
                meta = store.get_conv_meta(conversation_id) or {}
                contact_id = str(meta.get("contact_id") or "")
                if contact_id:
                    cs = _contacts_store(request)
                    if cs is not None:
                        try:
                            journey = cs.get_journey_by_contact(contact_id)
                            if journey is not None:
                                intimacy_score = float(journey.intimacy_score or 0)
                        except Exception:
                            pass
                exchange_count = max(0, message_count // 2)
            except Exception:
                logger.debug("script-suggestions 上下文失败", exc_info=True)

        custom = store.list_script_topics() if store else []
        engine = ConversationScriptEngine()
        stage = engine.derive_stage_from_signals(
            exchange_count=exchange_count, intimacy_score=intimacy_score,
        )
        result = engine.suggest_topics(
            stage,
            custom_topics=custom,
            last_msg_text=last_msg_text,
            message_count=message_count,
            reunion=reunion,
            limit=6,
        )
        return {"ok": True, "conversation_id": conversation_id, **result}

    @app.get("/api/workspace/script-topics")
    async def api_script_topics_list(request: Request, stage: str = ""):
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "topics": []}
        return {"ok": True, "topics": store.list_script_topics(stage=stage)}

    @app.post("/api/workspace/script-topics")
    async def api_script_topics_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        topic_id = store.upsert_script_topic(body)
        return {"ok": True, "topic_id": topic_id}

    @app.put("/api/workspace/script-topics/{topic_id}")
    async def api_script_topics_update(topic_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["topic_id"] = topic_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_script_topic(body)
        return {"ok": True, "topic_id": topic_id}

    @app.delete("/api/workspace/script-topics/{topic_id}")
    async def api_script_topics_delete(topic_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_script_topic(topic_id)
        return {"ok": ok}

    # ─── Phase 41: 客户互动积分与成就 ───────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_get(contact_id: str, request: Request):
        """CC1:读取客户互动积分（无则返回空）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "engagement": None}
        data = store.get_contact_engagement(contact_id)
        if data is None:
            return {"ok": True, "contact_id": contact_id, "engagement": None, "computed": False}
        from src.inbox.engagement_scorer import _ACHIEVEMENT_DEFS, EngagementScorer
        level_name, _ = EngagementScorer._level_for(int(data.get("points") or 0))
        ach_details = [
            {**_ACHIEVEMENT_DEFS.get(aid, {"name": aid, "icon": "🏅", "desc": ""}),
             "id": aid, "unlocked": True}
            for aid in (data.get("achievements") or [])
        ]
        for aid, defn in _ACHIEVEMENT_DEFS.items():
            if aid not in (data.get("achievements") or []):
                ach_details.append({**defn, "id": aid, "unlocked": False})
        return {
            "ok": True,
            "contact_id": contact_id,
            "computed": True,
            "engagement": {
                **data,
                "level_name": level_name,
                "achievement_details": ach_details,
                "is_vip": int(data.get("points") or 0) >= 600,
            },
        }

    @app.post("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_compute(contact_id: str, request: Request, _=Depends(api_auth)):
        """CC1:重新计算并存储互动积分。"""
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        result = store.compute_and_store_engagement(contact_id)
        return {"ok": True, "contact_id": contact_id, "engagement": result}

    # ─── Phase 42: 坐席 AI 副驾（打字辅助） ─────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/copilot-prefill")
    async def api_conv_copilot_prefill(
        conversation_id: str,
        request: Request,
        trigger: str = "open",
        workflow_text: str = "",
        workflow_chain_name: str = "",
        workflow_step: int = 0,
        mention_body: str = "",
        mention_from: str = "",
        polish: bool = True,
    ):
        """P49/P52：事件驱动 Copilot 预填（可选 LLM 润色）。"""
        api_auth(request)
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=trigger.strip(),
            workflow_text=workflow_text,
            workflow_chain_name=workflow_chain_name,
            workflow_step=workflow_step,
            mention_body=mention_body,
            mention_from=mention_from,
        )
        last_customer = ""
        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 20""",
                    (conversation_id,),
                ).fetchall()
                for r in rows:
                    if r["direction"] in ("in", "inbound") and r["text"]:
                        last_customer = str(r["text"])
                        break
            except Exception:
                pass
        templates: List[Dict[str, Any]] = []
        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass
        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text="",
            last_customer_msg=last_customer,
            stage=ctx["stage"],
            templates=templates,
            context=ctx,
            limit=4,
        )
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "trigger": ctx.get("trigger") or trigger,
            "stage": ctx["stage"],
            **result,
            "context": ctx,
        }
        payload = await _maybe_polish_copilot(
            request, payload,
            conversation_id=conversation_id,
            partial_text="",
            last_customer_msg=last_customer,
            polish_requested=bool(polish),
        )
        agent_id, _ = _agent_from_request(request)
        _record_copilot_impression_if_prefill(
            store, conversation_id, agent_id, payload, partial_text="",
        )
        return payload

    @app.post("/api/workspace/conv/{conversation_id}/reply-suggest")
    async def api_conv_reply_suggest(conversation_id: str, request: Request, _=Depends(api_auth)):
        """CC1/P49：实时回复补全（规则 + 模板 + 阶段/工作链/@mention 联动）。"""
        body = await request.json()
        partial = str(body.get("partial") or body.get("text") or "")
        recent = body.get("messages") if isinstance(body.get("messages"), list) else []

        last_customer = ""
        for m in reversed(recent):
            if isinstance(m, dict) and m.get("direction") in ("in", "inbound") and m.get("text"):
                last_customer = str(m["text"])
                break

        templates: List[Dict[str, Any]] = []
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=str(body.get("trigger") or ""),
            workflow_text=str(body.get("workflow_text") or ""),
            workflow_chain_name=str(body.get("workflow_chain_name") or ""),
            workflow_step=int(body.get("workflow_step") or 0),
            mention_body=str(body.get("mention_body") or ""),
            mention_from=str(body.get("mention_from") or ""),
        ) if store is not None else {}

        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass

        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text=partial,
            last_customer_msg=last_customer,
            stage=ctx.get("stage") or "initial",
            recent_messages=recent,
            templates=templates,
            context=ctx,
            limit=4 if not partial else 3,
        )
        polish_req = bool(body.get("polish"))
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "partial": partial,
            "stage": ctx.get("stage") or "initial",
            **result,
            "context": ctx,
        }
        if polish_req and not partial.strip():
            payload = await _maybe_polish_copilot(
                request, payload,
                conversation_id=conversation_id,
                partial_text=partial,
                last_customer_msg=last_customer,
                polish_requested=True,
            )
        else:
            payload["polished"] = False
        if not partial.strip() and store is not None:
            agent_id, _ = _agent_from_request(request)
            _record_copilot_impression_if_prefill(
                store, conversation_id, agent_id, payload, partial_text=partial,
            )
        return payload
