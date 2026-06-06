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


class ChannelSendError(Exception):
    """渠道发送失败（携带 HTTP 语义状态码，由路由层映射成 HTTPException）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


@runtime_checkable
class ChannelAdapter(Protocol):
    """统一渠道收件箱适配器契约（读 + 写对称）。

    ``platform``：平台标识（line/whatsapp/messenger/telegram/web/…）。
    ``collect_chats(request, limit)``：返回该平台最近会话的**统一格式** chat dict 列表
    （由 ``normalizer.normalize_chat`` 产出）；内部需自行容错，单账号失败不应影响其它。
    ``status(request)``：返回 ``{platform_status_key: 状态 dict}``（可空），供收件箱顶栏。
    ``send(request, account_id, chat_key, text)``：向该平台投递文本，返回 result dict
    （可含 ``conversation_id`` 供归属打点）；失败抛 ``ChannelSendError``。

    A2 写路径收尾：``status`` / ``send`` 与 ``collect_chats`` 对称收敛到适配器，
    新增渠道 = 新增一个适配器并注册，**核心聚合/发送/状态三处都不再改**。
    """

    platform: str

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        ...

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        ...

    async def send(
        self, request: Any, account_id: str, chat_key: str, text: str
    ) -> Dict[str, Any]:
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

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for svc in _line_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            try:
                st = svc.status()
                out[f"line_{aid}"] = {
                    "platform": "line", "account_id": aid,
                    "label": _account_label(svc, aid),
                    "running": st.get("running", False), "serial": st.get("serial") or "",
                }
            except Exception:
                pass
        return out

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        svcs = _line_services(request)
        target = next((s for s in svcs
                       if getattr(s, "account_id", "default") == account_id), None)
        if target is None and svcs:
            target = svcs[0]
        if target is None:
            raise ChannelSendError(503, "LINE 服务未启用")
        try:
            return await target.send_to_chat(chat_key=chat_key, text=text)
        except AttributeError:
            raise ChannelSendError(501, "LINE 暂不支持主动发送（需启用 approve 模式）")


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

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for svc in _whatsapp_services(request):
            if svc is None:
                continue
            aid = getattr(svc, "account_id", "default")
            try:
                st = svc.status()
                out[f"wa_{aid}"] = {
                    "platform": "whatsapp", "account_id": aid,
                    "label": _account_label(svc, aid),
                    "running": st.get("running", False), "serial": st.get("serial") or "",
                }
            except Exception:
                pass
        return out

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        svcs = _whatsapp_services(request)
        target = next((s for s in svcs
                       if getattr(s, "account_id", "default") == account_id), None)
        if target is None and svcs:
            target = svcs[0]
        if target is None:
            raise ChannelSendError(503, "WhatsApp 服务未启用")
        try:
            return await target.send_to_chat(chat_key=chat_key, text=text)
        except AttributeError:
            raise ChannelSendError(501, "WhatsApp 暂不支持主动发送（需启用 approve 模式）")


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

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        msvc = getattr(request.app.state, "messenger_rpa_service", None)
        if msvc is None:
            return {}
        try:
            return {"messenger": {
                "platform": "messenger", "account_id": "default", "label": "Messenger",
                "running": msvc.is_running if hasattr(msvc, "is_running") else False,
            }}
        except Exception:
            return {}

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        msvc = getattr(request.app.state, "messenger_rpa_service", None)
        if msvc is None:
            raise ChannelSendError(503, "Messenger 服务未启用")
        try:
            return await msvc.send_to_chat_name(chat_name=chat_key, text=text)
        except ChannelSendError:
            raise
        except Exception as ex:
            raise ChannelSendError(500, str(ex))


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

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        tg = getattr(request.app.state, "telegram_client", None)
        return {"telegram": {
            "platform": "telegram", "account_id": "default", "label": "Telegram",
            "running": bool(getattr(tg, "running", False)) if tg else False,
        }}

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        client = getattr(request.app.state, "telegram_client", None)
        if client is None:
            raise ChannelSendError(503, "Telegram 服务未启用")
        sender = getattr(client, "send_message", None) or getattr(client, "send_text", None)
        if not callable(sender):
            raise ChannelSendError(501, "Telegram 暂不支持从统一收件箱发送")
        try:
            result = sender(chat_key, text)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except ChannelSendError:
            raise
        except Exception as ex:
            raise ChannelSendError(500, str(ex))


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

    def _web_cfg(self, request: Any) -> Dict[str, Any]:
        cm = getattr(request.app.state, "config_manager", None)
        cfg = (getattr(cm, "config", None) or {}) if cm is not None else {}
        return cfg if isinstance(cfg, dict) else {}

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        web = (self._web_cfg(request).get("web_chat") or {})
        if not web.get("enabled"):
            return {}
        aid = str(web.get("account_id") or "web")
        return {f"web_{aid}": {
            "platform": "web", "account_id": aid,
            "label": str(web.get("title") or "网页客服"), "running": True,
        }}

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        """web 是服务端原生渠道：投递=落库 + SSE 推浏览器 + 漏斗 + 默认停 AI 交人工。"""
        import time as _time

        from src.integrations.web_chat.hub import get_web_outbound_hub
        from src.integrations.web_chat.service import WebChatService

        wc = WebChatService.from_config(self._web_cfg(request))
        visitor_id = chat_key
        cid = wc.conversation_id(visitor_id)
        store = getattr(request.app.state, "inbox_store", None)
        try:
            wc.record_message(store, visitor_id, text=text, direction="out", display_name="")
        except Exception:
            logger.debug("[web_chat] 坐席出站落库失败", exc_info=True)
        try:
            get_web_outbound_hub().publish(cid, {
                "type": "web_outbound", "conversation_id": cid,
                "text": text, "by": "agent", "ts": _time.time(),
            })
        except Exception:
            logger.debug("[web_chat] 坐席出站推送失败", exc_info=True)
        contacts = getattr(request.app.state, "contacts", None)
        hooks = getattr(contacts, "hooks", None) if contacts is not None else None
        if hooks is not None:
            try:
                hooks.on_message(
                    channel="web", account_id=wc.account_id, external_id=visitor_id,
                    direction="out", text_preview=text[:120], trace_id="web-agent",
                )
            except Exception:
                logger.debug("[web_chat] funnel(agent out) 失败", exc_info=True)
        if store is not None:
            try:
                store.set_automation_mode(cid, "manual")  # 人工接管后停 AI
            except Exception:
                logger.debug("[web_chat] set manual 失败", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("inbox_message", {
                "conversation_id": cid, "platform": "web", "account_id": wc.account_id,
                "chat_key": visitor_id, "preview": text[:80],
                "direction": "out", "ts": _time.time(),
            })
        except Exception:
            pass
        return {"delivered": True, "conversation_id": cid}


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


def status_via_adapters(
    request: Any, adapters: List[ChannelAdapter],
) -> Dict[str, Dict[str, Any]]:
    """遍历适配器汇总平台运行状态；单适配器失败不影响其它。"""
    out: Dict[str, Dict[str, Any]] = {}
    for adapter in adapters:
        try:
            st = adapter.status(request)
            if st:
                out.update(st)
        except Exception:
            logger.debug("适配器 %s status 失败",
                         getattr(adapter, "platform", "?"), exc_info=True)
    return out


async def send_via_adapters(
    request: Any, platform: str, account_id: str, chat_key: str, text: str,
    adapters: List[ChannelAdapter],
) -> Dict[str, Any]:
    """按 platform 路由到对应适配器投递；未知平台抛 ChannelSendError(400)。"""
    platform = str(platform or "").lower()
    for adapter in adapters:
        if getattr(adapter, "platform", "") == platform:
            return await adapter.send(request, account_id, chat_key, text)
    raise ChannelSendError(400, f"不支持的平台: {platform}")
