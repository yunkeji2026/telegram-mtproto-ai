"""统一收件箱——服务/存储累加器（巨石拆分 slice 2）。

从 ``unified_inbox_routes.py`` 抽出的 ``request.app.state`` 访问器：把各平台 RPA
服务、翻译/会话助手服务、持久层 store、contacts 子系统句柄的获取逻辑集中到一处。

这些函数只读/惰性初始化 ``app.state``，不含业务编排，是巨石拆分的低风险一层。
routes.py 通过 import 等价重导出，对外引用路径（如
``unified_inbox_routes._get_translation_service``）保持不变。
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import Request

from src.ai.chat_assistant_service import ChatAssistantService
from src.ai.translation_service import TranslationService, normalize_lang
from src.inbox.normalizer import conv_id as _conv_id

logger = logging.getLogger(__name__)


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


def _skill_manager(request: Request):
    """SkillManager（经 telegram_client 暴露）或 None。"""
    tc = getattr(request.app.state, "telegram_client", None)
    return getattr(tc, "skill_manager", None) if tc else None


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


def _resolve_conv_language(request: Request, platform: str, account_id: str, chat_key: str) -> str:
    """读取会话持久化的客户语言（conversations.language）用于 outbound 自动翻译。

    供 send 的 ``target_lang: "auto"`` 推断目标语言。读不到 / 为 'unknown' 时返回 ""，
    调用方据此回落「不翻译，按原文发送」，保证 best-effort 永不阻断发送。
    """
    ibx = _inbox_store(request)
    if ibx is None:
        return ""
    try:
        conv = ibx.get_conversation(_conv_id(platform, account_id, chat_key))
        # 跨子系统语言码可能为 zh-cn / jp 等非规范写法（如 WhatsApp RPA 状态），
        # 统一经 normalize_lang 收敛到规范 ISO 码，保证 outbound auto 翻译稳定命中。
        lang = normalize_lang(str((conv or {}).get("language") or "").strip())
        return "" if lang in ("", "unknown") else lang
    except Exception:
        logger.debug("[send] 读取会话语言失败（忽略）", exc_info=True)
        return ""


def _resolve_conv_engine(request: Request, platform: str, account_id: str, chat_key: str) -> str:
    """读取会话持久化的首选翻译引擎（conversations.pref_engine）。F+。

    供 translate / send 在调用方未显式指定 ``engine`` 时回落到会话偏好；读不到 / 空 →
    返回 ""，调用方据此走现有 failover（零回归）。
    """
    ibx = _inbox_store(request)
    if ibx is None:
        return ""
    try:
        conv = ibx.get_conversation(_conv_id(platform, account_id, chat_key))
        return str((conv or {}).get("pref_engine") or "").strip().lower()
    except Exception:
        logger.debug("[translate] 读取会话首选引擎失败（忽略）", exc_info=True)
        return ""


def _ecommerce_tools(request: Request):
    """电商工具服务（Phase D）。未启用时返回 None（feature-flag ecommerce_tools.enabled）。"""
    return getattr(request.app.state, "ecommerce_tools", None)


def _contacts_store(request: Request):
    """Contacts 子系统 store（未启用时 None）。"""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "store", None) if contacts is not None else None


def _contacts_gateway(request: Request):
    """Contacts 子系统 gateway（未启用时 None）。"""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "gateway", None) if contacts is not None else None
