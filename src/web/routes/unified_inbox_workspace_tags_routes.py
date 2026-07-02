"""统一收件箱——标签体系 / 会话级标签·摘要·归档路由域（巨石拆分 slice 15）。

把"标签聚合(tags/tag-stats) + 预设标签库 CRUD(tag-library) + 会话级 AI 摘要 / 标签读写 /
归档(conv/{id}/summarize|tags|archive)"这一内聚子域，从 ``register_unified_inbox_routes``
巨型闭包中外移为 ``register_workspace_tags_routes(app, *, api_auth)``，由主 register 在
**原位置**顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：services 存储/聊天助手、normalizer.store_message_to_obj；event_bus 为 handler
内局部 import（P28 标签/归档事件外发）。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import store_message_to_obj
from src.web.routes.unified_inbox_services import (
    _contacts_gateway,
    _contacts_store,
    _get_chat_assistant_service,
    _inbox_store,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_workspace_tags_routes(app, *, api_auth) -> None:
    """挂载标签体系 / 会话级标签·摘要·归档端点（/api/workspace/tags|tag-stats|tag-library*、conv/{id}/summarize|tags|archive）。"""

    @app.get("/api/workspace/tags")
    async def api_workspace_tags(request: Request, limit: int = 100):
        """全部标签 + 使用计数 + 预设库颜色（标签自动补全/快筛/上色）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tags": []}
        return {"ok": True, "tags": store.list_all_tags(limit=max(1, min(300, int(limit or 100))))}

    @app.get("/api/workspace/tag-stats")
    async def api_workspace_tag_stats(request: Request):
        """T2：会话级标签统计（count / unread / platforms），用于概览 strip。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "stats": []}
        try:
            stats = inbox.tag_stats()
        except Exception:
            logger.debug("tag-stats 失败（已忽略）", exc_info=True)
            stats = []
        return {"ok": True, "stats": stats}

    @app.get("/api/workspace/tag-library")
    async def api_workspace_tag_library_list(request: Request):
        """预设标签库（名称/颜色/排序）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "library": []}
        return {"ok": True, "library": gw.list_tag_library()}

    @app.post("/api/workspace/tag-library")
    async def api_workspace_tag_library_upsert(request: Request, _=Depends(api_auth)):
        """新增/更新预设标签：{tag, color?, sort_order?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        tag = str(body.get("tag") or "").strip()
        if not tag:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="tag"))
        ok = gw.upsert_tag_library(
            tag, color=str(body.get("color") or ""),
            sort_order=int(body.get("sort_order") or 0),
        )
        return {"ok": ok, "library": gw.list_tag_library()}

    @app.delete("/api/workspace/tag-library/{tag}")
    async def api_workspace_tag_library_delete(
        tag: str, request: Request, _=Depends(api_auth),
    ):
        """从预设库删除一个标签（不影响已打在客户上的标签）。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        return {"ok": gw.delete_tag_library(tag), "library": gw.list_tag_library()}

    # ── T1: 会话级标签 + 归档 API ─────────────────────────────────────

    @app.post("/api/workspace/conv/{conversation_id}/summarize")
    async def api_conv_summarize(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """Phase 19：为会话生成 AI 摘要并持久化到 conversation_meta.summary。

        调用 ChatAssistantService.analyze（与 inbox/analyze 同服务），
        以会话最近30条消息作为上下文，生成一句话概括。结果写库后返回。
        """
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        msgs = store.list_recent_messages(conversation_id, limit=30)
        if not msgs:
            return {"ok": True, "summary": ""}
        msg_objs = [store_message_to_obj(r) for r in msgs]
        # 取最后一条入站文字为代表性文本
        last_in = next((m for m in reversed(msg_objs) if m.get("direction") == "in"
                        and m.get("text")), None)
        text = str((last_in or msg_objs[-1]).get("text") or "")
        try:
            svc = _get_chat_assistant_service(request)
            analysis = await svc.analyze(text=text, messages=msg_objs)
            summary = str(getattr(analysis, "summary", "") or "").strip()
            if not summary:
                # Fallback: truncate last user message as summary
                summary = text[:80] + ("…" if len(text) > 80 else "")
        except Exception:
            logger.debug("conv summarize AI 调用失败（已忽略）", exc_info=True)
            summary = text[:80] + ("…" if len(text) > 80 else "")
        store.save_conv_summary(conversation_id, summary)
        return {"ok": True, "summary": summary}

    @app.get("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_get(conversation_id: str, request: Request):
        """T1：获取单个会话的标签列表。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "tags": []}
        return {"ok": True, "tags": store.get_conv_tags(conversation_id)}

    @app.put("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_put(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：覆写会话标签列表，广播 conv_tagged 事件供 Webhook 外发。"""
        body = await request.json()
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        ok = store.set_conv_tags(conversation_id, tags)
        # P28：广播标签变更事件
        if ok:
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_tagged", {
                    "conversation_id": conversation_id,
                    "tags": tags,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "tags": tags}

    @app.patch("/api/workspace/conv/{conversation_id}/archive")
    async def api_conv_archive(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：归档/取消归档会话，并广播 conv_archived 事件供 Webhook 外发。"""
        body = await request.json()
        archived = bool(body.get("archived", True))
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        ok = store.set_conv_archived(conversation_id, archived)
        if ok:
            # P34：归档时自动触发 QA 评分计算（异步非阻塞）
            if archived:
                try:
                    import asyncio as _aio
                    _aio.get_event_loop().run_in_executor(
                        None, store.compute_and_store_qa_score, conversation_id
                    )
                except Exception:
                    pass
            # P28：广播会话归档事件（修正 EventBus API 调用签名）
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_archived", {
                    "conversation_id": conversation_id,
                    "archived": archived,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "archived": archived}
