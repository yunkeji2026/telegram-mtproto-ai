"""统一收件箱路由 — 聚合所有平台最近消息/对话 + 跨平台发送。

端点：
  GET  /unified-inbox                   — 页面
  GET  /api/unified-inbox/chats         — 各平台最近对话列表（聚合）
  POST /api/unified-inbox/send          — 发送消息到指定平台/账号
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


# ── 服务获取帮助 ─────────────────────────────────────────────────────────

def _get_line_services(request: Request) -> list:
    svcs = getattr(request.app.state, "line_rpa_services", None)
    if svcs:
        return list(svcs)
    s = getattr(request.app.state, "line_rpa_service", None)
    return [s] if s else []


def _get_whatsapp_services(request: Request) -> list:
    svcs = getattr(request.app.state, "whatsapp_rpa_services", None)
    if svcs:
        return list(svcs)
    s = getattr(request.app.state, "whatsapp_rpa_service", None)
    return [s] if s else []


def _get_messenger_service(request: Request):
    return getattr(request.app.state, "messenger_rpa_service", None)


def _get_telegram_client(request: Request):
    return getattr(request.app.state, "telegram_client", None)


# ── 数据聚合 ─────────────────────────────────────────────────────────────

def _collect_all_chats(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    """从所有平台/账号收集最近对话，返回统一格式列表。"""
    out: List[Dict[str, Any]] = []

    # LINE 账号
    for svc in _get_line_services(request):
        if svc is None:
            continue
        aid = getattr(svc, "account_id", "default")
        try:
            label = (svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}).get("label") or aid
        except Exception:
            label = aid
        try:
            chats = svc.list_chats(limit) or []
        except Exception as ex:
            logger.debug("LINE list_chats [%s] 失败: %s", aid, ex)
            chats = []
        for c in chats:
            out.append({
                "platform": "line",
                "platform_name": "LINE",
                "account_id": aid,
                "account_label": label,
                "chat_key": c.get("chat_key") or c.get("name") or "",
                "name": c.get("name") or c.get("chat_key") or "",
                "last_msg": c.get("last_peer_text") or c.get("last_text") or "",
                "last_ts": c.get("last_ts") or c.get("ts") or 0,
                "unread": c.get("unread_count") or 0,
                "source": c,
            })

    # WhatsApp 账号
    for svc in _get_whatsapp_services(request):
        if svc is None:
            continue
        aid = getattr(svc, "account_id", "default")
        try:
            label = (svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}).get("label") or aid
        except Exception:
            label = aid
        try:
            # WA service 使用 list_pending 获取等待回复的对话
            rows = svc.list_pending(status="pending", limit=limit) or []
            chats = [{"chat_key": r.get("chat_key") or r.get("peer_name", ""),
                      "name": r.get("peer_name") or r.get("chat_key") or "",
                      "last_msg": r.get("peer_text") or "",
                      "last_ts": r.get("ts") or 0,
                      "unread": 1} for r in rows]
        except Exception as ex:
            logger.debug("WA list_pending [%s] 失败: %s", aid, ex)
            chats = []
        for c in chats:
            out.append({
                "platform": "whatsapp",
                "platform_name": "WhatsApp",
                "account_id": aid,
                "account_label": label,
                "chat_key": c.get("chat_key", ""),
                "name": c.get("name", ""),
                "last_msg": c.get("last_msg", ""),
                "last_ts": c.get("last_ts", 0),
                "unread": c.get("unread", 0),
                "source": c,
            })

    # Messenger
    msvc = _get_messenger_service(request)
    if msvc is not None:
        try:
            rows = msvc.list_approvals(status="pending", limit=limit) if hasattr(msvc, "list_approvals") else []
        except Exception as ex:
            logger.debug("Messenger list_approvals 失败: %s", ex)
            rows = []
        for r in rows or []:
            out.append({
                "platform": "messenger",
                "platform_name": "Messenger",
                "account_id": r.get("account_id") or "default",
                "account_label": r.get("account_id") or "Messenger",
                "chat_key": r.get("chat_key") or r.get("name", ""),
                "name": r.get("name") or r.get("chat_key", ""),
                "last_msg": r.get("peer_text") or "",
                "last_ts": r.get("ts") or 0,
                "unread": 1,
                "source": r,
            })

    # Telegram 最近消息（如有 event_tracker）
    client = _get_telegram_client(request)
    if client is not None:
        try:
            recent = getattr(client, "_recent_messages", None) or []
            for m in list(recent)[-limit:]:
                out.append({
                    "platform": "telegram",
                    "platform_name": "Telegram",
                    "account_id": "default",
                    "account_label": "Telegram",
                    "chat_key": str(m.get("chat_id") or ""),
                    "name": m.get("user_name") or m.get("chat_name") or str(m.get("chat_id", "")),
                    "last_msg": m.get("text") or "",
                    "last_ts": m.get("ts") or 0,
                    "unread": 1,
                    "source": m,
                })
        except Exception:
            pass

    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    return out[:limit * 4]


# ── 路由注册 ─────────────────────────────────────────────────────────────

def register_unified_inbox_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager=None,
):
    """挂载统一收件箱路由到 FastAPI app。"""

    @app.get("/unified-inbox", response_class=HTMLResponse)
    async def unified_inbox_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "unified_inbox.html", {})

    @app.get("/api/unified-inbox/chats")
    async def api_unified_inbox_chats(request: Request, limit: int = 30):
        api_auth(request)
        limit = max(5, min(100, int(limit or 30)))
        chats = _collect_all_chats(request, limit=limit)
        # 平台摘要（running 状态）
        platform_status: Dict[str, Any] = {}
        for svc in _get_line_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            try:
                st = svc.status()
                platform_status[f"line_{aid}"] = {
                    "platform": "line",
                    "account_id": aid,
                    "label": (svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}).get("label") or aid,
                    "running": st.get("running", False),
                    "serial": st.get("serial") or "",
                }
            except Exception:
                pass
        for svc in _get_whatsapp_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            try:
                st = svc.status()
                platform_status[f"wa_{aid}"] = {
                    "platform": "whatsapp",
                    "account_id": aid,
                    "label": (svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}).get("label") or aid,
                    "running": st.get("running", False),
                    "serial": st.get("serial") or "",
                }
            except Exception:
                pass
        msvc = _get_messenger_service(request)
        if msvc:
            try:
                platform_status["messenger"] = {
                    "platform": "messenger",
                    "account_id": "default",
                    "label": "Messenger",
                    "running": msvc.is_running if hasattr(msvc, "is_running") else False,
                }
            except Exception:
                pass
        tg = _get_telegram_client(request)
        platform_status["telegram"] = {
            "platform": "telegram",
            "account_id": "default",
            "label": "Telegram",
            "running": bool(getattr(tg, "running", False)) if tg else False,
        }
        return {
            "ok": True,
            "ts": time.time(),
            "chats": chats,
            "platform_status": platform_status,
        }

    @app.post("/api/unified-inbox/send")
    async def api_unified_inbox_send(request: Request, _=Depends(page_auth)):
        """向指定平台/账号发送消息。
        Body: { platform, account_id, chat_key, text }
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        if not chat_key or not text:
            raise HTTPException(400, "chat_key 和 text 不能为空")

        if platform == "line":
            svcs = _get_line_services(request)
            target = next((s for s in svcs if getattr(s, "account_id", "default") == account_id), None)
            if target is None and svcs:
                target = svcs[0]
            if target is None:
                raise HTTPException(503, "LINE 服务未启用")
            try:
                result = await target.send_to_chat(chat_key=chat_key, text=text)
                return {"ok": True, "result": result}
            except AttributeError:
                raise HTTPException(501, "LINE 暂不支持主动发送（需启用 approve 模式）")

        elif platform == "whatsapp":
            svcs = _get_whatsapp_services(request)
            target = next((s for s in svcs if getattr(s, "account_id", "default") == account_id), None)
            if target is None and svcs:
                target = svcs[0]
            if target is None:
                raise HTTPException(503, "WhatsApp 服务未启用")
            try:
                result = await target.send_to_chat(chat_key=chat_key, text=text)
                return {"ok": True, "result": result}
            except AttributeError:
                raise HTTPException(501, "WhatsApp 暂不支持主动发送（需启用 approve 模式）")

        elif platform == "messenger":
            msvc = _get_messenger_service(request)
            if msvc is None:
                raise HTTPException(503, "Messenger 服务未启用")
            try:
                result = await msvc.send_to_chat_name(chat_name=chat_key, text=text)
                return {"ok": True, "result": result}
            except Exception as ex:
                raise HTTPException(500, str(ex))

        else:
            raise HTTPException(400, f"不支持的平台: {platform}")
