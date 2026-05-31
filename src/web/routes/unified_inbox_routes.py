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

from src.ai.chat_assistant_service import ChatAssistantService
from src.ai.translation_service import TranslationService, detect_language
from src.inbox.ingest import ingest_collected_chats, ingest_thread

logger = logging.getLogger(__name__)
AUTOMATION_MODES = {"manual", "review", "multi_choice", "auto_ai"}


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


def _get_translation_service(request: Request) -> TranslationService:
    svc = getattr(request.app.state, "translation_service", None)
    if isinstance(svc, TranslationService):
        return svc
    ai_client = getattr(request.app.state, "ai_client", None)
    svc = TranslationService(ai_client=ai_client)
    request.app.state.translation_service = svc
    return svc


def _get_chat_assistant_service(request: Request) -> ChatAssistantService:
    svc = getattr(request.app.state, "chat_assistant_service", None)
    if isinstance(svc, ChatAssistantService):
        return svc
    ai_client = getattr(request.app.state, "ai_client", None)
    svc = ChatAssistantService(ai_client=ai_client)
    request.app.state.chat_assistant_service = svc
    return svc


def _automation_store(request: Request) -> Dict[str, str]:
    store = getattr(request.app.state, "unified_inbox_automation", None)
    if not isinstance(store, dict):
        store = {}
        request.app.state.unified_inbox_automation = store
    return store


def _inbox_store(request: Request):
    """持久层（Phase A）。未挂载时返回 None，调用方自动回落进程内 dict / 实时聚合。"""
    return getattr(request.app.state, "inbox_store", None)


def _read_automation_mode(request: Request, conversation_id: str) -> str:
    """优先读持久层，回落进程内 dict（修掉「重启即丢」生产阻断点）。"""
    store = _inbox_store(request)
    if store is not None:
        try:
            return store.get_automation_mode(conversation_id)
        except Exception:
            logger.debug("inbox_store.get_automation_mode 失败，回落进程内 dict", exc_info=True)
    return _automation_store(request).get(conversation_id, "review")


def _write_automation_mode(request: Request, conversation_id: str, mode: str) -> None:
    store = _inbox_store(request)
    if store is not None:
        try:
            store.set_automation_mode(conversation_id, mode)
            return
        except Exception:
            logger.debug("inbox_store.set_automation_mode 失败，回落进程内 dict", exc_info=True)
    _automation_store(request)[conversation_id] = mode


def _ingest_best_effort(request: Request, chats: List[Dict[str, Any]]) -> None:
    """旁路写入持久层。失败只 log，绝不影响收件箱响应。"""
    store = _inbox_store(request)
    if store is None or not chats:
        return
    try:
        ingest_collected_chats(store, chats)
    except Exception:
        logger.debug("统一收件箱旁路写入失败（已忽略）", exc_info=True)


def _ingest_thread_best_effort(request: Request, chat: Optional[Dict[str, Any]],
                               messages: List[Dict[str, Any]]) -> None:
    store = _inbox_store(request)
    if store is None or not chat or not messages:
        return
    try:
        ingest_thread(store, chat, messages)
    except Exception:
        logger.debug("统一收件箱会话历史写入失败（已忽略）", exc_info=True)


def _conv_id(platform: str, account_id: str, chat_key: str) -> str:
    return f"{platform}:{account_id}:{chat_key}"


def _message_obj(
    *,
    text: str,
    ts: Any = 0,
    direction: str = "in",
    message_id: str = "",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw = str(text or "")
    lang = detect_language(raw)
    return {
        "message_id": str(message_id or ""),
        "direction": direction if direction in {"in", "out"} else "in",
        "text": raw,
        "original_text": raw,
        "translated_text": raw,
        "language": lang,
        "translation": {
            "source_lang": lang,
            "target_lang": "zh",
            "ok": lang in {"zh", "unknown"} or not raw.strip(),
            "provider": "identity" if lang == "zh" else "none",
            "error": "" if lang in {"zh", "unknown"} else "not_requested",
        },
        "ts": ts or 0,
        "source": source or {},
    }


def _normalize_chat(
    *,
    platform: str,
    platform_name: str,
    account_id: str,
    account_label: str,
    chat_key: str,
    name: str,
    last_msg: str,
    last_ts: Any = 0,
    unread: Any = 0,
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    msg = _message_obj(text=last_msg, ts=last_ts, direction="in", source=source)
    return {
        "platform": platform,
        "platform_name": platform_name,
        "account_id": account_id,
        "account_label": account_label,
        "chat_key": chat_key,
        "conversation_id": _conv_id(platform, account_id, chat_key),
        "name": name,
        "last_msg": last_msg,
        "last_ts": last_ts or 0,
        "unread": unread or 0,
        "language": msg["language"],
        "last_message": msg,
        "messages": [msg] if last_msg else [],
        "can_send": True,
        "send_modes": ["manual", "review", "multi_choice", "auto_ai"],
        "automation_mode": "review",
        "risk": {"level": "unknown", "reasons": []},
        "relationship": {"stage": "", "intimacy_score": None},
        "source": source or {},
    }


def _candidate_messages_from_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("messages", "history", "recent_messages", "conversation"):
        rows = source.get(key)
        if isinstance(rows, list):
            out: List[Dict[str, Any]] = []
            for idx, row in enumerate(rows[-50:]):
                if isinstance(row, dict):
                    text = row.get("text") or row.get("raw") or row.get("peer_text") or row.get("message") or ""
                    direction = row.get("direction") or ("out" if row.get("is_self") else "in")
                    out.append(_message_obj(
                        text=str(text or ""),
                        ts=row.get("ts") or row.get("timestamp") or 0,
                        direction=str(direction),
                        message_id=str(row.get("id") or row.get("message_id") or idx),
                        source=row,
                    ))
            return [m for m in out if m.get("text")]
    return []


def _memory_bullets(request: Request, key: str, query: str = "") -> List[str]:
    store = getattr(request.app.state, "episodic_memory_store", None)
    if store is None or not hasattr(store, "get_bullets_for_prompt"):
        return []
    try:
        raw = store.get_bullets_for_prompt(key, max_items=6, query_text=query) or ""
    except Exception:
        return []
    out: List[str] = []
    for line in str(raw).splitlines():
        item = line.strip().lstrip("-• ").strip()
        if item:
            out.append(item)
    return out[:6]


def _context_relationship(request: Request, key: str, chat_key: str) -> Dict[str, Any]:
    store = getattr(request.app.state, "context_store", None)
    if store is None or not hasattr(store, "get"):
        return {}
    try:
        ctx = store.get(key)
    except Exception:
        return {}
    rel_root = ctx.get("companion_relationship") if isinstance(ctx, dict) else {}
    if not isinstance(rel_root, dict):
        return {}
    rel = rel_root.get(str(chat_key)) or rel_root.get("_default") or {}
    return rel if isinstance(rel, dict) else {}


def _build_profile(request: Request, chat: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    chat_key = str(chat.get("chat_key") or "")
    profile_key = f"{chat.get('platform')}:{chat.get('account_id')}:{chat_key}"
    latest_text = " ".join(str(m.get("text") or "") for m in messages[-5:])
    rel = dict(chat.get("relationship") or {})
    rel_from_ctx = _context_relationship(request, chat_key, chat_key)
    if rel_from_ctx:
        rel.update(rel_from_ctx)
    stage = rel.get("stage") or ("稳定陪伴" if len(messages) >= 20 else "升温" if len(messages) >= 8 else "初识")
    memories = _memory_bullets(request, profile_key, latest_text) or _memory_bullets(request, chat_key, latest_text)
    return {
        "profile_key": profile_key,
        "display_name": chat.get("name") or chat_key,
        "platform": chat.get("platform"),
        "platform_name": chat.get("platform_name"),
        "account_id": chat.get("account_id"),
        "account_label": chat.get("account_label"),
        "chat_key": chat_key,
        "language": chat.get("language") or detect_language(latest_text),
        "country_hint": "",
        "timezone_hint": "",
        "relationship": {
            "stage": stage,
            "exchange_count": rel.get("exchange_count", len(messages)),
            "intimacy_score": rel.get("intimacy_score"),
            "updated_at": rel.get("updated_at"),
        },
        "activity": {
            "message_count": len(messages),
            "last_ts": chat.get("last_ts") or 0,
            "unread": chat.get("unread") or 0,
        },
        "memories": memories,
        "tags": _profile_tags(chat, messages, memories),
        "notes": "",
    }


def _profile_tags(chat: Dict[str, Any], messages: List[Dict[str, Any]], memories: List[str]) -> List[str]:
    tags: List[str] = []
    lang = str(chat.get("language") or "")
    if lang and lang != "unknown":
        tags.append(f"语言:{lang}")
    if (chat.get("unread") or 0) > 0:
        tags.append("待回复")
    if len(messages) >= 8:
        tags.append("关系升温")
    if memories:
        tags.append("有记忆")
    return tags[:6]


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
            out.append(_normalize_chat(
                platform="line",
                platform_name="LINE",
                account_id=aid,
                account_label=label,
                chat_key=c.get("chat_key") or c.get("name") or "",
                name=c.get("name") or c.get("chat_key") or "",
                last_msg=c.get("last_peer_text") or c.get("last_text") or "",
                last_ts=c.get("last_ts") or c.get("ts") or 0,
                unread=c.get("unread_count") or 0,
                source=c,
            ))

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
            out.append(_normalize_chat(
                platform="whatsapp",
                platform_name="WhatsApp",
                account_id=aid,
                account_label=label,
                chat_key=c.get("chat_key", ""),
                name=c.get("name", ""),
                last_msg=c.get("last_msg", ""),
                last_ts=c.get("last_ts", 0),
                unread=c.get("unread", 0),
                source=c,
            ))

    # Messenger
    msvc = _get_messenger_service(request)
    if msvc is not None:
        try:
            rows = msvc.list_approvals(status="pending", limit=limit) if hasattr(msvc, "list_approvals") else []
        except Exception as ex:
            logger.debug("Messenger list_approvals 失败: %s", ex)
            rows = []
        for r in rows or []:
            aid = r.get("account_id") or "default"
            out.append(_normalize_chat(
                platform="messenger",
                platform_name="Messenger",
                account_id=aid,
                account_label=aid or "Messenger",
                chat_key=r.get("chat_key") or r.get("name", ""),
                name=r.get("name") or r.get("chat_key", ""),
                last_msg=r.get("peer_text") or "",
                last_ts=r.get("ts") or 0,
                unread=1,
                source=r,
            ))

    # Telegram 最近消息（如有 event_tracker）
    client = _get_telegram_client(request)
    if client is not None:
        try:
            recent = getattr(client, "_recent_messages", None) or []
            for m in list(recent)[-limit:]:
                out.append(_normalize_chat(
                    platform="telegram",
                    platform_name="Telegram",
                    account_id="default",
                    account_label="Telegram",
                    chat_key=str(m.get("chat_id") or ""),
                    name=m.get("user_name") or m.get("chat_name") or str(m.get("chat_id", "")),
                    last_msg=m.get("text") or "",
                    last_ts=m.get("ts") or 0,
                    unread=1,
                    source=m,
                ))
        except Exception:
            pass

    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    out = out[:limit * 4]
    # 旁路写入持久层（best-effort，不改读路径行为）
    _ingest_best_effort(request, out)
    for row in out:
        cid = str(row.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        row["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
    return out


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

    @app.get("/api/unified-inbox/thread")
    async def api_unified_inbox_thread(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 50,
    ):
        api_auth(request)
        platform = str(platform or "").lower()
        account_id = str(account_id or "default")
        chat_key = str(chat_key or "")
        if not platform or not chat_key:
            raise HTTPException(400, "platform 和 chat_key 不能为空")
        limit = max(1, min(100, int(limit or 50)))

        chats = _collect_all_chats(request, limit=100)
        target = next(
            (
                c for c in chats
                if c.get("platform") == platform
                and str(c.get("account_id") or "default") == account_id
                and str(c.get("chat_key") or "") == chat_key
            ),
            None,
        )

        messages: List[Dict[str, Any]] = []
        if platform == "telegram":
            client = _get_telegram_client(request)
            recent = getattr(client, "_recent_messages", None) if client is not None else []
            for idx, m in enumerate(list(recent or [])[-limit:]):
                if str(m.get("chat_id") or "") != chat_key:
                    continue
                messages.append(_message_obj(
                    text=m.get("text") or "",
                    ts=m.get("ts") or 0,
                    direction="out" if m.get("is_self") else "in",
                    message_id=str(m.get("id") or m.get("message_id") or idx),
                    source=m,
                ))

        if not messages and target:
            messages = _candidate_messages_from_source(target.get("source") or {})
        if not messages and target:
            messages = list(target.get("messages") or [])

        # 操作员打开会话时把较完整历史落库（best-effort）
        _ingest_thread_best_effort(request, target, messages)

        return {
            "ok": True,
            "chat": target,
            "messages": messages[-limit:],
            "count": len(messages[-limit:]),
        }

    @app.post("/api/unified-inbox/translate")
    async def api_unified_inbox_translate(request: Request, _=Depends(api_auth)):
        body = await request.json()
        text = str(body.get("text") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")
        svc = _get_translation_service(request)
        result = await svc.translate(
            text,
            target_lang=target_lang,
            source_lang=source_lang,
            style=style,
        )
        return {"ok": result.ok, "translation": result.to_dict()}

    @app.post("/api/unified-inbox/analyze")
    async def api_unified_inbox_analyze(request: Request, _=Depends(api_auth)):
        body = await request.json()
        text = str(body.get("text") or "")
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        chat = body.get("chat") if isinstance(body.get("chat"), dict) else {}
        if not text and messages:
            last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("text")), {})
            text = str(last.get("text") or "")
        svc = _get_chat_assistant_service(request)
        analysis = await svc.analyze(text=text, messages=messages, chat=chat)
        return {"ok": True, "analysis": analysis.to_dict()}

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

    @app.get("/api/unified-inbox/profile")
    async def api_unified_inbox_profile(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 50,
    ):
        api_auth(request)
        platform = str(platform or "").lower()
        account_id = str(account_id or "default")
        chat_key = str(chat_key or "")
        if not platform or not chat_key:
            raise HTTPException(400, "platform 和 chat_key 不能为空")
        chats = _collect_all_chats(request, limit=100)
        chat = next(
            (
                c for c in chats
                if c.get("platform") == platform
                and str(c.get("account_id") or "default") == account_id
                and str(c.get("chat_key") or "") == chat_key
            ),
            None,
        )
        if not chat:
            raise HTTPException(404, "chat not found")
        messages = _candidate_messages_from_source(chat.get("source") or {}) or list(chat.get("messages") or [])
        if platform == "telegram":
            client = _get_telegram_client(request)
            recent = getattr(client, "_recent_messages", None) if client is not None else []
            messages = [
                _message_obj(
                    text=m.get("text") or "",
                    ts=m.get("ts") or 0,
                    direction="out" if m.get("is_self") else "in",
                    message_id=str(m.get("id") or m.get("message_id") or idx),
                    source=m,
                )
                for idx, m in enumerate(list(recent or [])[-limit:])
                if str(m.get("chat_id") or "") == chat_key
            ] or messages
        return {"ok": True, "profile": _build_profile(request, chat, messages)}

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

        elif platform == "telegram":
            client = _get_telegram_client(request)
            if client is None:
                raise HTTPException(503, "Telegram 服务未启用")
            sender = getattr(client, "send_message", None) or getattr(client, "send_text", None)
            if not callable(sender):
                raise HTTPException(501, "Telegram 暂不支持从统一收件箱发送")
            try:
                result = sender(chat_key, text)
                if hasattr(result, "__await__"):
                    result = await result
                return {"ok": True, "result": result}
            except Exception as ex:
                raise HTTPException(500, str(ex))

        else:
            raise HTTPException(400, f"不支持的平台: {platform}")
