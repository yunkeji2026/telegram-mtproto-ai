"""统一收件箱路由 — 聚合所有平台最近消息/对话 + 跨平台发送。

端点：
  GET  /unified-inbox                   — 页面
  GET  /api/unified-inbox/chats         — 各平台最近对话列表（聚合）
  POST /api/unified-inbox/send          — 发送消息到指定平台/账号
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from src.ai.chat_assistant_service import ChatAssistantService
from src.ai.translation_service import TranslationService, detect_language
from src.inbox.channel_adapters import (
    collect_chats_via_adapters,
    default_inbox_adapters,
)
from src.inbox.ingest import ingest_collected_chats, ingest_thread
from src.inbox.normalizer import (
    candidate_messages_from_source,
    conv_id,
    message_obj,
    normalize_chat,
    store_row_to_chat,
)

logger = logging.getLogger(__name__)
AUTOMATION_MODES = {"manual", "review", "multi_choice", "auto_ai"}
_SLA_WARN_SEC = 1800  # 客户消息未回复超过该秒数标记 SLA 警告（默认 30 分钟）
_SLA_CRIT_SEC = 7200  # 超过该秒数标记严重超时（默认 2 小时）


def _fmt_ts(ts: Any) -> str:
    """秒级时间戳 → 'YYYY-MM-DD HH:MM'（0/空 → 空串），CSV 导出用。"""
    try:
        n = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 1e12:  # 容错毫秒
        n = int(n / 1000)
    import datetime
    return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M")

# 漏斗阶段中文标签（与 contacts.JourneyStage / _rpa_shared_funnel.html 对齐）
FUNNEL_STAGE_LABELS: Dict[str, str] = {
    "INITIAL": "初始接触",
    "ENGAGED": "深入互动",
    "WARMING": "升温中",
    "HANDOFF_READY": "引流就绪",
    "HANDOFF_SENT": "话术已发",
    "LINE_ADDED": "加好友",
    "LINE_ACCEPTED": "通过验证",
    "LINE_ENGAGED": "二次互动",
    "BONDED": "成交",
    "CONVERTED": "已转化",
    "LOST_HANDOFF": "流失-引流",
    "LOST_LINE_SILENT": "流失-LINE",
    "NEEDS_MANUAL_MERGE": "待人工合并",
}

_PLATFORM_LABELS: Dict[str, str] = {
    "line": "LINE", "whatsapp": "WhatsApp", "messenger": "Messenger",
    "telegram": "Telegram", "web": "网页",
}

# A2：渠道适配器注册表（模块级，无状态可复用）。新增渠道在 channel_adapters 注册即可。
_INBOX_ADAPTERS = default_inbox_adapters()


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


def _ecommerce_tools(request: Request):
    """电商工具服务（Phase D）。未启用时返回 None（feature-flag ecommerce_tools.enabled）。"""
    return getattr(request.app.state, "ecommerce_tools", None)


# 订单号抽取：复用单一真源（src.ecommerce_tools.extract），避免正则跨文件漂移
from src.ecommerce_tools.extract import extract_order_no as _extract_order_no


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


# A3：归一逻辑已提到 src/inbox/normalizer.py（单一真源、可单测）。
# 此处保留同名薄委托别名，路由内现有调用点零改动。
_conv_id = conv_id
_message_obj = message_obj
_normalize_chat = normalize_chat
_candidate_messages_from_source = candidate_messages_from_source


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


def _session_agent(request: Request) -> Dict[str, str]:
    """从 session 解析当前坐席身份（无 SessionMiddleware 时回落 agent）。"""
    sess: Dict[str, Any] = {}
    try:
        if "session" in request.scope:
            sess = dict(request.session)
    except Exception:
        sess = {}
    uid = str(sess.get("user_id") or sess.get("username") or "agent")
    name = sess.get("display_name") or sess.get("username") or uid
    return {"agent_id": uid, "display_name": str(name or uid)}


def _publish_follow_up(action: str, *, contact_id: str = "", task_id: str = "",
                       assignee: str = "") -> None:
    """发布跟进任务变更事件（SSE 实时刷新待办徽标）。失败静默。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("follow_up", {
            "action": action, "contact_id": contact_id,
            "task_id": task_id, "assignee": assignee,
        })
    except Exception:
        logger.debug("follow_up 事件发布失败（已忽略）", exc_info=True)


def _contacts_store(request: Request):
    """Contacts 子系统 store（未启用时 None）。"""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "store", None) if contacts is not None else None


def _contacts_gateway(request: Request):
    """Contacts 子系统 gateway（未启用时 None）。"""
    contacts = getattr(request.app.state, "contacts", None)
    return getattr(contacts, "gateway", None) if contacts is not None else None


def _lookup_contacts_enrichment(
    request: Request,
    platform: str,
    account_id: str,
    chat_key: str,
) -> Optional[Dict[str, Any]]:
    """按渠道身份查 Contact/Journey，供工作台客户档案右栏展示。"""
    store = _contacts_store(request)
    if store is None or not platform or not chat_key:
        return None
    try:
        ci = store.get_ci_by_external(platform, account_id, chat_key)
        if ci is None:
            with store._lock:  # noqa: SLF001
                row = store._conn.execute(  # noqa: SLF001
                    "SELECT * FROM channel_identities "
                    "WHERE channel=? AND external_id=? ORDER BY linked_at ASC LIMIT 1",
                    (platform, chat_key),
                ).fetchone()
            if row is None:
                return None
            from src.contacts.store import _row_to_ci
            ci = _row_to_ci(row)
        contact = store.get_contact(ci.contact_id)
        journey = store.get_journey_by_contact(ci.contact_id)
        events: List[Dict[str, Any]] = []
        if journey is not None:
            events = store.list_events(journey.journey_id, limit=5)
        funnel = journey.funnel_stage if journey else ""
        intimacy = journey.intimacy_score if journey else None
        # Phase 5-4：留资属性 + 老客户识别（同一 Contact 有多渠道身份 / 经留资合并）
        attributes: Dict[str, str] = {}
        try:
            attributes = store.get_contact_attributes(ci.contact_id) or {}
        except Exception:
            attributes = {}
        identity_channels: List[str] = []
        try:
            ids_map = store.list_channel_identities_for_contacts([ci.contact_id])
            for c in ids_map.get(ci.contact_id, []) or []:
                ch = getattr(c, "channel", "")
                if ch and ch not in identity_channels:
                    identity_channels.append(ch)
        except Exception:
            identity_channels = [ci.channel]
        is_returning = (
            len(identity_channels) > 1
            or str(getattr(ci, "linked_via", "")).startswith("prechat_")
        )
        return {
            "contact_id": ci.contact_id,
            "primary_name": (contact.primary_name if contact else "") or "",
            "funnel_stage": funnel,
            "funnel_stage_label": FUNNEL_STAGE_LABELS.get(funnel, funnel),
            "intimacy_score": intimacy,
            "readiness_score": journey.readiness_score if journey else None,
            "engagement_score": journey.engagement_score if journey else None,
            "journey_id": journey.journey_id if journey else "",
            "recent_events": events[:5],
            "channel_identity": ci.to_dict(),
            "attributes": attributes,
            "identity_channels": identity_channels,
            "is_returning": is_returning,
        }
    except Exception:
        logger.debug("contacts enrichment 失败（已忽略）", exc_info=True)
        return None


_EVENT_LABELS: Dict[str, str] = {
    "contact_created": "建档",
    "msg_in": "收到消息",
    "msg_out": "发出消息",
    "stage_change": "阶段变更",
    "token_issued": "引流暗号已签发",
    "handoff_sent": "引流话术已发送",
    "line_first_reply": "LINE 首次回复",
    "lead_captured": "客户留资",
    "channel_identity_merged": "身份已合并",
    "channel_identity_split": "身份已拆出（新建）",
    "channel_identity_split_out": "身份已拆出（原侧）",
    "journey_states_discarded": "合并丢弃旧状态",
    "crm_updated": "坐席更新备注/标签",
    "follow_up_added": "新增跟进任务",
    "follow_up_reassigned": "跟进任务改派",
}


def _build_contact_timeline(
    request: Request,
    identities: List[Dict[str, Any]],
    msg_limit: int,
    before_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """聚合一个 Contact 名下所有渠道身份的消息为单条时间线（按 ts 升序）。

    每个渠道身份 → conv_id(channel, account_id, external_id) → InboxStore.list_recent_messages。
    取每渠道**最近** msg_limit 条（可用 before_ts 游标向更早翻页），跨渠道合并后取最近
    msg_limit 条，避免大客户拉全量。
    """
    store = _inbox_store(request)
    if store is None or not identities:
        return []
    merged: List[Dict[str, Any]] = []
    per_conv = max(10, min(200, msg_limit))
    for ci in identities:
        channel = str(ci.get("channel") or "")
        account_id = str(ci.get("account_id") or "default")
        external_id = str(ci.get("external_id") or "")
        if not channel or not external_id:
            continue
        cid = _conv_id(channel, account_id, external_id)
        try:
            rows = store.list_recent_messages(cid, limit=per_conv, before_ts=before_ts)
        except Exception:
            logger.debug("timeline list_recent_messages 失败 cid=%s", cid, exc_info=True)
            continue
        for m in rows:
            text = str(m.get("text") or m.get("original_text") or "")
            merged.append({
                "channel": channel,
                "platform_label": _PLATFORM_LABELS.get(channel, channel),
                "account_id": account_id,
                "conversation_id": cid,
                "direction": m.get("direction") or "in",
                "text": text,
                "translated_text": (
                    m.get("translated_text") if m.get("translated_text") not in (None, "", text)
                    else ""
                ),
                "ts": m.get("ts") or 0,
            })
    merged.sort(key=lambda x: x.get("ts") or 0)
    if len(merged) > msg_limit:
        merged = merged[-msg_limit:]
    return merged


def _collect_quick_templates(config_manager) -> List[Dict[str, str]]:
    """聚合快捷回复：workspace 专属 > messenger approval > templates.yaml。"""
    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(label: str, text: str, source: str = "") -> None:
        label = str(label or "").strip()
        text = str(text or "").strip()
        if not label or not text:
            return
        key = f"{label}\0{text}"
        if key in seen:
            return
        seen.add(key)
        out.append({"label": label, "text": text, "source": source})

    cfg: Dict[str, Any] = {}
    if config_manager is not None:
        cfg = getattr(config_manager, "config", None) or {}

    for t in (cfg.get("workspace") or {}).get("quick_templates") or []:
        if isinstance(t, dict):
            _add(t.get("label"), t.get("text"), "workspace")

    for t in (cfg.get("messenger_rpa") or {}).get("approval_templates") or []:
        if isinstance(t, dict):
            _add(t.get("label"), t.get("text"), "messenger")

    if config_manager is not None and hasattr(config_manager, "get_dynamic_templates_config"):
        try:
            dyn = config_manager.get_dynamic_templates_config() or {}
            for key, val in dyn.items():
                if isinstance(val, list):
                    for i, text in enumerate(val):
                        if isinstance(text, str) and text.strip():
                            lbl = key if i == 0 else f"{key} #{i + 1}"
                            _add(lbl, text, "templates")
                elif isinstance(val, dict):
                    for subk, subv in val.items():
                        if isinstance(subv, str) and subv.strip():
                            _add(f"{key}.{subk}", subv, "templates")
        except Exception:
            logger.debug("加载 templates.yaml 失败", exc_info=True)

    return out[:60]


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
    contacts = _lookup_contacts_enrichment(
        request,
        str(chat.get("platform") or ""),
        str(chat.get("account_id") or "default"),
        chat_key,
    )
    if contacts:
        if contacts.get("primary_name"):
            display_name = contacts["primary_name"]
        else:
            display_name = chat.get("name") or chat_key
        if contacts.get("intimacy_score") is not None:
            rel["intimacy_score"] = contacts["intimacy_score"]
        if contacts.get("funnel_stage_label"):
            stage = contacts["funnel_stage_label"]
    else:
        display_name = chat.get("name") or chat_key
    return {
        "profile_key": profile_key,
        "display_name": display_name,
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
        "tags": _profile_tags(chat, messages, memories, contacts),
        "contacts": contacts,
        "notes": "",
    }


def _profile_tags(
    chat: Dict[str, Any],
    messages: List[Dict[str, Any]],
    memories: List[str],
    contacts: Optional[Dict[str, Any]] = None,
) -> List[str]:
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
    if contacts:
        fs = contacts.get("funnel_stage") or ""
        if fs.startswith("LOST"):
            tags.append("流失风险")
        elif fs in {"HANDOFF_READY", "HANDOFF_SENT"}:
            tags.append("引流中")
        elif fs in {"LINE_ENGAGED", "BONDED", "CONVERTED"}:
            tags.append("高价值")
        intim = contacts.get("intimacy_score")
        if intim is not None and intim >= 70:
            tags.append("高亲密")
        if contacts.get("is_returning"):
            tags.append("老客户")
        if contacts.get("attributes"):
            tags.append("已留资")
    return tags[:8]


# ── 数据聚合 ─────────────────────────────────────────────────────────────

def _collect_all_chats(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    """从所有平台/账号收集最近对话，返回统一格式列表。

    A2：改为遍历 ChannelAdapter 注册表（src/inbox/channel_adapters.py）。
    新增渠道 = 新增一个适配器并注册，无需改本函数。各平台的取数/字段映射
    封装在各自适配器内，行为与抽取前一致。
    """
    out: List[Dict[str, Any]] = collect_chats_via_adapters(
        request, limit, _INBOX_ADAPTERS,
    )

    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    out = out[:limit * 4]
    # 旁路写入持久层（best-effort，不改读路径行为）
    _ingest_best_effort(request, out)
    for row in out:
        cid = str(row.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        row["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
    return out


def _read_from_store_enabled(request: Request) -> bool:
    """A1 读路径灰度开关：config.inbox.read_from_store（默认 false=实时聚合）。"""
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if not isinstance(cfg, dict):
        return False
    return bool((cfg.get("inbox") or {}).get("read_from_store", False))


def _sla_cfg(request: Request) -> Dict[str, int]:
    """SLA 阈值（秒）：config.inbox.sla_warn_sec / sla_crit_sec，带默认值。"""
    warn, crit = _SLA_WARN_SEC, _SLA_CRIT_SEC
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if isinstance(cfg, dict):
        ib = cfg.get("inbox") or {}
        try:
            warn = int(ib.get("sla_warn_sec", warn) or warn)
            crit = int(ib.get("sla_crit_sec", crit) or crit)
        except (TypeError, ValueError):
            pass
    if crit < warn:
        crit = warn
    return {"warn": warn, "crit": crit}


def _sla_alert_snapshot(request: Request) -> Dict[str, Any]:
    """当前 SLA 快照：等待/警告/严重计数 + 严重超时会话清单（告警徽标/SSE 用）。"""
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "waiting": 0, "breaching": 0, "critical": 0, "items": []}
    sla = _sla_cfg(request)
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    dirs = inbox.last_message_dirs(list(cmap))
    now = time.time()
    waiting = breaching = 0
    items: List[Dict[str, Any]] = []
    for cid, info in dirs.items():
        if info.get("direction") != "in":
            continue
        waiting += 1
        wait = now - (info.get("ts") or now)
        if wait >= sla["warn"]:
            breaching += 1
        if wait >= sla["crit"]:
            c = cmap.get(cid) or {}
            items.append({
                "conversation_id": cid,
                "platform": str(c.get("platform") or ""),
                "account_id": str(c.get("account_id") or "default"),
                "chat_key": str(c.get("chat_key") or ""),
                "name": str(c.get("display_name") or c.get("chat_key") or cid),
                "wait_sec": int(wait),
            })
    items.sort(key=lambda x: -x["wait_sec"])
    return {"ok": True, "waiting": waiting, "breaching": breaching,
            "critical": len(items), "items": items[:50],
            "warn_sec": sla["warn"], "crit_sec": sla["crit"]}


def _sla_detail(
    request: Request, scope: str = "critical", agent: Optional[str] = None,
) -> Dict[str, Any]:
    """SLA/首响明细下钻：按 scope 列出会话清单（带坐席归属，供仪表盘点开跳转）。

    scope: waiting(全部待回复) / breaching(≥warn) / critical(≥crit) / unresponded(今日进线未回复)。
    agent: 传入则按 claim 坐席过滤（""=未认领）。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "scope": scope, "items": [], "count": 0}
    sla = _sla_cfg(request)
    now = time.time()
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    claim_map: Dict[str, Dict[str, str]] = {}
    try:
        for cl in inbox.list_conversation_claims():
            claim_map[str(cl.get("conversation_id") or "")] = {
                "agent_id": str(cl.get("agent_id") or ""),
                "agent_name": str(cl.get("agent_name") or ""),
            }
    except Exception:
        logger.debug("sla-detail claim 读取失败（已忽略）", exc_info=True)

    def _mk(cid: str, wait: float, level: str) -> Dict[str, Any]:
        c = cmap.get(cid) or {}
        cl = claim_map.get(cid)
        return {
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or cid),
            "wait_sec": int(max(0, wait)),
            "level": level,
            "agent_id": cl["agent_id"] if cl else "",
            "agent_name": (cl["agent_name"] if cl else "") or "",
        }

    items: List[Dict[str, Any]] = []
    if scope == "unresponded":
        lt = time.localtime(now)
        midnight = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        for r in inbox.first_response_rows(midnight):
            if r["t_out"] is None:
                wait = now - r["t_in"]
                level = ("crit" if wait >= sla["crit"]
                         else "warn" if wait >= sla["warn"] else "")
                items.append(_mk(r["cid"], wait, level))
    else:
        thr = (sla["crit"] if scope == "critical"
               else sla["warn"] if scope == "breaching" else 0)
        dirs = inbox.last_message_dirs(list(cmap))
        for cid, info in dirs.items():
            if info.get("direction") != "in":
                continue
            wait = now - (info.get("ts") or now)
            if wait < thr:
                continue
            level = ("crit" if wait >= sla["crit"]
                     else "warn" if wait >= sla["warn"] else "")
            items.append(_mk(cid, wait, level))

    if agent is not None:
        items = [it for it in items if it["agent_id"] == agent]
    items.sort(key=lambda x: -x["wait_sec"])
    return {"ok": True, "scope": scope, "count": len(items),
            "items": items[:200]}


def _agent_frt_detail(
    request: Request, agent: str, days: int = 7,
) -> Dict[str, Any]:
    """某坐席在窗口内的首响会话明细（绩效榜下钻）。"""
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "agent": agent, "days": 7, "count": 0, "items": []}
    sla = _sla_cfg(request)
    span = 30 if int(days or 7) >= 30 else 7
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    since = midnight - (span - 1) * 86400
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    items: List[Dict[str, Any]] = []
    for r in inbox.agent_first_responses(since):
        if r["resp_ts"] is None or r["agent_id"] != agent:
            continue
        frt = max(0, int(r["resp_ts"] - r["t_in"]))
        c = cmap.get(r["cid"]) or {}
        items.append({
            "conversation_id": r["cid"],
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or r["cid"]),
            "frt_sec": frt,
            "attained": frt <= sla["warn"],
            "responded_at": r["resp_ts"],
        })
    items.sort(key=lambda x: -x["frt_sec"])
    return {"ok": True, "agent": agent, "days": span,
            "count": len(items), "items": items[:200]}


def _collect_chats_from_store(request: Request, limit: int = 30) -> List[Dict[str, Any]]:
    """A1 读路径：直接从 InboxStore（持久事实源）读会话列表，映射回 chat dict 形状。

    返回 None 表示 store 不可用（调用方回落实时聚合）。
    """
    store = _inbox_store(request)
    if store is None:
        return None  # type: ignore[return-value]
    convs = store.list_conversations(limit=min(200, max(1, limit * 4)))
    out: List[Dict[str, Any]] = []
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        try:
            mc = store.count_messages(cid)
        except Exception:
            mc = 0
        out.append(store_row_to_chat(c, automation_mode=mode, message_count=mc))
    return out


def _chats_for_listing(request: Request, limit: int = 30) -> List[Dict[str, Any]]:
    """收件箱列表数据源（A1 灰度）：

    - 始终先跑实时聚合 `_collect_all_chats`（同时旁路 ingest 进 store，保持 store 新鲜）；
    - flag 开 + store 可用：列表改用 store-backed 视图（跨平台/跨重启持久），
      实时聚合的副作用（ingest）已经发生；
    - 否则：返回实时聚合结果（原行为，零变化）。
    """
    live = _collect_all_chats(request, limit=limit)
    if _read_from_store_enabled(request):
        stored = _collect_chats_from_store(request, limit=limit)
        if stored is not None:
            return stored
    return live


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

    @app.get("/workspace", response_class=HTMLResponse)
    async def workspace_page(request: Request, _=Depends(page_auth)):
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "unified_inbox.html", ctx)

    @app.get("/unified-inbox")
    async def unified_inbox_redirect(request: Request, _=Depends(page_auth)):
        """旧入口：保留并 301→ 新独立工作台 /workspace。"""
        from starlette.responses import RedirectResponse
        return RedirectResponse("/workspace", status_code=307)

    @app.get("/api/workspace/stream")
    async def api_workspace_stream(request: Request):
        """SSE：实时推送收件箱新消息事件（替代前端轮询）。"""
        api_auth(request)
        import json as _json
        from starlette.responses import StreamingResponse
        from src.integrations.shared.event_bus import get_event_bus

        bus = get_event_bus()
        queue = bus.subscribe()

        _sla_seen: set = set()

        def _sla_pushes():
            """边沿触发：返回本轮"新转入严重超时"的会话 SSE 帧（去重 + 恢复后可再报）。"""
            frames: List[str] = []
            try:
                snap = _sla_alert_snapshot(request)
                items = snap.get("items", [])
                cur = {it["conversation_id"] for it in items}
                for it in items:
                    cid = it["conversation_id"]
                    if cid not in _sla_seen:
                        _sla_seen.add(cid)
                        frames.append(
                            "data: " + _json.dumps(
                                {"type": "sla_alert", "data": it},
                                ensure_ascii=False) + "\n\n")
                # 已恢复（不再严重）的从 seen 移除，便于下次再次告警
                _sla_seen.intersection_update(cur)
            except Exception:
                logger.debug("SLA SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        async def _gen():
            try:
                # 仅 replay 最近的 inbox_message 事件，避免设备类噪声
                _sse_types = {"inbox_message", "agent_presence",
                              "conversation_claim", "follow_up"}
                for evt in bus.recent_events(30):
                    if evt.get("type") in _sse_types:
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                # 连接建立即推一轮当前严重超时（无需等首个心跳）
                for fr in _sla_pushes():
                    yield fr
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        if evt.get("type") in _sse_types:
                            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        for fr in _sla_pushes():
                            yield fr
                    if await request.is_disconnected():
                        break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/unified-inbox/chats")
    async def api_unified_inbox_chats(request: Request, limit: int = 30):
        api_auth(request)
        limit = max(5, min(100, int(limit or 30)))
        chats = _chats_for_listing(request, limit=limit)
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
        # web 渠道：从配置读 account_id，running=enabled
        try:
            _wc_cfg = (config_manager.config or {}) if config_manager else {}
            _web = (_wc_cfg.get("web_chat") or {}) if isinstance(_wc_cfg, dict) else {}
            if _web.get("enabled"):
                _waid = str(_web.get("account_id") or "web")
                platform_status[f"web_{_waid}"] = {
                    "platform": "web",
                    "account_id": _waid,
                    "label": str(_web.get("title") or "网页客服"),
                    "running": True,
                }
        except Exception:
            pass
        # Phase 6-6：批量给会话挂 contact_id + 逾期跟进红点（contacts 启用时）
        try:
            _cstore = _contacts_store(request)
            if _cstore is not None and chats:
                pairs = [(str(c.get("platform") or ""), str(c.get("chat_key") or ""))
                         for c in chats]
                cmap = _cstore.resolve_contacts_by_external(pairs)
                overdue = _cstore.overdue_contact_ids()
                for c in chats:
                    cid = cmap.get((str(c.get("platform") or ""),
                                    str(c.get("chat_key") or "")))
                    if cid:
                        c["contact_id"] = cid
                        c["follow_up_overdue"] = cid in overdue
        except Exception:
            logger.debug("会话列表 contact 关联失败（已忽略）", exc_info=True)
        # Phase 6-7/6-8：SLA — 末条为入站则计算当前未回复时长，分级（warn/crit）标色
        try:
            _ibx = _inbox_store(request)
            if _ibx is not None and chats:
                _sla = _sla_cfg(request)
                _cids = [str(c.get("conversation_id") or "") for c in chats]
                _dirs = _ibx.last_message_dirs([x for x in _cids if x])
                _now = time.time()
                for c in chats:
                    info = _dirs.get(str(c.get("conversation_id") or ""))
                    if info and info.get("direction") == "in":
                        wait = max(0, int(_now - (info.get("ts") or _now)))
                        c["unanswered_sec"] = wait
                        c["sla_breach"] = wait >= _sla["warn"]
                        c["sla_level"] = ("crit" if wait >= _sla["crit"]
                                          else "warn" if wait >= _sla["warn"] else "")
                    else:
                        c["unanswered_sec"] = 0
                        c["sla_breach"] = False
                        c["sla_level"] = ""
        except Exception:
            logger.debug("会话列表 SLA 统计失败（已忽略）", exc_info=True)
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

        out_msgs = messages[-limit:]
        cid = _conv_id(platform, account_id, chat_key)
        translate_stats: Dict[str, Any] = {"enabled": False}
        try:
            from src.workspace.inbound_translate import enrich_inbound_translations
            out_msgs, translate_stats = await enrich_inbound_translations(
                request,
                out_msgs,
                conversation_id=cid,
                config_manager=config_manager,
                translation_svc=_get_translation_service(request),
            )
        except Exception:
            logger.debug("入站自动翻译失败（已忽略）", exc_info=True)

        return {
            "ok": True,
            "chat": target,
            "messages": out_msgs,
            "count": len(out_msgs),
            "auto_translate": translate_stats,
        }

    # ── Phase 5：坐席协作（presence + 会话租约）────────────────────
    from src.workspace.agent_coordinator import AgentCoordinator, web_funnel_snapshot

    @app.get("/api/workspace/presence")
    async def api_workspace_presence_list(request: Request):
        api_auth(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return {"ok": True, "agents": coord.list_presence()}

    @app.post("/api/workspace/presence")
    async def api_workspace_presence_set(request: Request, _=Depends(api_auth)):
        body = await request.json()
        status = str(body.get("status") or "online")
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        row = coord.set_presence(
            agent["agent_id"],
            display_name=str(body.get("display_name") or agent["display_name"]),
            status=status,
        )
        return {"ok": True, "presence": row}

    @app.post("/api/workspace/heartbeat")
    async def api_workspace_heartbeat(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = {}
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            pass
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        row = coord.heartbeat(
            agent["agent_id"],
            display_name=str(body.get("display_name") or agent["display_name"]),
            status=str(body.get("status") or ""),
        )
        return {"ok": True, "presence": row}

    @app.get("/api/workspace/claims")
    async def api_workspace_claims_list(request: Request):
        api_auth(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return {"ok": True, "claims": coord.list_claims()}

    @app.post("/api/workspace/claim")
    async def api_workspace_claim(request: Request, _=Depends(api_auth)):
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = _conv_id(platform, account_id, chat_key)
        force = bool(body.get("force"))
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        result = coord.claim(
            conversation_id,
            agent["agent_id"],
            agent_name=agent["display_name"],
            force=force,
        )
        if not result.get("ok"):
            return {"ok": False, **result}
        return {"ok": True, "conversation_id": conversation_id, "claim": result.get("claim")}

    @app.post("/api/workspace/claim/renew")
    async def api_workspace_claim_renew(request: Request, _=Depends(api_auth)):
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            platform = str(body.get("platform") or "").lower()
            chat_key = str(body.get("chat_key") or "")
            account_id = str(body.get("account_id") or "default")
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = _conv_id(platform, account_id, chat_key)
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return coord.renew_claim(conversation_id, agent["agent_id"])

    @app.post("/api/workspace/claim/release")
    async def api_workspace_claim_release(request: Request, _=Depends(api_auth)):
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            platform = str(body.get("platform") or "").lower()
            chat_key = str(body.get("chat_key") or "")
            account_id = str(body.get("account_id") or "default")
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = _conv_id(platform, account_id, chat_key)
        force = bool(body.get("force"))
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return coord.release_claim(conversation_id, agent["agent_id"], force=force)

    @app.get("/api/workspace/metrics/web-funnel")
    async def api_workspace_web_funnel(request: Request):
        api_auth(request)
        return {"ok": True, "metrics": web_funnel_snapshot(request, config_manager)}

    # ── Phase 5-5：坐席手动合并 / 拆分 / 审核队列 ────────────────
    @app.get("/api/workspace/contacts/overview")
    async def api_workspace_contact_overview(
        request: Request,
        platform: str = "",
        account_id: str = "default",
        chat_key: str = "",
    ):
        """当前会话对应 Contact 档案 + 该 Contact 的渠道身份 + 可合并候选。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        ci = store.get_ci_by_external(platform, account_id, chat_key)
        if ci is None:
            return {"ok": True, "contact": None, "candidates": []}
        overview = gw.contact_overview(ci.contact_id)
        candidates = gw.merge_candidates_for(ci.contact_id)
        return {
            "ok": True,
            "current_ci_id": ci.channel_identity_id,
            "contact": overview,
            "candidates": candidates,
        }

    @app.post("/api/workspace/contacts/merge")
    async def api_workspace_contact_merge(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not ci_id or not target:
            raise HTTPException(400, "ci_id 和 target_contact_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        try:
            ok = gw.manual_merge_identity(
                ci_id=ci_id, target_contact_id=target, operator=agent["agent_id"],
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}

    @app.post("/api/workspace/contacts/merge-contact")
    async def api_workspace_contact_merge_contact(request: Request, _=Depends(api_auth)):
        """contact 级合并：把 source 的所有渠道身份并入 target。"""
        body = await request.json()
        source = str(body.get("source_contact_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not source or not target:
            raise HTTPException(400, "source_contact_id 和 target_contact_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        ok = gw.merge_contacts(
            source_contact_id=source, target_contact_id=target, operator=agent["agent_id"],
        )
        return {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}

    @app.post("/api/workspace/contacts/split")
    async def api_workspace_contact_split(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        if not ci_id:
            raise HTTPException(400, "ci_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        new_cid = gw.split_identity(ci_id=ci_id, operator=agent["agent_id"])
        if not new_cid:
            return {"ok": False, "error": "nothing_to_split"}
        return {"ok": True, "new_contact_id": new_cid}

    @app.get("/api/workspace/merge-reviews")
    async def api_workspace_merge_reviews(request: Request):
        """待人工裁决的合并候选队列（含两侧档案摘要供对比）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled", "reviews": []}
        store = _contacts_store(request)
        out: List[Dict[str, Any]] = []
        for rv in gw.list_pending_merge_reviews():
            cand_ci = store.get_channel_identity(rv["candidate_ci_id"]) if store else None
            cand_overview = (
                gw.contact_overview(cand_ci.contact_id) if cand_ci else None
            )
            out.append({
                **rv,
                "candidate": cand_overview,
                "candidate_channel": cand_ci.channel if cand_ci else "",
                "target": gw.contact_overview(rv["target_contact_id"]),
            })
        return {"ok": True, "reviews": out, "count": len(out)}

    @app.post("/api/workspace/merge-reviews/{review_id}")
    async def api_workspace_merge_review_resolve(
        review_id: str, request: Request, _=Depends(api_auth),
    ):
        body = await request.json()
        action = str(body.get("action") or "").lower()
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action 必须是 approve / reject")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        if action == "approve":
            ok = gw.approve_merge_review(review_id, resolved_by=agent["agent_id"])
        else:
            ok = gw.reject_merge_review(review_id, resolved_by=agent["agent_id"])
        return {"ok": bool(ok), "action": action, "review_id": review_id}

    # ── Phase 6-1：Contact 360 全景视图 ─────────────────────────
    @app.get("/api/workspace/contacts/search")
    async def api_workspace_contacts_search(request: Request, q: str = "", limit: int = 20):
        """按 名称 / contact_id / 渠道 external_id 搜索 Contact（手动合并目标选择）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(1, min(50, int(limit or 20)))
        contacts, total = store.search_contacts(str(q or "").strip(), limit=limit)
        out = []
        for c in contacts:
            ov = gw.contact_overview(c.contact_id)
            if ov:
                out.append(ov)
        return {"ok": True, "contacts": out, "total": total}

    @app.get("/api/workspace/contact/{contact_id}")
    async def api_workspace_contact_detail(
        contact_id: str, request: Request, msg_limit: int = 60, before_ts: float = 0.0,
    ):
        """Contact 360：聚合档案 + 跨渠道消息时间线 + 事件历史 + 合并候选。

        before_ts>0：分页加载更早消息（仅返回 timeline，前端拼接）。
        """
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        overview = gw.contact_overview(contact_id)
        if overview is None:
            raise HTTPException(404, "contact 不存在")
        msg_limit = max(10, min(200, int(msg_limit or 60)))
        cursor = float(before_ts) if before_ts and before_ts > 0 else None
        timeline = _build_contact_timeline(
            request, overview.get("identities") or [], msg_limit, before_ts=cursor,
        )
        # 翻页请求：只回时间线 + 下一页游标
        next_cursor = timeline[0]["ts"] if (len(timeline) >= msg_limit and timeline) else 0
        if cursor is not None:
            return {"ok": True, "timeline": timeline, "next_cursor": next_cursor,
                    "has_more": bool(next_cursor)}
        journey = store.get_journey_by_contact(contact_id)
        events: List[Dict[str, Any]] = []
        if journey is not None:
            for e in store.list_events(journey.journey_id, limit=40):
                et = e.get("event_type") or e.get("type") or ""
                events.append({
                    "event_type": et,
                    "label": _EVENT_LABELS.get(et, et),
                    "ts": e.get("ts") or 0,
                    "payload": e.get("payload") or {},
                })
        candidates = gw.merge_candidates_for(contact_id)
        return {
            "ok": True,
            "contact": overview,
            "timeline": timeline,
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
            "events": events,
            "candidates": candidates,
        }

    @app.get("/workspace/contact/{contact_id}", response_class=HTMLResponse)
    async def workspace_contact_page(
        contact_id: str, request: Request, _=Depends(page_auth),
    ):
        ctx: Dict[str, Any] = {
            "contact_id": contact_id,
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "contact360.html", ctx)

    # ── Phase 6-2：客户列表 / CRM 入口 ──────────────────────────
    @app.get("/api/workspace/contacts/list")
    async def api_workspace_contacts_list(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 30,
        offset: int = 0,
    ):
        """CRM 客户列表：分页 + 阶段/留资/标签/跟进筛选 + 漏斗阶段汇总。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(5, min(100, int(limit or 30)))
        offset = max(0, int(offset or 0))
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=limit, offset=offset,
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        try:
            stage_counts = store.count_journeys_by_stage()
        except Exception:
            stage_counts = {}
        try:
            due_count = store.count_due_follow_ups()
        except Exception:
            due_count = 0
        return {
            "ok": True,
            "contacts": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "stage_counts": stage_counts,
            "stage_labels": FUNNEL_STAGE_LABELS,
            "due_follow_ups": due_count,
        }

    @app.post("/api/workspace/contact/{contact_id}/crm")
    async def api_workspace_contact_crm(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """保存客户 CRM 字段：备注 / 标签 / 跟进时间。未传的字段不改。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        note = body.get("note")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise HTTPException(400, "tags 必须是数组")
        fu = body.get("follow_up_at")
        follow_up_at = None
        if fu is not None:
            try:
                follow_up_at = int(fu)
            except (TypeError, ValueError):
                raise HTTPException(400, "follow_up_at 必须是时间戳整数")
        agent = _session_agent(request)
        return gw.update_contact_crm(
            contact_id, note=note, tags=tags, follow_up_at=follow_up_at,
            operator=agent["agent_id"],
        )

    @app.get("/api/workspace/follow-ups")
    async def api_workspace_follow_ups(request: Request, scope: str = "due", limit: int = 50):
        """待跟进客户列表（scope=due 已到期 / any 全部有跟进）+ 到期计数（全部/本人）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        scope = scope if scope in ("due", "any") else "due"
        rows, total = store.list_contacts_overview(
            follow_up=scope, limit=max(5, min(100, int(limit or 50))),
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        agent = _session_agent(request)
        return {"ok": True, "contacts": rows, "total": total,
                "due_follow_ups": store.count_due_follow_ups(),
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.post("/api/workspace/contact/{contact_id}/follow-up")
    async def api_workspace_follow_up_add(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """为客户新增跟进任务：{due_at, note, assignee?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "due_at 必须是时间戳整数")
        if due_at <= 0:
            raise HTTPException(400, "due_at 不能为空")
        agent = _session_agent(request)
        assignee = str(body.get("assignee") or "").strip() or agent["agent_id"]
        out = gw.add_follow_up_task(
            contact_id, due_at=due_at, note=str(body.get("note") or ""),
            assignee=assignee, operator=agent["agent_id"],
        )
        if out.get("ok"):
            _publish_follow_up("added", contact_id=contact_id,
                               task_id=out.get("task_id") or "", assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/done")
    async def api_workspace_follow_up_done(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """标记跟进任务完成。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        out = gw.complete_follow_up_task(task_id, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("done", task_id=task_id)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/assign")
    async def api_workspace_follow_up_assign(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """改派跟进任务给某坐席：{assignee}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        assignee = str(body.get("assignee") or "").strip()
        if not assignee:
            raise HTTPException(400, "assignee 不能为空")
        agent = _session_agent(request)
        out = gw.reassign_follow_up_task(
            task_id, assignee=assignee, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("assigned", contact_id=out.get("contact_id") or "",
                               task_id=task_id, assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/snooze")
    async def api_workspace_follow_up_snooze(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """延期跟进任务：{days} 顺延 或 {due_at} 直设。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            days = int(body.get("days") or 0)
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "days/due_at 必须是整数")
        if days <= 0 and due_at <= 0:
            raise HTTPException(400, "需提供 days 或 due_at")
        agent = _session_agent(request)
        out = gw.snooze_follow_up_task(
            task_id, days=days, due_at=due_at, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("snoozed", contact_id=out.get("contact_id") or "",
                               task_id=task_id)
        return out

    @app.get("/api/workspace/my-tasks")
    async def api_workspace_my_tasks(
        request: Request, scope: str = "mine", due: str = "today", limit: int = 100,
    ):
        """跟进待办列表：scope=mine(本人)/all(全部)，due=today/overdue/all。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "tasks": []}
        agent = _session_agent(request)
        assignee = agent["agent_id"] if scope != "all" else None
        now = int(time.time())
        if due == "overdue":
            due_before: Optional[int] = now
        elif due == "all":
            due_before = None
        else:  # today（含逾期 + 今天到期）
            lt = time.localtime(now)
            due_before = int(time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 59, 59, 0, 0, -1)))
        tasks = store.list_open_tasks(
            assignee=assignee, due_before=due_before,
            limit=max(1, min(500, int(limit or 100))))
        for t in tasks:
            t["channel_labels"] = [_PLATFORM_LABELS.get(c, c) for c in (t.get("channels") or [])]
            t["overdue"] = bool(t.get("due_at") and t["due_at"] <= now)
        return {"ok": True, "tasks": tasks,
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.get("/api/workspace/contact/{contact_id}/tasks")
    async def api_workspace_contact_tasks(
        contact_id: str, request: Request, include_done: int = 0,
    ):
        """某客户的跟进任务（会话内联面板用，轻量）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tasks": []}
        return {"ok": True,
                "tasks": store.list_follow_up_tasks(
                    contact_id, include_done=bool(include_done))}

    @app.get("/api/workspace/sla-alerts")
    async def api_workspace_sla_alerts(request: Request):
        """SLA 告警源（顶栏徽标轮询 + 严重超时清单下钻）。"""
        api_auth(request)
        return _sla_alert_snapshot(request)

    @app.get("/api/workspace/sla-detail")
    async def api_workspace_sla_detail(
        request: Request, scope: str = "critical", agent: Optional[str] = None,
    ):
        """SLA/首响明细下钻清单（仪表盘卡片/坐席行点开）。"""
        api_auth(request)
        scope = scope if scope in {"waiting", "breaching", "critical",
                                   "unresponded"} else "critical"
        return _sla_detail(request, scope=scope, agent=agent)

    @app.get("/api/workspace/agent-frt-detail")
    async def api_workspace_agent_frt_detail(
        request: Request, agent: str = "", days: int = 7,
    ):
        """某坐席窗口内首响会话明细（绩效榜下钻）。"""
        api_auth(request)
        return _agent_frt_detail(request, agent=str(agent or ""), days=days)

    def _daily_report_rows(request: Request, span: int) -> List[Dict[str, Any]]:
        """逐日经营指标表（坐席日报/导出共用）。

        每日一行：新客/留资/引流(转化) + 首响(条数/已响应/均值/达标率) +
        解决(引流)时长(解决数/均值)。窗口 = 今天回溯 span 天，按本地日期。
        """
        sla = _sla_cfg(request)
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        days_keys = [time.strftime("%Y-%m-%d", time.localtime(since + i * 86400))
                     for i in range(span)]
        rows: Dict[str, Dict[str, Any]] = {
            k: {"date": k, "new_contacts": 0, "leads": 0, "conversions": 0,
                "frt_count": 0, "frt_responded": 0, "frt_avg_sec": 0,
                "frt_attain_rate": 0.0, "resolved": 0, "resolution_avg_sec": 0}
            for k in days_keys}
        store = _contacts_store(request)
        if store is not None:
            try:
                by_new = store.count_contacts_by_day(since)
                by_lead = store.count_events_by_day("lead_captured", since)
                by_conv = store.count_events_by_day("handoff_sent", since)
                for k in days_keys:
                    rows[k]["new_contacts"] = by_new.get(k, 0)
                    rows[k]["leads"] = by_lead.get(k, 0)
                    rows[k]["conversions"] = by_conv.get(k, 0)
                res_acc: Dict[str, List[float]] = {}
                for rr in store.resolution_stats(since):
                    if rr["resolved_ts"] is None:
                        continue
                    rday = time.strftime("%Y-%m-%d",
                                         time.localtime(rr["resolved_ts"]))
                    acc = res_acc.setdefault(rday, [0.0, 0.0])
                    acc[0] += max(0, rr["resolved_ts"] - rr["t_in"])
                    acc[1] += 1
                for k, (s, n) in res_acc.items():
                    if k in rows and n:
                        rows[k]["resolved"] = int(n)
                        rows[k]["resolution_avg_sec"] = int(s / n)
            except Exception:
                logger.debug("daily-report contacts 统计失败（已忽略）", exc_info=True)
        inbox = _inbox_store(request)
        if inbox is not None:
            try:
                fr_acc: Dict[str, List[float]] = {}
                for r in inbox.first_response_rows(since):
                    day = time.strftime("%Y-%m-%d", time.localtime(r["t_in"]))
                    acc = fr_acc.setdefault(day, [0.0, 0.0, 0.0, 0.0])  # n,resp,sum,attain
                    acc[0] += 1
                    if r["t_out"] is not None:
                        frt = max(0.0, r["t_out"] - r["t_in"])
                        acc[1] += 1
                        acc[2] += frt
                        if frt <= sla["warn"]:
                            acc[3] += 1
                for k, (n, resp, s, att) in fr_acc.items():
                    if k not in rows:
                        continue
                    rows[k]["frt_count"] = int(n)
                    rows[k]["frt_responded"] = int(resp)
                    rows[k]["frt_avg_sec"] = int(s / resp) if resp else 0
                    rows[k]["frt_attain_rate"] = round(att / resp * 100, 1) if resp else 0.0
            except Exception:
                logger.debug("daily-report inbox 统计失败（已忽略）", exc_info=True)
        return [rows[k] for k in days_keys]

    @app.get("/api/workspace/daily-report.csv")
    async def api_workspace_daily_report(request: Request, days: int = 7):
        """逐日经营日报 CSV（历史回看）：days=7/30，每行一天，含汇总行。"""
        api_auth(request)
        span = 30 if int(days or 7) >= 30 else 7
        data = _daily_report_rows(request, span)
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # Excel UTF-8 BOM
        w = csv.writer(buf)
        w.writerow(["date", "new_contacts", "leads", "conversions",
                    "frt_count", "frt_responded", "frt_avg_sec",
                    "frt_attain_rate_pct", "resolved", "resolution_avg_sec"])
        tot = {"new_contacts": 0, "leads": 0, "conversions": 0, "frt_count": 0,
               "frt_responded": 0, "resolved": 0, "frt_sum": 0, "res_sum": 0,
               "attain": 0}
        for r in data:
            w.writerow([r["date"], r["new_contacts"], r["leads"], r["conversions"],
                        r["frt_count"], r["frt_responded"], r["frt_avg_sec"],
                        r["frt_attain_rate"], r["resolved"], r["resolution_avg_sec"]])
            tot["new_contacts"] += r["new_contacts"]
            tot["leads"] += r["leads"]
            tot["conversions"] += r["conversions"]
            tot["frt_count"] += r["frt_count"]
            tot["frt_responded"] += r["frt_responded"]
            tot["resolved"] += r["resolved"]
            tot["frt_sum"] += r["frt_avg_sec"] * r["frt_responded"]
            tot["res_sum"] += r["resolution_avg_sec"] * r["resolved"]
            tot["attain"] += round(r["frt_attain_rate"] / 100 * r["frt_responded"])
        frt_avg = int(tot["frt_sum"] / tot["frt_responded"]) if tot["frt_responded"] else 0
        res_avg = int(tot["res_sum"] / tot["resolved"]) if tot["resolved"] else 0
        attain = round(tot["attain"] / tot["frt_responded"] * 100, 1) if tot["frt_responded"] else 0.0
        w.writerow(["合计", tot["new_contacts"], tot["leads"], tot["conversions"],
                    tot["frt_count"], tot["frt_responded"], frt_avg, attain,
                    tot["resolved"], res_avg])
        fname = "daily-report-%dd-%s.csv" % (
            span, time.strftime("%Y%m%d", time.localtime()))
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=" + fname},
        )

    @app.get("/api/workspace/dashboard")
    async def api_workspace_dashboard(request: Request, days: int = 7):
        """工作台仪表盘：今日会话/留资/引流 + 到期跟进 + 坐席负载 + 趋势 + SLA + 首响。"""
        api_auth(request)
        store = _contacts_store(request)
        agent = _session_agent(request)
        sla = _sla_cfg(request)
        span = 30 if int(days or 7) >= 30 else 7
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        out: Dict[str, Any] = {"ok": True, "today": {}, "agent_load": [],
                               "funnel": {}, "trend": [], "sla": {}, "days": span,
                               "first_response": {}, "sla_by_agent": [],
                               "agent_frt": [], "resolution": {}, "res_trend": []}
        if store is not None:
            try:
                ev = store.count_events_since_multi(
                    ["lead_captured", "handoff_sent"], midnight)
                out["today"] = {
                    "new_contacts": store.count_contacts_created_since(midnight),
                    "leads": ev.get("lead_captured", 0),
                    "handoffs": ev.get("handoff_sent", 0),
                }
                out["due_tasks"] = store.count_due_tasks()
                out["due_tasks_mine"] = store.count_due_tasks(assignee=agent["agent_id"])
                out["agent_load"] = store.agent_task_load()
                out["stage_counts"] = store.count_journeys_by_stage()
                # N 日趋势（按本地日期）：新客户 / 留资 / 引流(转化)
                by_new = store.count_contacts_by_day(since)
                by_lead = store.count_events_by_day("lead_captured", since)
                by_conv = store.count_events_by_day("handoff_sent", since)
                trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    trend.append({"day": key[5:], "new_contacts": by_new.get(key, 0),
                                  "leads": by_lead.get(key, 0),
                                  "conversions": by_conv.get(key, 0)})
                out["trend"] = trend
                # 解决(引流)时长：首条 msg_in → handoff_sent（按解决日聚合）
                res_per_day: Dict[str, Dict[str, float]] = {}
                r_sum = r_cnt = 0
                for rr in store.resolution_stats(since):
                    if rr["resolved_ts"] is None:
                        continue
                    dur = max(0, rr["resolved_ts"] - rr["t_in"])
                    rday = time.strftime("%Y-%m-%d", time.localtime(rr["resolved_ts"]))
                    pd = res_per_day.setdefault(rday, {"sum": 0.0, "n": 0})
                    pd["sum"] += dur
                    pd["n"] += 1
                    if rr["resolved_ts"] >= midnight:
                        r_sum += dur
                        r_cnt += 1
                out["resolution"] = {
                    "today_resolved": r_cnt,
                    "today_avg_sec": int(r_sum / r_cnt) if r_cnt else 0}
                res_trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    pd = res_per_day.get(key)
                    res_trend.append({
                        "day": key[5:],
                        "avg_min": round(pd["sum"] / pd["n"] / 60, 1) if pd and pd["n"] else 0,
                        "count": pd["n"] if pd else 0})
                out["res_trend"] = res_trend
            except Exception:
                logger.debug("dashboard 统计失败（已忽略）", exc_info=True)
        # SLA + 首响：均基于 inbox 消息
        try:
            inbox = _inbox_store(request)
            if inbox is not None:
                # 当前等待回复（末条入站）+ 分级
                cids = [c["conversation_id"] for c in inbox.list_conversations(limit=500)]
                dirs = inbox.last_message_dirs(cids)
                # 活跃 claim → 会话归属坐席（lease 有效，可靠；过期已 purge）
                claim_map: Dict[str, Dict[str, str]] = {}
                try:
                    for cl in inbox.list_conversation_claims():
                        claim_map[str(cl.get("conversation_id") or "")] = {
                            "agent_id": str(cl.get("agent_id") or ""),
                            "agent_name": str(cl.get("agent_name") or ""),
                        }
                except Exception:
                    logger.debug("dashboard claim 读取失败（已忽略）", exc_info=True)
                waiting = breaching = critical = 0
                by_agent: Dict[str, Dict[str, Any]] = {}
                for cid, v in dirs.items():
                    if v.get("direction") != "in":
                        continue
                    waiting += 1
                    wait = now - (v.get("ts") or now)
                    is_warn = wait >= sla["warn"]
                    is_crit = wait >= sla["crit"]
                    if is_crit:
                        critical += 1
                    if is_warn:
                        breaching += 1
                    cl = claim_map.get(cid)
                    akey = cl["agent_id"] if cl and cl["agent_id"] else ""
                    bucket = by_agent.get(akey)
                    if bucket is None:
                        bucket = {"agent_id": akey,
                                  "agent_name": (cl["agent_name"] if cl else "")
                                  or akey or "(未认领)",
                                  "waiting": 0, "breaching": 0, "critical": 0}
                        by_agent[akey] = bucket
                    bucket["waiting"] += 1
                    if is_warn:
                        bucket["breaching"] += 1
                    if is_crit:
                        bucket["critical"] += 1
                out["sla"] = {"waiting": waiting, "breaching": breaching,
                              "critical": critical, "warn_sec": sla["warn"],
                              "crit_sec": sla["crit"]}
                out["sla_by_agent"] = sorted(
                    by_agent.values(),
                    key=lambda x: (-x["critical"], -x["breaching"], -x["waiting"]))
                # 首响：窗口内首次进线的会话，首条入站→首条其后出站
                rows = inbox.first_response_rows(since)
                per_day: Dict[str, Dict[str, float]] = {}
                t_sum = t_cnt = t_attain = t_resp = 0
                for r in rows:
                    day = time.strftime("%Y-%m-%d", time.localtime(r["t_in"]))
                    d = per_day.setdefault(day, {"n": 0, "resp": 0, "sum": 0.0,
                                                 "attain": 0})
                    d["n"] += 1
                    if r["t_out"] is not None:
                        frt = max(0.0, r["t_out"] - r["t_in"])
                        d["resp"] += 1
                        d["sum"] += frt
                        if frt <= sla["warn"]:
                            d["attain"] += 1
                    if r["t_in"] >= midnight:
                        t_cnt += 1
                        if r["t_out"] is not None:
                            frt = max(0.0, r["t_out"] - r["t_in"])
                            t_resp += 1
                            t_sum += frt
                            if frt <= sla["warn"]:
                                t_attain += 1
                out["first_response"] = {
                    "today_count": t_cnt,
                    "today_responded": t_resp,
                    "today_avg_sec": int(t_sum / t_resp) if t_resp else 0,
                    "today_attain_rate": round(t_attain / t_resp * 100, 1) if t_resp else 0.0,
                }
                # 首响达标率趋势（与 trend 对齐 day 维度）
                frt_trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    d = per_day.get(key)
                    rate = round(d["attain"] / d["resp"] * 100, 1) if d and d["resp"] else 0.0
                    frt_trend.append({"day": key[5:], "rate": rate,
                                      "count": d["n"] if d else 0})
                out["frt_trend"] = frt_trend
                # 坐席首响绩效（基于 agent_sends 归属，窗口内）
                ag: Dict[str, Dict[str, Any]] = {}
                for r in inbox.agent_first_responses(since):
                    if r["resp_ts"] is None or not r["agent_id"]:
                        continue
                    frt = max(0.0, r["resp_ts"] - r["t_in"])
                    a = ag.get(r["agent_id"])
                    if a is None:
                        a = {"agent_id": r["agent_id"],
                             "agent_name": r["agent_name"] or r["agent_id"],
                             "responded": 0, "_sum": 0.0, "attain": 0}
                        ag[r["agent_id"]] = a
                    a["responded"] += 1
                    a["_sum"] += frt
                    if frt <= sla["warn"]:
                        a["attain"] += 1
                agent_frt = []
                for a in ag.values():
                    n = a["responded"]
                    agent_frt.append({
                        "agent_id": a["agent_id"], "agent_name": a["agent_name"],
                        "responded": n,
                        "avg_sec": int(a["_sum"] / n) if n else 0,
                        "attain_rate": round(a["attain"] / n * 100, 1) if n else 0.0})
                agent_frt.sort(key=lambda x: -x["responded"])
                out["agent_frt"] = agent_frt
        except Exception:
            logger.debug("dashboard SLA/首响 统计失败（已忽略）", exc_info=True)
        try:
            out["funnel"] = web_funnel_snapshot(request, config_manager)
        except Exception:
            out["funnel"] = {}
        return out

    @app.get("/api/workspace/tags")
    async def api_workspace_tags(request: Request, limit: int = 100):
        """全部标签 + 使用计数 + 预设库颜色（标签自动补全/快筛/上色）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tags": []}
        return {"ok": True, "tags": store.list_all_tags(limit=max(1, min(300, int(limit or 100))))}

    @app.get("/api/workspace/tag-library")
    async def api_workspace_tag_library_list(request: Request):
        """预设标签库（名称/颜色/排序）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "library": []}
        return {"ok": True, "library": gw.list_tag_library()}

    @app.post("/api/workspace/tag-library")
    async def api_workspace_tag_library_upsert(request: Request, _=Depends(api_auth)):
        """新增/更新预设标签：{tag, color?, sort_order?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        tag = str(body.get("tag") or "").strip()
        if not tag:
            raise HTTPException(400, "tag 不能为空")
        ok = gw.upsert_tag_library(
            tag, color=str(body.get("color") or ""),
            sort_order=int(body.get("sort_order") or 0),
        )
        return {"ok": ok, "library": gw.list_tag_library()}

    @app.delete("/api/workspace/tag-library/{tag}")
    async def api_workspace_tag_library_delete(
        tag: str, request: Request, _=Depends(api_auth),
    ):
        """从预设库删除一个标签（不影响已打在客户上的标签）。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        return {"ok": gw.delete_tag_library(tag), "library": gw.list_tag_library()}

    @app.get("/api/workspace/contacts/export.csv")
    async def api_workspace_contacts_export(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 5000,
    ):
        """按当前筛选导出客户列表 CSV（最多 limit 行）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            raise HTTPException(503, "contacts 未启用")
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, _total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=max(1, min(20000, int(limit or 5000))), offset=0,
        )
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # Excel UTF-8 BOM
        w = csv.writer(buf)
        w.writerow(["contact_id", "name", "channels", "funnel_stage",
                    "intimacy", "has_lead", "tags", "follow_up_at", "last_active_at"])
        for r in rows:
            stage_lbl = FUNNEL_STAGE_LABELS.get(r.get("funnel_stage") or "",
                                                r.get("funnel_stage") or "")
            w.writerow([
                r.get("contact_id") or "",
                r.get("primary_name") or "",
                " ".join(_PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])),
                stage_lbl,
                "" if r.get("intimacy_score") is None else r.get("intimacy_score"),
                "1" if r.get("has_lead") else "0",
                " ".join(r.get("tags") or []),
                _fmt_ts(r.get("follow_up_at") or 0),
                _fmt_ts(r.get("last_active_at") or 0),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=contacts.csv"},
        )

    @app.get("/workspace/contacts", response_class=HTMLResponse)
    async def workspace_contacts_page(request: Request, _=Depends(page_auth)):
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "contacts_list.html", ctx)

    @app.get("/workspace/tasks", response_class=HTMLResponse)
    async def workspace_tasks_page(request: Request, _=Depends(page_auth)):
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "tasks.html", ctx)

    @app.get("/workspace/dash", response_class=HTMLResponse)
    async def workspace_dash_page(request: Request, _=Depends(page_auth)):
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "workspace_dashboard.html", ctx)

    @app.get("/api/unified-inbox/templates")
    async def api_unified_inbox_templates(request: Request):
        """快捷回复模板（workspace + messenger approval + templates.yaml）。"""
        api_auth(request)
        tpls = _collect_quick_templates(config_manager)
        return {"ok": True, "templates": tpls, "count": len(tpls)}

    @app.get("/api/unified-inbox/kb-search")
    async def api_unified_inbox_kb_search(
        request: Request,
        q: str = "",
        limit: int = 5,
    ):
        """KB 内联检索：坐席在工作台快速查话术/知识条目。"""
        api_auth(request)
        query = str(q or "").strip()
        limit = max(1, min(10, int(limit or 5)))
        kb = getattr(request.app.state, "kb_store", None)
        if kb is None:
            return {"ok": False, "entries": [], "error": "kb_unavailable"}
        if not query:
            return {"ok": True, "entries": [], "search_mode": "none"}
        try:
            result = kb.search(query, top_k=limit)
        except Exception:
            logger.debug("kb-search 失败", exc_info=True)
            return {"ok": False, "entries": [], "error": "search_failed"}
        entries: List[Dict[str, Any]] = []
        for row in result.get("entries") or []:
            answer = (
                row.get("example_reply_zh")
                or row.get("example_reply")
                or row.get("steps")
                or ""
            )
            entries.append({
                "entry_id": row.get("id") or row.get("entry_id") or "",
                "title": row.get("title") or row.get("scenario") or "",
                "answer": str(answer).strip(),
                "category": row.get("category") or "",
                "score": row.get("_score"),
                "search_mode": row.get("_mode") or result.get("search_mode"),
            })
        return {
            "ok": True,
            "entries": entries,
            "search_mode": result.get("search_mode") or "bm25",
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
        out = analysis.to_dict()
        # C1 LLM 可能抽出 order_no（动态属性）；否则用通用兜底正则
        order_no = str(getattr(analysis, "order_no", "") or "").strip() or _extract_order_no(text)
        out["order_no"] = order_no
        result: Dict[str, Any] = {"ok": True, "analysis": out}

        # Phase D：检测到订单号 + 电商工具启用 → 查订单并回事实串（事实校验，勿编造）
        ecom = _ecommerce_tools(request)
        if order_no and ecom is not None:
            try:
                tr = await ecom.lookup_order(order_no, by="inbox_analyze")
                d = tr.to_dict()
                d["facts"] = tr.to_context_facts()
                result["order_lookup"] = d
            except Exception:
                logger.debug("inbox analyze 订单查询失败（已忽略）", exc_info=True)
        return result

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

        _send_agent = _session_agent(request)

        def _mark_send(cid: str) -> None:
            """发送成功后打坐席首响归属点（best-effort，失败不影响发送）。"""
            ibx = _inbox_store(request)
            if ibx is None or not cid:
                return
            try:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
            except Exception:
                logger.debug("record_agent_send 失败（已忽略）", exc_info=True)

        if platform == "line":
            svcs = _get_line_services(request)
            target = next((s for s in svcs if getattr(s, "account_id", "default") == account_id), None)
            if target is None and svcs:
                target = svcs[0]
            if target is None:
                raise HTTPException(503, "LINE 服务未启用")
            try:
                result = await target.send_to_chat(chat_key=chat_key, text=text)
                _mark_send(_conv_id("line", account_id, chat_key))
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
                _mark_send(_conv_id("whatsapp", account_id, chat_key))
                return {"ok": True, "result": result}
            except AttributeError:
                raise HTTPException(501, "WhatsApp 暂不支持主动发送（需启用 approve 模式）")

        elif platform == "messenger":
            msvc = _get_messenger_service(request)
            if msvc is None:
                raise HTTPException(503, "Messenger 服务未启用")
            try:
                result = await msvc.send_to_chat_name(chat_name=chat_key, text=text)
                _mark_send(_conv_id("messenger", account_id, chat_key))
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
                _mark_send(_conv_id("telegram", account_id, chat_key))
                return {"ok": True, "result": result}
            except Exception as ex:
                raise HTTPException(500, str(ex))

        elif platform == "web":
            # 坐席人工接管：落库 + 推送给访客浏览器（SSE）+ 切 manual 让 AI 停
            from src.integrations.web_chat.hub import get_web_outbound_hub
            from src.integrations.web_chat.service import WebChatService
            _wc = WebChatService.from_config(
                (config_manager.config or {}) if config_manager else {}
            )
            visitor_id = chat_key
            cid = _wc.conversation_id(visitor_id)
            store = _inbox_store(request)
            try:
                _wc.record_message(store, visitor_id, text=text,
                                   direction="out", display_name="")
            except Exception:
                logger.debug("[web_chat] 坐席出站落库失败", exc_info=True)
            _mark_send(cid)
            try:
                get_web_outbound_hub().publish(cid, {
                    "type": "web_outbound", "conversation_id": cid,
                    "text": text, "by": "agent", "ts": time.time(),
                })
            except Exception:
                logger.debug("[web_chat] 坐席出站推送失败", exc_info=True)
            # 记入漏斗（坐席人工回复也是一次 msg_out）
            _contacts = getattr(request.app.state, "contacts", None)
            _hooks = getattr(_contacts, "hooks", None) if _contacts is not None else None
            if _hooks is not None:
                try:
                    _hooks.on_message(
                        channel="web", account_id=_wc.account_id, external_id=visitor_id,
                        direction="out", text_preview=text[:120], trace_id="web-agent",
                    )
                except Exception:
                    logger.debug("[web_chat] funnel(agent out) 失败", exc_info=True)
            # 人工接管后默认停 AI（坐席可在工作台把模式切回 auto_ai 交还）
            try:
                _write_automation_mode(request, cid, "manual")
            except Exception:
                pass
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("inbox_message", {
                    "conversation_id": cid, "platform": "web",
                    "account_id": _wc.account_id, "chat_key": visitor_id,
                    "preview": text[:80], "direction": "out", "ts": time.time(),
                })
            except Exception:
                pass
            return {"ok": True, "result": {"delivered": True}}

        else:
            raise HTTPException(400, f"不支持的平台: {platform}")
