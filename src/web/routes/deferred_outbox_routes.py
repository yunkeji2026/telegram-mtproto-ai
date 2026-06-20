"""多平台 deferred 发送队列 · 运营可观测性 API（只读）。

让运营看到非 messenger 主动消息「排了多少 / 发了多少 / 卡在哪道护栏 / 各平台 sender
是否就绪」。store/dispatcher 经 `app.state` 注入（main.py `_ensure_deferred_outbox`）；
未启用（功能关）→ 端点返回 `enabled:false`，不报错。读经 `api_auth`（后台管理面）。
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_deferred_outbox_routes(app, *, api_auth) -> None:
    @app.get("/api/deferred-outbox/status")
    async def api_deferred_outbox_status(
        request: Request, limit: int = 50, _=Depends(api_auth),
    ):
        """队列总览：各状态计数 + pending 按平台/护栏分组 + 最近条目 + sender 就绪。"""
        store = getattr(request.app.state, "deferred_outbox_store", None)
        if store is None:
            return {"ok": True, "enabled": False,
                    "message": "多平台 deferred 队列未启用（companion.multiplatform_deferred.enabled=false）"}
        try:
            stats = store.stats()
        except Exception as ex:
            return {"ok": False, "enabled": True, "error": f"{type(ex).__name__}:{ex}"}

        lim = max(1, min(int(limit or 50), 200))
        recent = []
        try:
            for r in store.list_recent(limit=lim):
                recent.append({
                    "id": r.get("id"),
                    "platform": r.get("platform"),
                    "account_id": r.get("account_id"),
                    "chat_key": r.get("chat_key"),
                    "status": r.get("status"),
                    "reason": r.get("reason"),
                    "attempts": r.get("attempts"),
                    "defer_until": r.get("defer_until"),
                    "created_at": r.get("created_at"),
                    "sent_at": r.get("sent_at"),
                    "error": r.get("error"),
                    # 不回 reply_text 全文（隐私）：只给长度提示
                    "reply_len": len(str(r.get("reply_text") or "")),
                })
        except Exception:
            logger.debug("deferred-outbox recent 读取失败", exc_info=True)

        senders = []
        dispatcher = getattr(request.app.state, "deferred_outbox_dispatcher", None)
        if dispatcher is not None:
            try:
                senders = dispatcher.registered_platforms()
            except Exception:
                senders = []
        return {
            "ok": True,
            "enabled": True,
            "stats": stats,
            "senders": senders,
            "recent": recent,
        }


__all__ = ["register_deferred_outbox_routes"]
