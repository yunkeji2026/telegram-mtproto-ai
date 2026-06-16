"""统一收件箱——批量操作 / 通知中心路由域（巨石拆分 slice 26）。

把 ``register_unified_inbox_routes`` 巨型闭包中相邻的两段子域整体外移为
``register_batch_notif_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 23 批量操作：``batch/archive`` + ``batch/tags`` + ``batch/assign``
- Phase 24 通知中心：``notifications`` (GET) + ``notifications/read`` (POST)
  （SSE 实时推送在别处；此处仅断线重连后的历史同步 + badge 已读标记）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 26 端点契约断言）。

依赖全部朝下：services._inbox_store（通知中心仅读 app.state.notif_queue）。
只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging

from fastapi import Depends, Request

from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def register_batch_notif_routes(app, *, api_auth) -> None:
    """挂载批量操作（归档/标签/分配）+ 通知中心（历史/已读）端点。"""

    # ─── Phase 23: 批量操作 ─────────────────────────────────────────────────

    @app.post("/api/workspace/batch/archive")
    async def api_batch_archive(request: Request, _=Depends(api_auth)):
        """P23：批量归档/取消归档会话。

        Body: {conversation_ids: [str, ...], archived: bool}
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        archived = bool(body.get("archived", True))
        if not cids:
            return {"ok": False, "error": "conversation_ids 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        updated = 0
        for cid in cids[:200]:  # 单次上限 200 条
            try:
                ok = store.set_conv_archived(cid, archived)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "archived": archived}

    @app.post("/api/workspace/batch/tags")
    async def api_batch_tags(request: Request, _=Depends(api_auth)):
        """P23：批量修改会话标签。

        Body: {conversation_ids: [str, ...], tags: [str, ...],
               mode: 'set'|'add'|'remove'}
          mode=set  → 替换全部标签
          mode=add  → 追加（去重）
          mode=remove → 删除指定标签
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        mode = str(body.get("mode", "add")).lower()
        if mode not in ("set", "add", "remove"):
            mode = "add"
        if not cids:
            return {"ok": False, "error": "conversation_ids 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        import json as _json
        updated = 0
        for cid in cids[:200]:
            try:
                current = store.get_conv_tags(cid) or []
                if mode == "set":
                    new_tags = tags
                elif mode == "add":
                    new_tags = list(dict.fromkeys(current + tags))  # 保序去重
                else:  # remove
                    rm = set(tags)
                    new_tags = [t for t in current if t not in rm]
                ok = store.set_conv_tags(cid, new_tags)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "mode": mode}

    @app.post("/api/workspace/batch/assign")
    async def api_batch_assign(request: Request, _=Depends(api_auth)):
        """P23：批量分配会话给坐席。

        Body: {conversation_ids: [str, ...], agent_id: str}
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        agent_id = str(body.get("agent_id") or "").strip()
        if not cids or not agent_id:
            return {"ok": False, "error": "conversation_ids / agent_id 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        updated = 0
        for cid in cids[:200]:
            try:
                store.update_conv_meta(cid, {"claimed_by": agent_id})
                updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "agent_id": agent_id}

    # ─── Phase 24: 通知中心（SSE 事件广播） ───────────────────────────────

    @app.get("/api/workspace/notifications")
    async def api_workspace_notifications(
        request: Request,
        limit: int = 50,
    ):
        """P24：获取最近通知（SSE 事件历史，存于内存队列）。

        前端在 SSE 断线重连后调用此接口同步缺漏事件。
        """
        # 通知队列挂在 app.state.notif_queue（由 SSE 推送时顺带写入）
        queue: list = getattr(request.app.state, "notif_queue", [])
        limit = max(1, min(200, int(limit or 50)))
        return {"ok": True, "notifications": queue[-limit:]}

    @app.post("/api/workspace/notifications/read")
    async def api_workspace_notifications_read(request: Request, _=Depends(api_auth)):
        """P24：标记所有通知为已读（仅清除前端 badge，不删除历史）。"""
        return {"ok": True, "read_at": int(__import__("time").time() * 1000)}
