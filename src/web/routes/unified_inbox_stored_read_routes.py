"""统一收件箱——A1 store-backed 持久化读端点 / 自动化模式路由域（巨石拆分 slice 29）。

把 ``register_unified_inbox_routes`` 巨型闭包中 A1 读路径子域整体外移为
``register_stored_read_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- A1 store-backed 读：``unified-inbox/stored-chats`` + ``unified-inbox/history``
  （直接从 InboxStore 统一事实源读会话/消息，独立于 live 聚合的 /chats、/thread）
- 自动化模式：``unified-inbox/automation`` (GET/POST)

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 29 端点契约断言）。

依赖全部朝下：services._inbox_store、aggregate.(_read/_write_automation_mode)、
helpers.AUTOMATION_MODES、normalizer.conv_id。只收 api_auth 一个参数（零闭包私有 helper）。

注：A1 的 ``unified-inbox/profile`` 端点深度耦合 live 路径 helper（_collect_all_chats /
_get_telegram_client / _message_obj），不在本刀范围，随核心 live 集群后续处理。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import conv_id as _conv_id
from src.web.routes.unified_inbox_aggregate import (
    _read_automation_mode,
    _write_automation_mode,
)
from src.web.routes.unified_inbox_helpers import AUTOMATION_MODES
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def register_stored_read_routes(app, *, api_auth) -> None:
    """挂载 A1 持久化读端点（stored-chats/history）+ 自动化模式（GET/POST）。"""

    # ── A1 读路径增量①：store-backed 持久化读端点 ──────────────────────
    # 直接从 InboxStore（统一事实源）读会话/消息，独立于 live 聚合（/chats、/thread）。
    # 价值：跨平台、跨重启的持久历史可查（蓝图 A1 验收）；不改 live 路径，零风险。

    @app.get("/api/unified-inbox/stored-chats")
    async def api_unified_inbox_stored_chats(
        request: Request, limit: int = 50, platform: str = "",
    ):
        """从持久层读会话列表（事实源），区别于实时聚合的 /chats。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "统一收件箱持久层未启用")
        limit = max(1, min(200, int(limit or 50)))
        convs = store.list_conversations(limit=limit, platform=str(platform or ""))
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            mode = _read_automation_mode(request, cid)
            c["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
            c["message_count"] = store.count_messages(cid)
        return {"ok": True, "source": "store", "count": len(convs), "chats": convs}

    @app.get("/api/unified-inbox/history")
    async def api_unified_inbox_history(
        request: Request, conversation_id: str = "", limit: int = 50,
    ):
        """从持久层读某会话的历史消息（跨重启可查），并附最近一次分析。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "统一收件箱持久层未启用")
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, "conversation_id 不能为空")
        limit = max(1, min(200, int(limit or 50)))
        conv = store.get_conversation(cid)
        if conv is None:
            return {"ok": True, "found": False, "source": "store",
                    "conversation_id": cid, "messages": [], "count": 0}
        # 取**最近** limit 条（ts 升序），而非最旧 limit 条：
        # AI 草稿/时间线都要看「当前正在聊的内容」，用最旧会导致长会话上下文错位。
        if hasattr(store, "list_recent_messages"):
            messages = store.list_recent_messages(cid, limit=limit)
        else:
            messages = store.list_messages(cid, limit=limit)
        analysis = None
        if hasattr(store, "latest_analysis"):
            try:
                analysis = store.latest_analysis(cid)
            except Exception:
                analysis = None
        return {
            "ok": True, "found": True, "source": "store",
            "conversation_id": cid, "conversation": conv,
            "messages": messages, "count": store.count_messages(cid),
            "analysis": analysis,
        }

    @app.get("/api/unified-inbox/automation")
    async def api_unified_inbox_automation_get(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
    ):
        api_auth(request)
        cid = _conv_id(str(platform or "").lower(), str(account_id or "default"), str(chat_key or ""))
        mode = _read_automation_mode(request, cid)
        return {"ok": True, "conversation_id": cid, "mode": mode}

    @app.post("/api/unified-inbox/automation")
    async def api_unified_inbox_automation_set(request: Request, _=Depends(api_auth)):
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        mode = str(body.get("mode") or "review")
        if not platform or not chat_key:
            raise HTTPException(400, "platform 和 chat_key 不能为空")
        if mode not in AUTOMATION_MODES:
            raise HTTPException(400, f"不支持的自动化模式: {mode}")
        cid = _conv_id(platform, account_id, chat_key)
        _write_automation_mode(request, cid, mode)
        return {"ok": True, "conversation_id": cid, "mode": mode}

    @app.get("/api/unified-inbox/automation-stats")
    async def api_unified_inbox_automation_stats(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 30,
    ):
        """全自动安全条：本会话今日自动发/拦截统计 + 近期审计记录（draft_audit_log）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "统一收件箱持久层未启用")
        cid = _conv_id(str(platform or "").lower(), str(account_id or "default"), str(chat_key or ""))
        now = datetime.now()
        since_ts = datetime(now.year, now.month, now.day).timestamp()
        stats = store.get_conversation_automation_stats(cid, since_ts=since_ts)
        recent = store.list_draft_audit(
            conversation_id=cid, since_ts=since_ts, limit=max(1, min(100, int(limit or 30))),
        )
        return {
            "ok": True,
            "conversation_id": cid,
            "since_ts": since_ts,
            "stats": stats,
            "recent": recent,
        }
