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
        paused = []
        dispatcher = getattr(request.app.state, "deferred_outbox_dispatcher", None)
        if dispatcher is not None:
            try:
                senders = dispatcher.registered_platforms()
            except Exception:
                senders = []
            try:
                paused = dispatcher.paused_platforms()
            except Exception:
                paused = []
        return {
            "ok": True,
            "enabled": True,
            "stats": stats,
            "senders": senders,
            "paused": paused,
            "recent": recent,
        }

    # ── 运营动作（mutate）：重试 / 取消积压 / 暂停·恢复平台 ──────────
    async def _body(request: Request) -> dict:
        try:
            return await request.json() or {}
        except Exception:
            return {}

    def _require_store(request: Request):
        return getattr(request.app.state, "deferred_outbox_store", None)

    @app.post("/api/deferred-outbox/retry")
    async def api_deferred_outbox_retry(request: Request, _=Depends(api_auth)):
        """重试终态消息：body {id:N} 单条，或 {status:"failed|expired|cancelled"} 批量。"""
        store = _require_store(request)
        if store is None:
            return {"ok": False, "enabled": False, "error": "queue_not_enabled"}
        body = await _body(request)
        row_id = int(body.get("id") or 0)
        status = str(body.get("status") or "").strip()
        try:
            if row_id > 0:
                ok = store.requeue(row_id)
                return {"ok": ok, "requeued": 1 if ok else 0}
            if status:
                n = store.requeue_status(status)
                return {"ok": True, "requeued": n}
            return {"ok": False, "error": "need id or status"}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    @app.post("/api/deferred-outbox/cancel")
    async def api_deferred_outbox_cancel(request: Request, _=Depends(api_auth)):
        """取消 pending 积压：body {reason:"no_sender"} 和/或 {platform:"line"}。

        必须至少给一个过滤条件，避免误清空整个队列。
        """
        store = _require_store(request)
        if store is None:
            return {"ok": False, "enabled": False, "error": "queue_not_enabled"}
        body = await _body(request)
        reason = str(body.get("reason") or "").strip()
        platform = str(body.get("platform") or "").strip()
        if not reason and not platform:
            return {"ok": False, "error": "need reason or platform"}
        try:
            n = store.cancel_pending(reason=reason, platform=platform)
            return {"ok": True, "cancelled": n}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    @app.post("/api/deferred-outbox/pause")
    async def api_deferred_outbox_pause(request: Request, _=Depends(api_auth)):
        """暂停某平台投递：body {platform:"line"}（pending 不丢，逐 tick 推后）。"""
        dispatcher = getattr(request.app.state, "deferred_outbox_dispatcher", None)
        if dispatcher is None:
            return {"ok": False, "enabled": False, "error": "queue_not_enabled"}
        body = await _body(request)
        platform = str(body.get("platform") or "").strip()
        if not platform:
            return {"ok": False, "error": "need platform"}
        dispatcher.pause(platform)
        return {"ok": True, "paused": dispatcher.paused_platforms()}

    @app.post("/api/deferred-outbox/resume")
    async def api_deferred_outbox_resume(request: Request, _=Depends(api_auth)):
        """恢复某平台投递：body {platform:"line"}。"""
        dispatcher = getattr(request.app.state, "deferred_outbox_dispatcher", None)
        if dispatcher is None:
            return {"ok": False, "enabled": False, "error": "queue_not_enabled"}
        body = await _body(request)
        platform = str(body.get("platform") or "").strip()
        if not platform:
            return {"ok": False, "error": "need platform"}
        dispatcher.resume(platform)
        return {"ok": True, "paused": dispatcher.paused_platforms()}


__all__ = ["register_deferred_outbox_routes"]
