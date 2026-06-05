"""Channel Adapter（Phase A2）：把统一收件箱「按平台收集会话」的逻辑收敛为统一契约。

动机（蓝图 §2.2 / §3 A2）：此前 ``_collect_all_chats`` 内联 LINE/WhatsApp/Messenger/
Telegram 四套不同的 source API（``list_chats`` / ``list_pending`` / ``list_approvals``
/ ``_recent_messages``）与字段映射，新增渠道要改核心聚合函数。

本模块抽出 ``ChannelAdapter`` 协议 + 每平台一个适配器：
- 适配器**原样封装**既有逻辑（行为不变，含各自的容错），输出统一 chat dict（见 normalizer）。
- 核心聚合改为「遍历适配器注册表」，新增渠道 = 新增一个适配器并注册，**不改核心**。

为保持 ``src/inbox`` 不耦合 web 框架，``request`` 一律以 ``Any`` 处理（只读 app.state）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Protocol, runtime_checkable

from src.inbox.normalizer import normalize_chat, store_row_to_chat

logger = logging.getLogger(__name__)


@runtime_checkable
class ChannelAdapter(Protocol):
    """统一渠道收件箱适配器契约。

    ``platform``：平台标识（line/whatsapp/messenger/telegram/…）。
    ``collect_chats(request, limit)``：返回该平台最近会话的**统一格式** chat dict 列表
    （由 ``normalizer.normalize_chat`` 产出）；内部需自行容错，单账号失败不应影响其它。
    """

    platform: str

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        ...


def _account_label(svc: Any, aid: str) -> str:
    try:
        cfg = svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}
        return cfg.get("label") or aid
    except Exception:
        return aid


def _line_services(request: Any) -> list:
    svcs = getattr(request.app.state, "line_rpa_services", None)
    if svcs:
        return list(svcs)
    s = getattr(request.app.state, "line_rpa_service", None)
    return [s] if s else []


def _whatsapp_services(request: Any) -> list:
    svcs = getattr(request.app.state, "whatsapp_rpa_services", None)
    if svcs:
        return list(svcs)
    s = getattr(request.app.state, "whatsapp_rpa_service", None)
    return [s] if s else []


class LineInboxAdapter:
    platform = "line"

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for svc in _line_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            label = _account_label(svc, aid)
            try:
                chats = svc.list_chats(limit) or []
            except Exception as ex:
                logger.debug("LINE list_chats [%s] 失败: %s", aid, ex)
                chats = []
            for c in chats:
                out.append(normalize_chat(
                    platform="line", platform_name="LINE",
                    account_id=aid, account_label=label,
                    chat_key=c.get("chat_key") or c.get("name") or "",
                    name=c.get("name") or c.get("chat_key") or "",
                    last_msg=c.get("last_peer_text") or c.get("last_text") or "",
                    last_ts=c.get("last_ts") or c.get("ts") or 0,
                    unread=c.get("unread_count") or 0,
                    source=c,
                ))
        return out


class WhatsAppInboxAdapter:
    platform = "whatsapp"

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for svc in _whatsapp_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            label = _account_label(svc, aid)
            try:
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
                out.append(normalize_chat(
                    platform="whatsapp", platform_name="WhatsApp",
                    account_id=aid, account_label=label,
                    chat_key=c.get("chat_key", ""), name=c.get("name", ""),
                    last_msg=c.get("last_msg", ""), last_ts=c.get("last_ts", 0),
                    unread=c.get("unread", 0), source=c,
                ))
        return out


class MessengerInboxAdapter:
    platform = "messenger"

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        msvc = getattr(request.app.state, "messenger_rpa_service", None)
        if msvc is None:
            return out
        try:
            rows = (msvc.list_approvals(status="pending", limit=limit)
                    if hasattr(msvc, "list_approvals") else [])
        except Exception as ex:
            logger.debug("Messenger list_approvals 失败: %s", ex)
            rows = []
        for r in rows or []:
            aid = r.get("account_id") or "default"
            out.append(normalize_chat(
                platform="messenger", platform_name="Messenger",
                account_id=aid, account_label=aid or "Messenger",
                chat_key=r.get("chat_key") or r.get("name", ""),
                name=r.get("name") or r.get("chat_key", ""),
                last_msg=r.get("peer_text") or "",
                last_ts=r.get("ts") or 0, unread=1, source=r,
            ))
        return out


class TelegramInboxAdapter:
    platform = "telegram"

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        client = getattr(request.app.state, "telegram_client", None)
        if client is None:
            return out
        try:
            recent = getattr(client, "_recent_messages", None) or []
            for m in list(recent)[-limit:]:
                out.append(normalize_chat(
                    platform="telegram", platform_name="Telegram",
                    account_id="default", account_label="Telegram",
                    chat_key=str(m.get("chat_id") or ""),
                    name=m.get("user_name") or m.get("chat_name") or str(m.get("chat_id", "")),
                    last_msg=m.get("text") or "",
                    last_ts=m.get("ts") or 0, unread=1, source=m,
                ))
        except Exception:
            pass
        return out


class WebInboxAdapter:
    """web 渠道：会话即在统一收件箱（服务端原生），直接从 store 读出供工作台展示。"""

    platform = "web"

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            return []
        out: List[Dict[str, Any]] = []
        try:
            rows = store.list_conversations(limit=limit, platform="web") or []
        except Exception:
            logger.debug("WebInboxAdapter list_conversations 失败", exc_info=True)
            return []
        for r in rows:
            cid = str(r.get("conversation_id") or "")
            mode = "auto_ai"
            mcount = 0
            try:
                mode = store.get_automation_mode(cid)
                mcount = store.count_messages(cid)
            except Exception:
                pass
            out.append(store_row_to_chat(r, automation_mode=mode, message_count=mcount))
        return out


def default_inbox_adapters() -> List[ChannelAdapter]:
    """默认渠道适配器注册表。新增渠道在此追加即可，核心聚合无需改动。"""
    return [
        LineInboxAdapter(),
        WhatsAppInboxAdapter(),
        MessengerInboxAdapter(),
        TelegramInboxAdapter(),
        WebInboxAdapter(),
    ]


def collect_chats_via_adapters(
    request: Any, limit: int, adapters: List[ChannelAdapter],
) -> List[Dict[str, Any]]:
    """遍历适配器收集会话；单个适配器失败不影响其它（多一层隔离，只增不减结果）。"""
    out: List[Dict[str, Any]] = []
    for adapter in adapters:
        try:
            out.extend(adapter.collect_chats(request, limit))
        except Exception:
            logger.debug("适配器 %s 收集失败",
                         getattr(adapter, "platform", "?"), exc_info=True)
    return out
