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


# P3：默认译文显示语言 KV 键前缀（运营级，区别于会话级 conversations.pref_engine）。
_DEFAULT_LANG_KEY = "inbox.default_lang"
# P4-C：默认「回复语言」KV 键前缀（出站轴：把坐席草稿译成客户语言；区别于上面的入站显示语言）。
#       桌面 copilot 草稿语言选择器在无会话级记忆时回落到此（账号 > 平台 > 全局）。
_REPLY_LANG_KEY = "inbox.default_reply_lang"


def _resolve_default_lang(
    request: Request, platform: str, account_id: str, *, base: str = _DEFAULT_LANG_KEY,
) -> dict:
    """P3：解析「默认译文显示语言」。优先级：账号 > 平台 > 全局。

    均未配置 → ``resolved=""``（前端据此回落浏览器本地/内置 zh）。返回还含 ``scopes``
    （各维度已配置原始值，供运营编辑回显）。best-effort：store 不可用/异常 → 全空。
    P4-C：``base`` 可切到 ``_REPLY_LANG_KEY`` 复用同一解析逻辑做「默认回复语言」。
    """
    out = {"resolved": "", "scopes": {"global": "", "platform": "", "account": ""}}
    ibx = _inbox_store(request)
    if ibx is None:
        return out
    p = str(platform or "").lower().strip()
    a = str(account_id or "default").strip()
    try:
        g = normalize_lang(ibx.get_app_setting(base))
        pl = normalize_lang(ibx.get_app_setting(f"{base}.platform.{p}")) if p else ""
        ac = normalize_lang(
            ibx.get_app_setting(f"{base}.account.{p}.{a}")
        ) if p else ""
        out["scopes"] = {"global": g, "platform": pl, "account": ac}
        out["resolved"] = ac or pl or g or ""
    except Exception:
        logger.debug("[default-lang] 解析失败（忽略）", exc_info=True)
    return out


def _resolve_default_reply_lang(request: Request, platform: str, account_id: str) -> dict:
    """P4-C：解析「默认回复语言」（出站轴）。复用 _resolve_default_lang 的优先级。"""
    return _resolve_default_lang(request, platform, account_id, base=_REPLY_LANG_KEY)


def _list_default_langs(request: Request, *, base: str = _DEFAULT_LANG_KEY) -> list:
    """P4-A：列出所有已配置的「默认译文语言」并解析回 scope 维度（运营管理面板用）。

    返回 ``[{scope, platform, account_id, lang, updated_by, updated_at}]``。store 不可用 → []。
    P4-C：``base`` 可切到 ``_REPLY_LANG_KEY`` 复用同一列出逻辑做「默认回复语言」。
    """
    items: list = []
    ibx = _inbox_store(request)
    if ibx is None:
        return items
    try:
        rows = ibx.list_app_settings(base)
    except Exception:
        logger.debug("[default-lang] 列出失败（忽略）", exc_info=True)
        return items
    pf_prefix, ac_prefix = base + ".platform.", base + ".account."
    for r in rows:
        k = str(r.get("key") or "")
        if k == base:
            scope, platform, account = "global", "", ""
        elif k.startswith(pf_prefix):
            scope, platform, account = "platform", k[len(pf_prefix):], ""
        elif k.startswith(ac_prefix):
            rest = k[len(ac_prefix):]
            parts = rest.split(".", 1)
            scope, platform = "account", parts[0]
            account = parts[1] if len(parts) > 1 else ""
        else:
            continue
        items.append({
            "scope": scope, "platform": platform, "account_id": account,
            "lang": r.get("value") or "", "updated_by": r.get("updated_by") or "",
            "updated_at": r.get("updated_at") or 0,
        })
    return items


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
