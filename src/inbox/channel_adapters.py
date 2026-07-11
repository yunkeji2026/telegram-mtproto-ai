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

from src.inbox.normalizer import conv_id, name_is_real, normalize_chat, store_row_to_chat

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


def _writeback_outbound(platform: str, account_id: str, chat_key: str, text: str,
                        res: Dict[str, Any]) -> None:
    """把刚发出的消息乐观回写收件箱线程（direction=out），与 ``orch.send`` 同口径。

    为何需要：编排器接管路径发完会 ``emit_incoming`` 回写；**回落到平台适配器**时
    RPA runner 只写 ContactHooks/状态库、网页号什么都不回写——坐席/客户要等下次轮询才
    看到这条。Messenger/LINE/WA 回落路径在此补上同一机制。

    去重安全：有 ``message_id`` → 以其为 platform_msg_id；无 id → hash 键乐观落库，
    日后带 id 的平台回显由 store 跨路径去重护栏收敛。best-effort，绝不影响发送结果。
    """
    try:
        from src.integrations.protocol_bridge import emit_incoming, make_message
        mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        emit_incoming(make_message(
            platform=platform, account_id=account_id, chat_key=chat_key,
            text=text, direction="out", msg_id=mid,
        ))
    except Exception:
        logger.debug("[%s] 出站回写收件箱失败", platform, exc_info=True)


def _resolve_peer_name(request: Any, platform: str, account_id: str,
                       chat_key: str) -> str:
    """把 chat_key 解析成 RPA 发送队列所需的「显示名」（UI 找人用）。

    先查 inbox_store 会话身份，再回落 chat_key 本身（若像真实昵称）。
    """
    store = getattr(request.app.state, "inbox_store", None)
    if store is not None:
        try:
            conv = store.get_conversation(
                conv_id(platform, account_id, chat_key)) or {}
            dn = str(conv.get("display_name") or "").strip()
            if dn and name_is_real(dn, chat_key):
                return dn
        except Exception:
            logger.debug("[%s] 会话名解析(store)失败", platform, exc_info=True)
    ck = str(chat_key or "").strip()
    if ck and name_is_real(ck, ck):
        return ck
    return ck


async def _send_via_rpa_queue(
    target: Any, platform: str, account_id: str, chat_key: str, text: str, *,
    request: Any,
) -> Dict[str, Any]:
    """RPA 回落发送：真实 service 走 ``enqueue_send``；测试 fake 回落 ``send_to_chat``。"""
    peer_name = _resolve_peer_name(request, platform, account_id, chat_key)
    enqueue = getattr(target, "enqueue_send", None)
    if callable(enqueue):
        try:
            item_id = enqueue(chat_key=chat_key, peer_name=peer_name, text=text)
        except TypeError:
            item_id = enqueue(chat_key, peer_name, text)
        except ValueError as ex:
            raise ChannelSendError(400, str(ex))
        except Exception as ex:
            raise ChannelSendError(502, f"{platform} 入队发送失败: {ex}")
        res = {"delivered": True, "queued": True, "item_id": int(item_id)}
        _writeback_outbound(platform, account_id, chat_key, text, res)
        return res
    sender = getattr(target, "send_to_chat", None)
    if callable(sender):
        try:
            res = await sender(chat_key=chat_key, text=text)
        except TypeError:
            res = await sender(chat_key, text)
        _writeback_outbound(
            platform, account_id, chat_key, text,
            res if isinstance(res, dict) else {})
        return res if isinstance(res, dict) else {"delivered": True}
    label = {"line": "LINE", "whatsapp": "WhatsApp"}.get(platform, platform)
    raise ChannelSendError(501, f"{label} 暂不支持主动发送（需启用 approve 模式）")


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
                chat = normalize_chat(
                    platform="line", platform_name="LINE",
                    account_id=aid, account_label=label,
                    chat_key=c.get("chat_key") or c.get("name") or "",
                    name=c.get("name") or c.get("chat_key") or "",
                    last_msg=c.get("last_peer_text") or c.get("last_text") or "",
                    last_ts=c.get("last_ts") or c.get("ts") or 0,
                    unread=c.get("unread_count") or 0,
                    source=c,
                )
                # 群消息「@我」：live 视图透传（供「群组动态」高亮/置顶；store-backed 读路径不带，优雅降级）
                if c.get("last_mentioned"):
                    chat["mentioned"] = True
                out.append(chat)
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
        return await _send_via_rpa_queue(
            target, "line", account_id, chat_key, text, request=request)


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
        return await _send_via_rpa_queue(
            target, "whatsapp", account_id, chat_key, text, request=request)


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
        """向 Messenger 会话投递（编排器未接管时的回落路径）。

        Messenger 有两种后端，收发口径不同，必须分流（否则会把网页会话丢给不存在的
        手机、或用数字 thread id 当「会话名」在官方 App 里搜不到人）：

        - **网页号（mode=web）**：连接由 :8791 Node 微服务保活，按 thread id 直发
          （``chat_key`` 即 jid）——与 ``MessengerWebWorker.send`` 同口径。
        - **RPA/设备号（mode=device）**：ADB 驱动官方 App，需按会话「显示名」找人后发，
          故先把 ``chat_key`` 解析成真实昵称再调 ``send_to_chat_name_for_account``。

        历史缺陷：此处曾直接 ``msvc.send_to_chat_name(chat_name=chat_key, text=text)``——
        但 service 无该方法（在 runner 上）、参数名也是 ``reply_text``，任何 Messenger
        出站都必抛 ``AttributeError``（AutosendWorker 全自动回复因此发不出）。
        """
        account_id = str(account_id or "")
        chat_key = str(chat_key or "")
        cfg = self._config(request)
        # 统一护栏（补洞）：编排器发送路径已过 send_blocked，但当编排器不拥有该账号、
        # 回落到本适配器直发 :8791 网页服务/RPA 时，此前**绕过** Kill-Switch + 反封号闸门
        # → 三级急停对这条兜底路径失效。此处补上：platform:messenger / account 级急停命中
        # 即拒发，与编排器路径同口径（返回 blocked，AutosendWorker 记 autosend_failed）。
        try:
            from src.integrations.shared.send_guard import send_blocked
            from src.integrations.account_registry import get_account_registry
            _blk, _reason = send_blocked(
                "messenger", account_id, config=cfg, registry=get_account_registry())
            if _blk:
                logger.warning(
                    "[messenger] 发送被护栏拦截 account=%s (%s)", account_id, _reason)
                raise ChannelSendError(403, f"blocked:{_reason}")
        except ChannelSendError:
            raise
        except Exception:
            logger.debug("[messenger] send_blocked 护栏检查异常（放行）", exc_info=True)
        # 分流决策：注册表 mode=web 权威判为网页号；注册表沉默但「只配了网页、无 RPA
        # service」时也按网页号发（web-only 部署兜底）。
        prefer_web = self._is_web_mode(account_id) or (
            getattr(request.app.state, "messenger_rpa_service", None) is None
        )
        if prefer_web:
            from src.integrations.messenger_web_login import web_enabled
            if web_enabled(cfg):
                res = await self._send_web(cfg, account_id, chat_key, text)
                _writeback_outbound("messenger", account_id, chat_key, text, res)
                return res
        res = await self._send_rpa(request, account_id, chat_key, text)
        _writeback_outbound("messenger", account_id, chat_key, text, res)
        return res

    @staticmethod
    def _config(request: Any) -> Dict[str, Any]:
        try:
            cm = getattr(request.app.state, "config_manager", None)
            return getattr(cm, "config", None) or {}
        except Exception:
            return {}

    @staticmethod
    def _is_web_mode(account_id: str) -> bool:
        """注册表里该 Messenger 账号是否登记为网页号（mode=web）。查不到/异常 → False。"""
        try:
            from src.integrations.account_registry import get_account_registry
            row = get_account_registry().get("messenger", account_id) or {}
            return str(row.get("mode") or "").lower() == "web"
        except Exception:
            logger.debug("[messenger] 账号 mode 查询失败", exc_info=True)
            return False

    async def _send_web(self, cfg: Dict[str, Any], account_id: str,
                        chat_key: str, text: str) -> Dict[str, Any]:
        from src.integrations.messenger_web_login import _post_json, service_base_url
        base = service_base_url(cfg)
        try:
            res = await _post_json(
                f"{base}/accounts/{account_id}/send",
                {"jid": chat_key, "text": text},
            )
        except ChannelSendError:
            raise
        except Exception as ex:
            raise ChannelSendError(503, f"Messenger 网页服务不可达: {ex}")
        # 未送达判定收严：ok/delivered/sent 任一显式 False 都算失败（防「composer 未清空」
        # 的静默丢消息被当成功；Node 现也会对该情形回 HTTP 502，双保险）。
        if isinstance(res, dict) and (
            res.get("ok") is False
            or res.get("delivered") is False
            or res.get("sent") is False
        ):
            raise ChannelSendError(
                502, str(res.get("error") or "Messenger 网页发送失败（未确认送达）"))
        return {
            "delivered": True,
            "message_id": str((res or {}).get("message_id") or ""),
            "conversation_id": conv_id("messenger", account_id, chat_key),
        }

    async def _send_rpa(self, request: Any, account_id: str,
                        chat_key: str, text: str) -> Dict[str, Any]:
        msvc = getattr(request.app.state, "messenger_rpa_service", None)
        if msvc is None:
            raise ChannelSendError(503, "Messenger 服务未启用")
        sender = getattr(msvc, "send_to_chat_name_for_account", None)
        if not callable(sender):
            raise ChannelSendError(501, "Messenger 暂不支持从统一收件箱主动发送")
        chat_name = self._resolve_chat_name(request, account_id, chat_key) or chat_key
        try:
            r = await sender(account_id, chat_name=chat_name, reply_text=text)
        except ChannelSendError:
            raise
        except Exception as ex:
            raise ChannelSendError(500, str(ex))
        if isinstance(r, dict) and r.get("ok") is False:
            raise ChannelSendError(
                502, str(r.get("error") or r.get("step") or "send failed"))
        return r if isinstance(r, dict) else {"delivered": True}

    @staticmethod
    def _resolve_chat_name(request: Any, account_id: str, chat_key: str) -> str:
        """把数字 thread id 解析成 RPA 找人所需的「显示名」：先查持久层会话身份，
        再回落待审批清单里带的 name。都无 → 空串（上游回落用 chat_key）。"""
        store = getattr(request.app.state, "inbox_store", None)
        if store is not None:
            try:
                conv = store.get_conversation(
                    conv_id("messenger", account_id, chat_key)) or {}
                dn = str(conv.get("display_name") or "").strip()
                if dn and name_is_real(dn, chat_key):
                    return dn
            except Exception:
                logger.debug("[messenger] 会话名解析(store)失败", exc_info=True)
        msvc = getattr(request.app.state, "messenger_rpa_service", None)
        if msvc is not None and hasattr(msvc, "list_approvals"):
            try:
                for r in (msvc.list_approvals(status="pending", limit=50) or []):
                    if (str(r.get("chat_key") or "") == chat_key
                            or str(r.get("customer_id") or "") == chat_key):
                        nm = str(r.get("name") or "").strip()
                        if nm:
                            return nm
            except Exception:
                logger.debug("[messenger] 会话名解析(approvals)失败", exc_info=True)
        return ""


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
                direction = str(m.get("direction") or "").lower()
                is_self = direction == "out" or bool(
                    m.get("is_self") or m.get("outgoing")
                )
                chat = normalize_chat(
                    platform="telegram", platform_name="Telegram",
                    account_id="default", account_label="Telegram",
                    chat_key=str(m.get("chat_id") or ""),
                    name=m.get("user_name") or m.get("chat_name") or str(m.get("chat_id", "")),
                    last_msg=m.get("text") or "",
                    last_ts=m.get("ts") or 0, unread=0 if is_self else 1, source=m,
                )
                chat["last_message"]["direction"] = "out" if is_self else "in"
                out.append(chat)
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


class ProtocolInboxAdapter:
    """protocol 多账号（Telegram pyrogram / WhatsApp Baileys）的收件箱视图。

    与 RPA 适配器不同：protocol 账号的消息由 worker **实时 push 落库**（见
    ``src/integrations/protocol_bridge.py``），故本适配器从 ``inbox_store`` 读出
    （形如 ``WebInboxAdapter``），只挑 ``account_registry`` 中 ``mode==protocol`` 的账号，
    避免与 RPA 适配器对同一平台重复出数。``send`` 不走这里——由 ``send_via_adapters``
    在进入平台适配器前，先按 ``orchestrator.owns`` 路由到对应 worker。
    """

    platform = "protocol"  # 哨兵：不参与按平台的 send 路由

    def _protocol_ids(self) -> "tuple[Dict[str, set], Dict[str, set]]":
        """返回 (active, removed) 两组 ``{platform: {account_id}}``。

        active：mode∈(protocol,desktop) 且 status≠removed —— 参与发送/收信展示。
        removed：mode∈(protocol,desktop) 且 status==removed —— 仅只读历史展示
        （账号已移除但 store 里的历史会话仍在，供查看；不参与发送）。
        """
        try:
            from src.integrations.account_registry import get_account_registry
            rows = get_account_registry().list() or []
        except Exception:
            return {}, {}
        active: Dict[str, set] = {}
        removed: Dict[str, set] = {}
        for a in rows:
            # protocol=真 worker push 落库；desktop=桌面壳同步桥落库（均按 store 读出）
            if a.get("mode") not in ("protocol", "desktop"):
                continue
            bucket = removed if a.get("status") == "removed" else active
            bucket.setdefault(str(a.get("platform") or ""), set()).add(
                str(a.get("account_id") or ""))
        return active, removed

    @staticmethod
    def _show_removed_history(request: Any) -> bool:
        """是否在收件箱里只读展示「已移除账号」的历史会话（config 门控，默认开）。

        ``inbox.show_removed_history``=false → 回到旧行为（彻底隐藏 removed 账号）。
        """
        try:
            cm = getattr(request.app.state, "config_manager", None)
            cfg = (getattr(cm, "config", None) or {}) if cm is not None else {}
            ibx = (cfg.get("inbox") or {}) if isinstance(cfg, dict) else {}
            return bool(ibx.get("show_removed_history", True))
        except Exception:
            return True

    def collect_chats(self, request: Any, limit: int) -> List[Dict[str, Any]]:
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            return []
        active, removed = self._protocol_ids()
        show_removed = self._show_removed_history(request)
        # 账号 → 是否只读（removed 账号只读）。active 优先（同号若同时命中以 active 为准）。
        readonly_ids: Dict[str, set] = removed if show_removed else {}
        plats = set(active) | (set(readonly_ids) if show_removed else set())
        if not plats:
            return []
        out: List[Dict[str, Any]] = []
        for plat in plats:
            active_ids = active.get(plat, set())
            ro_ids = readonly_ids.get(plat, set()) if show_removed else set()
            wanted = active_ids | ro_ids
            if not wanted:
                continue
            try:
                rows = store.list_conversations(limit=limit * 4, platform=plat) or []
            except Exception:
                logger.debug("ProtocolInboxAdapter list_conversations[%s] 失败",
                             plat, exc_info=True)
                continue
            for r in rows:
                aid = str(r.get("account_id") or "")
                if aid not in wanted:
                    continue
                is_ro = aid in ro_ids and aid not in active_ids
                mode = "review"
                mcount = 0
                cid = str(r.get("conversation_id") or "")
                try:
                    mode = store.get_automation_mode(cid)
                    mcount = store.count_messages(cid)
                except Exception:
                    pass
                out.append(store_row_to_chat(
                    r, automation_mode=mode, message_count=mcount,
                    read_only=is_ro,
                    account_status="removed" if is_ro else "",
                ))
        return out

    def status(self, request: Any) -> Dict[str, Dict[str, Any]]:
        # protocol 账号运行状态由编排器汇总（前端走 /api/accounts/orchestrator）
        return {}

    async def send(self, request: Any, account_id: str, chat_key: str, text: str
                   ) -> Dict[str, Any]:
        raise ChannelSendError(501, "protocol 账号发送由编排器路由")


def default_inbox_adapters() -> List[ChannelAdapter]:
    """默认渠道适配器注册表。新增渠道在此追加即可，核心聚合无需改动。"""
    return [
        LineInboxAdapter(),
        WhatsAppInboxAdapter(),
        MessengerInboxAdapter(),
        TelegramInboxAdapter(),
        WebInboxAdapter(),
        ProtocolInboxAdapter(),
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
    adapters: List[ChannelAdapter], *, reply_to: Any = None, mentions: Any = None,
) -> Dict[str, Any]:
    """按 platform 路由到对应适配器投递；未知平台抛 ChannelSendError(400)。

    M6①：protocol 多账号优先——若编排器拥有该 (platform, account_id) 的运行中 worker，
    直接经 worker 发送（多开账号各自独立连接），否则回落到平台适配器（RPA/单连接）。

    P4-5B：``reply_to``={id,from_me,participant,text} 携带原生引用回复上下文，仅经编排器
    worker 的协议发送路径生效（WhatsApp）；RPA/官方 API 适配器不支持则忽略（向后兼容）。
    """
    platform = str(platform or "").lower()
    try:
        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        if orch.owns(platform, account_id):
            _record_send_route(platform, "orchestrator")
            try:
                try:
                    return await orch.send(
                        platform, account_id, chat_key, text,
                        reply_to=reply_to, mentions=mentions)
                except TypeError:
                    # 旧签名（无 mentions kwarg）→ 回落，保持向后兼容
                    return await orch.send(
                        platform, account_id, chat_key, text, reply_to=reply_to)
            except ChannelSendError:
                raise
            except Exception as ex:  # noqa: BLE001
                raise ChannelSendError(502, f"protocol 发送失败: {ex}")
    except ChannelSendError:
        raise
    except Exception:
        logger.debug("编排器路由不可用，回落平台适配器", exc_info=True)
    for adapter in adapters:
        if getattr(adapter, "platform", "") == platform:
            # 回落到平台适配器（RPA/单连接/网页微服务）——观测这条兜底路径的触发频率，
            # 让「编排器漏接」在看板可见（回落率高=该查 worker ownership），而非崩了才知道。
            _record_send_route(platform, "adapter")
            return await adapter.send(request, account_id, chat_key, text)
    raise ChannelSendError(400, f"不支持的平台: {platform}")


def _record_send_route(platform: str, route: str) -> None:
    """把发送路由去向记进进程级观测（best-effort，绝不影响发送）。"""
    try:
        from src.inbox.send_route_stats import get_send_route_stats
        get_send_route_stats().record(platform, route)
    except Exception:
        logger.debug("send-route 观测记录失败", exc_info=True)
