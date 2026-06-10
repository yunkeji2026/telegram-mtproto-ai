"""统一收件箱路由 — 聚合所有平台最近消息/对话 + 跨平台发送。

端点：
  GET  /unified-inbox                   — 页面
  GET  /api/unified-inbox/chats         — 各平台最近对话列表（聚合）
  POST /api/unified-inbox/send          — 发送消息到指定平台/账号
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from src.ai.chat_assistant_service import ChatAssistantService
from src.ai.translation_service import TranslationService, detect_language
from src.inbox.channel_adapters import (
    ChannelSendError,
    collect_chats_via_adapters,
    default_inbox_adapters,
    send_via_adapters,
    status_via_adapters,
)
from src.inbox.ingest import ingest_collected_chats, ingest_thread
from src.integrations.account_registry import get_account_registry
from src.integrations.account_orchestrator import (
    account_key as _acct_key,
    ensure_builtin_workers,
    get_orchestrator,
    orchestrator_enabled,
)
from src.integrations.fingerprint import get_fingerprint_store, summarize as fp_summarize
from src.integrations.proxy_pool import get_proxy_pool
from src.integrations.platform_login import (
    SUPPORTED_PLATFORMS,
    get_login_manager,
    get_login_provider,
    list_modes,
    mode_available,
    online_account_keys,
)
from src.inbox.normalizer import (
    candidate_messages_from_source,
    conv_id,
    message_obj,
    normalize_chat,
    store_message_to_obj,
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


# ─── P30: 风险信号检测 + 话术衍生 ──────────────────────────────────────────

_RISK_PATTERNS: List[Dict[str, Any]] = [
    {
        "signal": "price_negotiation",
        "label": "价格谈判",
        "patterns": ["多少钱", "价格", "便宜", "优惠", "折扣", "打折", "降价", "便宜点",
                     "price", "discount", "cheaper", "offer"],
    },
    {
        "signal": "complaint",
        "label": "投诉抱怨",
        "patterns": ["投诉", "投诉你", "找你们老板", "太差", "骗人", "退款", "退钱",
                     "complaint", "refund", "scam", "terrible", "awful"],
    },
    {
        "signal": "churn_intent",
        "label": "流失意向",
        "patterns": ["不买了", "取消", "算了", "不要了", "退订", "注销",
                     "cancel", "unsubscribe", "not interested"],
    },
    {
        "signal": "comparison_shopping",
        "label": "比价竞品",
        "patterns": ["别家", "其他家", "竞争对手", "比较", "哪家好",
                     "competitor", "compare", "other brand", "versus"],
    },
    {
        "signal": "urgency",
        "label": "紧急催促",
        "patterns": ["马上", "立刻", "赶紧", "尽快", "等很久", "urgent", "asap", "hurry", "immediately"],
    },
    {
        "signal": "escalation_intent",
        "label": "升级意图",
        "patterns": ["报警", "12315", "消费者协会", "律师", "起诉", "曝光",
                     "sue", "lawyer", "report", "media"],
    },
]


# ─── P33：语种检测 + 多语言话术模板 ─────────────────────────────────────────

# 印尼语 / 英语常用词（拉丁含糊文本的关键词打分，保留 P33 业务语境）
_ID_KEYWORDS = {"anda", "saya", "ini", "itu", "tidak", "dengan", "untuk", "yang",
                "bisa", "kami", "harga", "mau", "sudah", "belum", "tolong"}
_EN_KEYWORDS = {"the", "is", "are", "was", "were", "have", "has", "you", "your",
                "please", "sorry", "thank", "hello", "hi", "can", "how", "what"}


def _detect_language(text: str) -> str:
    """P33→统一：复用全局确定性检测器 ``translation_service.detect_language``。

    与全局检测器的差异（仅业务语境兜底，逐字保留 P33 原行为）：
    - 空文本 → 'zh'（业务主力语言，全局检测器返回 'unknown'）。
    - 强检测器落到弱的 'en'/'unknown'（含糊拉丁）时，沿用 P33 的 id/en 关键词
      打分，且默认回落 'zh'——避免把无明确英文关键词的拉丁文本误判为英文。

    脚本类语种（zh/ja/ko/th/km/ar/ru/hi/he/el）与越南语、明确拉丁关键词
    （es/pt/fr/de/it/tr/id/tl）一律采信全局检测器，从而白嫖其泰铢加固与新增语种。
    """
    if not text or not text.strip():
        return "zh"

    lang = detect_language(text)
    if lang not in ("en", "unknown"):
        return lang

    # 含糊拉丁：保留 P33 的 id/en 关键词打分，默认业务语言 zh
    words_lc = set(text.lower().split())
    id_score = len(words_lc & _ID_KEYWORDS)
    en_score = len(words_lc & _EN_KEYWORDS)
    if id_score > en_score and id_score >= 2:
        return "id"
    if en_score >= 2:
        return "en"
    return "zh"


# 多语言话术模板（P33）：各语种的缓和前缀、主动 CTA
_LANG_TEMPLATES = {
    "zh": {
        "soothing_prefix": "非常抱歉给您带来了不便，我们高度重视您的反馈。",
        "active_cta": "如果您有任何疑问，欢迎随时联系我们！",
        "labels": {
            "safe": "标准回复",
            "active": "主动引导",
            "soothing": "缓和共情",
        },
    },
    "en": {
        "soothing_prefix": "We sincerely apologize for any inconvenience. Your feedback is very important to us. ",
        "active_cta": " If you have any further questions, please feel free to reach out anytime!",
        "labels": {
            "safe": "Standard Reply",
            "active": "Proactive Engagement",
            "soothing": "Empathetic Reply",
        },
    },
    "id": {
        "soothing_prefix": "Kami mohon maaf atas ketidaknyamanan ini. Masukan Anda sangat berarti bagi kami. ",
        "active_cta": " Jika ada pertanyaan lebih lanjut, jangan ragu untuk menghubungi kami kapan saja!",
        "labels": {
            "safe": "Balasan Standar",
            "active": "Pendekatan Proaktif",
            "soothing": "Balasan Empatik",
        },
    },
    "th": {
        "soothing_prefix": "ขออภัยในความไม่สะดวกอย่างสุดซึ้ง เราให้ความสำคัญกับความคิดเห็นของคุณอย่างยิ่ง ",
        "active_cta": " หากมีคำถามใดๆ กรุณาติดต่อเราได้ตลอดเวลา!",
        "labels": {
            "safe": "ตอบมาตรฐาน",
            "active": "เชิงรุก",
            "soothing": "เห็นอกเห็นใจ",
        },
    },
}


def _detect_risk_signals(text: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """P30-A：多模式风险信号检测（规则驱动，零 LLM 消耗）。

    合并最近 5 条入站消息 + 当前文本，逐信号类型匹配关键词。
    返回命中的信号列表：[{signal, label, matched}]
    """
    # 合并最近 5 条入站消息
    recent_texts = [str(m.get("text") or "") for m in messages[-5:]
                    if m.get("direction") in ("in", "inbound")] + [text]
    combined = " ".join(recent_texts).lower()

    signals: List[Dict[str, Any]] = []
    for item in _RISK_PATTERNS:
        matched = [p for p in item["patterns"] if p.lower() in combined]
        if matched:
            signals.append({
                "signal": item["signal"],
                "label": item["label"],
                "matched": matched[:3],  # 最多展示 3 个命中关键词
            })
    return signals


def _derive_tiered_replies(
    base_reply: str,
    risk_signals: List[Dict[str, Any]],
    lang: str = "zh",
) -> List[Dict[str, Any]]:
    """P30-B / P33：基于 AI 基础回复衍生阶梯式话术（安全/标准/主动三档）。

    lang 参数用于选择对应语种的话术前缀与 CTA（P33 多语言支持）。
    risk_signals 影响主动档的警示标注（高风险时降级推荐主动档）。
    """
    has_risk = bool(risk_signals)
    high_risk_signals = {"complaint", "escalation_intent", "churn_intent"}
    is_high_risk = any(s["signal"] in high_risk_signals for s in risk_signals)

    # 读取对应语种模板（P33），回落中文
    tpl = _LANG_TEMPLATES.get(lang) or _LANG_TEMPLATES["zh"]
    labels = tpl["labels"]
    soothing_prefix = tpl["soothing_prefix"]
    active_cta = tpl["active_cta"]

    # 安全档：纯信息回复（无承诺、无价格）
    safe = {
        "text": base_reply,
        "rationale": labels.get("safe", "标准 AI 建议回复"),
        "risk_level": "low",
        "recommended": not is_high_risk,
        "lang": lang,
    }

    # 主动档：添加行动引导（CTA），适合低风险/价值转化场景
    active_text = (base_reply.rstrip("。！.!") + active_cta) if base_reply else active_cta
    active = {
        "text": active_text,
        "rationale": labels.get("active", "主动引导型——追加行动号召"),
        "risk_level": "medium",
        "recommended": not has_risk,
        "lang": lang,
    }

    # 缓和档：高风险时（投诉/升级/流失）推荐共情优先
    soothing_text = soothing_prefix + base_reply if base_reply else soothing_prefix
    soothing = {
        "text": soothing_text,
        "rationale": labels.get("soothing", "缓和共情型——高风险场景首选"),
        "risk_level": "high" if is_high_risk else "medium",
        "recommended": is_high_risk,
        "lang": lang,
    }

    return [safe, active, soothing] if not is_high_risk else [soothing, safe, active]


def _build_context_summary(messages: List[Dict[str, Any]]) -> str:
    """P30-C：多轮对话上下文摘要（规则兜底，LLM 可覆盖）。

    取最近 10 条消息，按方向交替，输出"客户说了什么 / 坐席说了什么"简洁摘要。
    """
    lines: List[str] = []
    for m in messages[-10:]:
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        direction = m.get("direction", "")
        role = "客户" if direction in ("in", "inbound") else "坐席"
        lines.append(f"{role}：{text[:60]}{'…' if len(text)>60 else ''}")
    return " | ".join(lines) if lines else ""


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
    """旁路写入持久层，并在首轮冷启动后开启实时 SSE 事件发布。

    首次调用时 publish_events=False（冷启动不洪泛），之后切换为 True；
    仅有真正新消息（store.ingest_batch n>0）时才发 inbox_message 事件。
    """
    store = _inbox_store(request)
    if store is None or not chats:
        return
    try:
        # 首轮冷启动：向 store 写入存量数据但不发事件（避免把历史消息全部推送）
        first_done = getattr(request.app.state, "_inbox_first_ingest_done", False)
        ingest_collected_chats(store, chats, publish_events=first_done)
        if not first_done:
            request.app.state._inbox_first_ingest_done = True
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


_SUPERVISOR_ROLES = {"master", "admin"}


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
    role = str(sess.get("role") or "")
    return {"agent_id": uid, "display_name": str(name or uid), "role": role}


def _is_supervisor(request: Request) -> bool:
    """主管能力 = 角色属于 master/admin（管理向功能的统一门槛）。"""
    return _session_agent(request).get("role", "") in _SUPERVISOR_ROLES


def _require_supervisor(request: Request) -> None:
    """主管专属端点守卫；非主管抛 403。"""
    if not _is_supervisor(request):
        raise HTTPException(403, "需要主管权限")


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


def _conv_relationship_context(
    request: Request, conversation_id: str, store: Any,
) -> Dict[str, Any]:
    """P43/P45：拉取会话关系阶段上下文（轮次 + 亲密度 + contact_id）。"""
    cid = str(conversation_id or "").strip()
    ctx: Dict[str, Any] = {
        "conversation_id": cid,
        "message_count": 0,
        "exchange_count": 0,
        "intimacy_score": None,
        "contact_id": "",
        "last_msg_text": "",
        "last_msg_direction": "in",
    }
    if store is None or not cid:
        return ctx
    try:
        msg_count = store._conn.execute(
            "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?", (cid,),
        ).fetchone()["c"]
        ctx["message_count"] = int(msg_count or 0)
        ctx["exchange_count"] = max(0, ctx["message_count"] // 2)
        rows = store._conn.execute(
            """SELECT direction, text FROM messages
               WHERE conversation_id = ? ORDER BY ts DESC LIMIT 20""",
            (cid,),
        ).fetchall()
        for r in rows:
            if r["direction"] in ("in", "inbound"):
                ctx["last_msg_text"] = str(r["text"] or "")
                ctx["last_msg_direction"] = "in"
                break
            ctx["last_msg_direction"] = str(r["direction"] or "in")
        meta = store.get_conv_meta(cid) or {}
        ctx["contact_id"] = str(meta.get("contact_id") or "")
        if ctx["contact_id"]:
            cs = _contacts_store(request)
            if cs is not None:
                try:
                    journey = cs.get_journey_by_contact(ctx["contact_id"])
                    if journey is not None:
                        ctx["intimacy_score"] = float(journey.intimacy_score or 0)
                except Exception:
                    pass
    except Exception:
        logger.debug("_conv_relationship_context 失败", exc_info=True)
    return ctx


def _agent_from_request(request: Request) -> Tuple[str, str]:
    agent_id = str(request.session.get("user_name") or request.session.get("username") or "")
    agent_name = str(request.session.get("display_name") or agent_id)
    return agent_id, agent_name


def _user_store_from_config(config_manager: Any) -> Any:
    """P48：惰性加载 WebUserStore（与 admin 同路径）。"""
    if config_manager is None:
        return None
    try:
        from src.utils.web_user_store import WebUserStore
        cfg_dir = config_manager.config_path.parent
        return WebUserStore(cfg_dir / "web_users.db")
    except Exception:
        return None


def _build_copilot_context(
    request: Request,
    conversation_id: str,
    store: Any,
    *,
    trigger: str = "",
    workflow_text: str = "",
    workflow_chain_name: str = "",
    workflow_step: int = 0,
    mention_body: str = "",
    mention_from: str = "",
) -> Dict[str, Any]:
    """P49：组装 Copilot 联动上下文（阶段 / 流失 / 工作链 / @mention）。"""
    rel = _build_relationship_stage_payload(request, conversation_id, store)
    mctx = _mention_context_for_conv(request, conversation_id, store)
    me = _session_agent(request)["agent_id"]

    mention_note = mention_body
    mention_agent = mention_from
    if store is not None and not mention_note:
        try:
            recent = store.get_recent_mention_note(conversation_id, me)
            if recent:
                mention_note = str(recent.get("body") or "")
                mention_agent = str(recent.get("agent_name") or recent.get("agent_id") or "")
        except Exception:
            pass

    script_topics: List[Dict[str, Any]] = []
    if store is not None:
        try:
            from src.inbox.conversation_script import ConversationScriptEngine
            engine = ConversationScriptEngine()
            stage = str(rel.get("display_stage") or rel.get("stage") or "initial")
            script_topics = engine.suggest_topics(
                stage,
                custom_topics=store.list_script_topics(),
                reunion=bool(rel.get("reunion")),
                limit=3,
            ).get("topics", [])
        except Exception:
            pass

    if not trigger and mention_note:
        trigger = "mention"
    elif not trigger and mctx.get("churn_level") == "high":
        trigger = "churn"
    elif not trigger and rel.get("reunion"):
        trigger = "reunion"

    contact_id = str((rel.get("context") or {}).get("contact_id") or "")
    recent_downgrade = False
    if store is not None and contact_id:
        try:
            import time as _t
            week_ago = _t.time() - 7 * 86400
            for row in store.list_contact_stage_audits(contact_id, limit=5):
                if (
                    row.get("action") == "stage_downgrade"
                    and float(row.get("ts") or 0) > week_ago
                ):
                    recent_downgrade = True
                    break
        except Exception:
            pass

    return {
        "trigger": trigger,
        "stage": str(rel.get("display_stage") or rel.get("stage") or "initial"),
        "stage_label": rel.get("display_stage_label") or rel.get("stage_label") or "",
        "contact_stage": rel.get("contact_stage") or "",
        "contact_stage_label": rel.get("contact_stage_label") or "",
        "next_stage_label": rel.get("next_stage_label") or "",
        "pending_stage_label": rel.get("pending_stage_label") or "",
        "pending_advancement": bool(rel.get("pending_advancement")),
        "reunion": bool(rel.get("reunion")),
        "recent_downgrade": recent_downgrade,
        "churn_level": mctx.get("churn_level") or "",
        "claim_agent_id": mctx.get("claim_agent_id") or "",
        "overdue_chain": mctx.get("overdue_chain"),
        "workflow_text": workflow_text,
        "workflow_chain_name": workflow_chain_name,
        "workflow_step": int(workflow_step or 0),
        "mention_note": mention_note,
        "mention_from": mention_agent,
        "script_topics": script_topics,
    }


async def _maybe_polish_copilot(
    request: Request,
    result: Dict[str, Any],
    *,
    conversation_id: str,
    partial_text: str,
    last_customer_msg: str,
    polish_requested: bool,
) -> Dict[str, Any]:
    """P52：可选 LLM 润色（失败回退规则建议）。"""
    from src.inbox.copilot_polisher import (
        get_polish_config,
        polish_suggestions,
        should_polish,
    )
    cm = getattr(request.app.state, "config_manager", None)
    cfg = get_polish_config(cm)
    if not should_polish(
        polish_requested=polish_requested,
        partial_text=partial_text,
        cfg=cfg,
    ):
        result["polished"] = False
        return result
    ai_client = getattr(request.app.state, "ai_client", None)
    ctx = result.get("context") or {}
    pr = await polish_suggestions(
        ai_client,
        result.get("suggestions") or [],
        context=ctx,
        last_customer_msg=last_customer_msg,
        cfg=cfg,
    )
    result["suggestions"] = pr.get("suggestions") or result.get("suggestions")
    result["polished"] = bool(pr.get("polished"))
    if pr.get("polish_error"):
        result["polish_error"] = pr["polish_error"]
    if pr.get("polish_count"):
        result["polish_count"] = pr["polish_count"]
    if result.get("polished"):
        store = _inbox_store(request)
        if store is not None:
            agent_id, _ = _agent_from_request(request)
            store.record_draft_audit(
                "", action="copilot_polish", agent_id=agent_id,
                reason=f"润色 {pr.get('polish_count', 0)} 条 Copilot 建议",
                conversation_id=conversation_id,
            )
    return result


def _record_copilot_impression_if_prefill(
    store: Any,
    conversation_id: str,
    agent_id: str,
    payload: Dict[str, Any],
    *,
    partial_text: str,
) -> None:
    """P54：仅预填（空 partial）记曝光，避免打字 debounce 刷屏。"""
    if store is None or (partial_text or "").strip():
        return
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return
    ctx = payload.get("context") or {}
    top = suggestions[0] if suggestions else {}
    try:
        store.record_copilot_impression(
            conversation_id, agent_id,
            trigger=str(ctx.get("trigger") or payload.get("trigger") or "open"),
            stage=str(ctx.get("stage") or payload.get("stage") or "initial"),
            polished=bool(payload.get("polished")),
            suggestion_count=len(suggestions),
            top_source=str(top.get("source") or ""),
        )
    except Exception:
        pass


def _record_copilot_adopt_from_send(
    store: Any,
    conversation_id: str,
    agent_id: str,
    text: str,
    meta: Any,
) -> None:
    """P54：发送时记录 Copilot 采纳（best-effort）。"""
    if store is None or not isinstance(meta, dict):
        return
    from src.inbox.copilot_stats import classify_adoption
    suggested = str(meta.get("suggested_text") or meta.get("text") or "")
    match = str(meta.get("match") or "").strip() or classify_adoption(suggested, text)
    try:
        store.record_copilot_adopt(
            conversation_id, agent_id,
            match=match,
            source=str(meta.get("source") or ""),
            polished=bool(meta.get("polished")),
            trigger=str(meta.get("trigger") or ""),
            stage=str(meta.get("stage") or ""),
            suggested_preview=suggested,
            sent_preview=text,
        )
    except Exception:
        pass


def _mention_context_for_conv(
    request: Request, conversation_id: str, store: Any,
) -> Dict[str, Any]:
    """P48：组装 @mention 推荐所需会话上下文。"""
    rel = _build_relationship_stage_payload(request, conversation_id, store)
    ctx = rel.get("context") or {}
    stage = str(rel.get("display_stage") or rel.get("stage") or "initial")
    churn_level = ""
    claim_agent_id = ""
    overdue_chain = False
    if store is not None:
        try:
            meta = store.get_conv_meta(conversation_id) or {}
            churn_raw = str(meta.get("churn_risk") or "")
            if churn_raw:
                cd = json.loads(churn_raw)
                churn_level = str(cd.get("level") or "")
        except Exception:
            pass
        try:
            claim = store.get_conversation_claim(conversation_id)
            if claim:
                claim_agent_id = str(claim.get("agent_id") or "")
        except Exception:
            pass
        try:
            overdue_chain = store.has_overdue_chain_execution(conversation_id)
        except Exception:
            pass
    return {
        "stage": stage,
        "stage_label": rel.get("display_stage_label") or rel.get("stage_label") or "",
        "churn_level": churn_level,
        "claim_agent_id": claim_agent_id,
        "overdue_chain": overdue_chain,
        "contact_id": ctx.get("contact_id") or "",
    }


def _build_contact_relationship_payload(
    request: Request,
    contact_id: str,
    store: Any,
) -> Dict[str, Any]:
    """P50：客户级关系阶段（聚合信号 + 冲突检测）。"""
    from src.inbox.contact_rel_stage import (
        detect_stage_conflict,
        enrich_with_contact_stage,
    )
    from src.inbox.relationship_stage import compute_relationship_stage, enrich_with_manual_state

    cs = _contacts_store(request)
    intimacy_score = None
    message_count = 0
    conv_ids: List[str] = []
    if store is not None:
        try:
            rows = store._conn.execute(
                "SELECT conversation_id FROM conversations WHERE contact_id = ? LIMIT 30",
                (contact_id,),
            ).fetchall()
            conv_ids = [r["conversation_id"] for r in rows]
            if conv_ids:
                ph = ",".join("?" * len(conv_ids))
                message_count = store._conn.execute(
                    f"SELECT COUNT(*) as c FROM messages WHERE conversation_id IN ({ph})",
                    conv_ids,
                ).fetchone()["c"]
        except Exception:
            pass
    if cs is not None:
        try:
            journey = cs.get_journey_by_contact(contact_id)
            if journey is not None:
                intimacy_score = float(journey.intimacy_score or 0)
        except Exception:
            pass

    contact_rec = store.get_contact_rel_stage(contact_id) if store else None
    contact_stage = str((contact_rec or {}).get("confirmed_stage") or "")
    conv_stages = store.list_conv_rel_stages_for_contact(contact_id) if store else {}
    conflict = detect_stage_conflict(contact_stage, conv_stages)

    exchange_count = max(0, int(message_count or 0) // 2)
    computed = compute_relationship_stage(
        exchange_count=exchange_count, intimacy_score=intimacy_score,
        previous_stage=contact_stage,
    )
    confirmed = contact_stage or computed.get("stage") or "initial"
    reunion_ack = float((contact_rec or {}).get("reunion_ack_ts") or 0)

    result = enrich_with_manual_state(
        computed,
        confirmed_stage=confirmed,
        pending_stage="",
        reunion_ack_ts=reunion_ack,
    )
    return enrich_with_contact_stage(
        result,
        contact_stage=contact_stage,
        contact_updated_by=str((contact_rec or {}).get("updated_by") or ""),
        conflict=conflict,
    )


def _build_relationship_stage_payload(
    request: Request,
    conversation_id: str,
    store: Any,
    *,
    emit_pending_event: bool = False,
) -> Dict[str, Any]:
    """P43/P46/P50：构建关系阶段响应（确认制 + 客户级同步）。"""
    import time as _t
    from src.inbox.contact_rel_stage import (
        detect_stage_conflict,
        enrich_with_contact_stage,
    )
    from src.inbox.relationship_stage import (
        compute_relationship_stage,
        enrich_with_manual_state,
    )
    from src.utils.companion_relationship import STAGE_ORDER as _STAGES

    ctx = _conv_relationship_context(request, conversation_id, store)
    contact_id = str(ctx.get("contact_id") or "")
    meta = store.get_rel_stage_meta(conversation_id) if store else {
        "confirmed": "", "pending": "", "pending_ts": 0.0, "reunion_ack_ts": 0.0,
    }
    contact_rec = store.get_contact_rel_stage(contact_id) if store and contact_id else None
    contact_stage = str((contact_rec or {}).get("confirmed_stage") or "")
    confirmed = meta["confirmed"]
    computed = compute_relationship_stage(
        exchange_count=ctx["exchange_count"],
        intimacy_score=ctx["intimacy_score"],
        previous_stage=confirmed or contact_stage,
    )

    # P50：首次种子 — 优先客户级，否则写回客户级
    if store is not None and not confirmed:
        seed = contact_stage or computed.get("stage") or ""
        if seed:
            store.confirm_rel_stage(conversation_id, seed)
            confirmed = seed
            meta["confirmed"] = confirmed
            if contact_id and not contact_stage:
                store.set_contact_rel_stage(contact_id, seed)

    ci = _STAGES.index(computed["stage"]) if computed["stage"] in _STAGES else 0
    di = _STAGES.index(confirmed) if confirmed in _STAGES else 0
    pending_new = False
    if store is not None and ci > di:
        if meta["pending"] != computed["stage"]:
            store.set_rel_stage_pending(conversation_id, computed["stage"])
            pending_new = True
        pending_stage = computed["stage"]
    elif store is not None and meta["pending"]:
        store.clear_rel_stage_pending(conversation_id)
        pending_stage = ""
    else:
        pending_stage = meta["pending"] if ci > di else ""

    reunion_ack = float(meta["reunion_ack_ts"] or 0)
    if contact_rec and float(contact_rec.get("reunion_ack_ts") or 0) > reunion_ack:
        reunion_ack = float(contact_rec["reunion_ack_ts"])

    result = enrich_with_manual_state(
        computed,
        confirmed_stage=confirmed,
        pending_stage=pending_stage,
        reunion_ack_ts=reunion_ack,
    )

    conflict = None
    if store and contact_id:
        conv_stages = store.list_conv_rel_stages_for_contact(contact_id)
        conflict = detect_stage_conflict(contact_stage, conv_stages)
        result = enrich_with_contact_stage(
            result,
            contact_stage=contact_stage,
            contact_updated_by=str((contact_rec or {}).get("updated_by") or ""),
            conflict=conflict,
        )

    if emit_pending_event and pending_new:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("stage_advance_pending", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "confirmed_stage": confirmed,
                "confirmed_stage_label": result.get("confirmed_stage_label"),
                "pending_stage": result.get("pending_stage"),
                "pending_stage_label": result.get("pending_stage_label"),
                "ts": _t.time(),
            })
        except Exception:
            pass

    return {**result, "context": ctx}


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


def _is_protocol_account(request: Request, platform: str, account_id: str) -> bool:
    """该账号是否为 store-backed 模式（消息 push 落库、线程/列表按 store 读出）。

    含两类：``protocol``（编排器接管的真 worker）与 ``desktop``（桌面壳同步桥，无 worker）。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
        return bool(row and row.get("mode") in ("protocol", "desktop"))
    except Exception:
        return False


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


def _dnd_active(prefs: Dict[str, Any], now: Optional[float] = None) -> bool:
    """坐席当前是否处于免打扰时段（本地分钟，支持跨午夜）。"""
    try:
        start = int(prefs.get("dnd_start", -1))
        end = int(prefs.get("dnd_end", -1))
    except (TypeError, ValueError):
        return False
    if start < 0 or end < 0 or start == end:
        return False
    lt = time.localtime(now if now is not None else time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜


def _agent_sla_cfg(request: Request) -> Dict[str, Any]:
    """全局 SLA 阈值叠加当前坐席个性化覆盖 + 免打扰/静音状态。"""
    base = _sla_cfg(request)
    warn, crit = base["warn"], base["crit"]
    muted = False
    dnd = False
    inbox = _inbox_store(request)
    if inbox is not None:
        try:
            agent = _session_agent(request)
            prefs = inbox.get_agent_prefs(agent["agent_id"])
            if int(prefs.get("warn_sec") or 0) > 0:
                warn = int(prefs["warn_sec"])
            if int(prefs.get("crit_sec") or 0) > 0:
                crit = int(prefs["crit_sec"])
            muted = bool(prefs.get("muted"))
            dnd = _dnd_active(prefs)
        except Exception:
            logger.debug("读取坐席告警偏好失败（已忽略）", exc_info=True)
    if crit < warn:
        crit = warn
    return {"warn": warn, "crit": crit, "muted": muted, "dnd": dnd}


def _sla_alert_snapshot(request: Request) -> Dict[str, Any]:
    """当前 SLA 快照：等待/警告/严重计数 + 严重超时会话清单（告警徽标/SSE 用）。

    阈值按当前坐席个性化覆盖；静音或免打扰时段则 items 置空 + quiet=true，
    使徽标与 SSE toast 在该坐席侧静默（计数仍照常返回供仪表盘参考）。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "waiting": 0, "breaching": 0, "critical": 0,
                "items": [], "quiet": False}
    sla = _agent_sla_cfg(request)
    quiet = bool(sla["muted"] or sla["dnd"])
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
            "critical": len(items), "items": [] if quiet else items[:50],
            "quiet": quiet, "warn_sec": sla["warn"], "crit_sec": sla["crit"]}


def _presence_stale_sec(request: Request) -> float:
    """在线判定窗口（秒）：config.workspace.presence_stale_sec，默认 120。"""
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if isinstance(cfg, dict):
        ws = cfg.get("workspace") or {}
        try:
            return max(30.0, float(ws.get("presence_stale_sec") or 120))
        except (TypeError, ValueError):
            pass
    return 120.0


def _escalation_snapshot(request: Request) -> Dict[str, Any]:
    """升级快照（团队安全网，**全局口径、不受查看者个人静默影响**）。

    列出"严重超时(≥全局 crit)且无人有效处理"的会话 + 原因：
      unclaimed=无人认领 / holder_offline=认领坐席离线 / holder_quiet=认领坐席静音或免打扰。
    用于 6-18 个人可静默后的兜底：被放下的会话不能就此无人管。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "count": 0, "items": []}
    sla = _sla_cfg(request)  # 全局团队阈值，不叠加个人覆盖
    now = time.time()
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    dirs = inbox.last_message_dirs(list(cmap))
    claim_map: Dict[str, Dict[str, str]] = {}
    try:
        for cl in inbox.list_conversation_claims():
            claim_map[str(cl.get("conversation_id") or "")] = {
                "agent_id": str(cl.get("agent_id") or ""),
                "agent_name": str(cl.get("agent_name") or ""),
            }
    except Exception:
        logger.debug("escalation claim 读取失败（已忽略）", exc_info=True)
    online: Dict[str, str] = {}
    try:
        for p in inbox.list_agent_presence(
                active_within_sec=_presence_stale_sec(request)):
            online[str(p.get("agent_id") or "")] = str(p.get("status") or "")
    except Exception:
        logger.debug("escalation presence 读取失败（已忽略）", exc_info=True)
    items: List[Dict[str, Any]] = []
    for cid, info in dirs.items():
        if info.get("direction") != "in":
            continue
        wait = now - (info.get("ts") or now)
        if wait < sla["crit"]:
            continue
        cl = claim_map.get(cid)
        reason = ""
        if not cl or not cl["agent_id"]:
            reason = "unclaimed"
        else:
            aid = cl["agent_id"]
            status = online.get(aid)
            if status not in ("online", "busy"):
                reason = "holder_offline"
            else:
                try:
                    prefs = inbox.get_agent_prefs(aid)
                    if prefs.get("muted") or _dnd_active(prefs):
                        reason = "holder_quiet"
                except Exception:
                    reason = ""
        if not reason:
            continue
        c = cmap.get(cid) or {}
        items.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or cid),
            "wait_sec": int(max(0, wait)),
            "reason": reason,
            "agent_id": cl["agent_id"] if cl else "",
            "agent_name": (cl["agent_name"] if cl else "") or "",
        })
    items.sort(key=lambda x: -x["wait_sec"])
    today_count = 0
    try:
        lt = time.localtime(now)
        midnight = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        today_count = inbox.count_escalations_since(midnight)
    except Exception:
        logger.debug("escalation today_count 失败（已忽略）", exc_info=True)
    return {"ok": True, "count": len(items), "items": items[:50],
            "today_count": today_count, "crit_sec": sla["crit"]}


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


def _thread_messages_from_store(
    request: Request, conversation_id: str, limit: int = 50,
) -> Optional[List[Dict[str, Any]]]:
    """A1 读路径收尾：从 InboxStore 读会话历史（持久事实源），映射回 thread 消息形状。

    返回 None=store 不可用；返回 []=store 中该会话无消息（调用方据此决定是否回落实时）。
    """
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        rows = store.list_recent_messages(conversation_id, limit=limit)
    except Exception:
        logger.debug("store thread 读取失败（已忽略）", exc_info=True)
        return None
    return [store_message_to_obj(r) for r in rows]


def _store_conv_as_chat(request: Request, conversation_id: str) -> Optional[Dict[str, Any]]:
    """从 store 取持久会话行并映射为 chat dict（thread 在实时源已无该会话时兜底 header）。"""
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        row = store.get_conversation(conversation_id)
    except Exception:
        return None
    if not row:
        return None
    mode = _read_automation_mode(request, conversation_id)
    try:
        mc = store.count_messages(conversation_id)
    except Exception:
        mc = 0
    return store_row_to_chat(row, automation_mode=mode, message_count=mc)


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

        _esc_seen: set = set()

        def _pick_assigned_supervisor(inbox) -> str:
            """负载均衡：从在线主管中选当前指派数最少的那个。
            都不在线或无法确定时返回空串（保留广播语义）。"""
            if inbox is None:
                return ""
            try:
                now = time.time()
                since = now - 86400  # 24 h 窗口内的已指派数
                candidates: List[Dict[str, Any]] = [
                    p for p in inbox.list_agent_presence(
                        active_within_sec=_presence_stale_sec(request))
                    if p.get("status") in ("online", "busy")
                    and p.get("agent_id") in _SUPERVISOR_ROLES.__class__.__mro__  # 占位，下面替换
                ]
                # 真正过滤主管：从 web_users 存储读角色
                # 但 inbox_store 不关联 web_users → 降级：取全部在线坐席中 supervisor
                # （实战中 presence 列表来自各坐席 POST /presence；无法区分角色）
                # 简化策略：取在线 presence 中 role 字段为 master/admin 的；
                # 若全无 role 字段，回落取全部在线人中负载最低的。
                presence = inbox.list_agent_presence(
                    active_within_sec=_presence_stale_sec(request))
                online = [p for p in presence
                          if p.get("status") in ("online", "busy")]
                if not online:
                    return ""
                # 优先取有 supervisor role 的；无则取全部在线（团队规模小时合理）
                sups = [p for p in online
                        if str(p.get("role") or "") in _SUPERVISOR_ROLES]
                pool = sups if sups else online
                # 选指派数最少的
                best = min(
                    pool,
                    key=lambda p: inbox.count_assigned_escalations(
                        str(p["agent_id"]), since_ts=since),
                )
                return str(best.get("agent_id") or "")
            except Exception:
                logger.debug("auto-assign supervisor 失败（已忽略）", exc_info=True)
                return ""

        def _esc_pushes():
            """边沿触发：新升级 → 审计落库 + 自动指派主管 + 推定向 SSE 帧。

            Phase 6-24：每条新升级自动指派给负载最低的在线主管（assigned_to）。
            SSE 帧附带 assigned_to；前端据此决定是否显示 loud toast（指派给我）。
            """
            frames: List[str] = []
            try:
                snap = _escalation_snapshot(request)
                items = snap.get("items", [])
                cur = {it["conversation_id"] for it in items}
                inbox = _inbox_store(request)
                for it in items:
                    cid = it["conversation_id"]
                    if cid in _esc_seen:
                        continue
                    _esc_seen.add(cid)
                    assigned_to = ""
                    if inbox is not None:
                        try:
                            is_new = inbox.record_escalation(
                                cid, reason=it.get("reason", ""),
                                agent_id=it.get("agent_id", ""),
                                agent_name=it.get("agent_name", ""),
                                wait_sec=it.get("wait_sec", 0))
                            if is_new:
                                # 新升级：自动指派给负载最低在线主管
                                assigned_to = _pick_assigned_supervisor(inbox)
                                if assigned_to:
                                    # 查刚插入的 esc_id 再更新 assigned_to
                                    try:
                                        rows = inbox.list_escalations(
                                            since_ts=time.time() - 10, limit=5)
                                        esc_id = next(
                                            (r["id"] for r in rows
                                             if r.get("conversation_id") == cid),
                                            None)
                                        if esc_id is not None:
                                            inbox.set_escalation_assigned(
                                                esc_id, assigned_to)
                                    except Exception:
                                        logger.debug("set_escalation_assigned 失败",
                                                     exc_info=True)
                            else:
                                # 已有记录：查当前 assigned_to（保持前次指派）
                                try:
                                    rows = inbox.list_escalations(
                                        since_ts=time.time() - 3600, limit=20)
                                    existing = next(
                                        (r for r in rows
                                         if r.get("conversation_id") == cid), None)
                                    assigned_to = str(
                                        (existing or {}).get("assigned_to") or "")
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug("升级审计落库失败（已忽略）", exc_info=True)
                    payload = dict(it)
                    payload["assigned_to"] = assigned_to
                    frames.append(
                        "data: " + _json.dumps(
                            {"type": "escalation", "data": payload},
                            ensure_ascii=False) + "\n\n")
                _esc_seen.intersection_update(cur)
            except Exception:
                logger.debug("升级 SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        async def _gen():
            # P24: 通知队列（全局内存，保留最近 200 条重要通知）
            _notif_types = {
                "inbox_message", "draft_sla_breach", "draft_reassigned",
                "anomaly_alert", "sla_alert", "escalation", "queue_alert",
                "stage_advance", "stage_advance_pending", "stage_downgrade",
                "stage_reunion", "stage_sync", "workflow_step",
                "workflow_execution_completed", "workflow_execution_failed",
                "workflow_execution_cancelled",  # P44/P47
            }

            def _maybe_push_notif(evt: dict):
                """将重要事件写入 app.state.notif_queue（P24 通知中心）。"""
                if evt.get("type") not in _notif_types:
                    return
                nq: list = getattr(request.app.state, "notif_queue", None)
                if nq is None:
                    nq = []
                    request.app.state.notif_queue = nq
                import time as _time
                nq.append({**evt, "_notif_ts": int(_time.time() * 1000)})
                if len(nq) > 200:
                    del nq[:-200]

            try:
                # 仅 replay 最近的 inbox_message 事件，避免设备类噪声
                _sse_types = {
                    "inbox_message", "agent_presence",
                    "conversation_claim", "follow_up",
                    "draft_created",          # G1：自动草稿生成实时推送
                    "draft_sla_breach",       # K1：草稿 SLA 超时红线预警
                    "draft_reassigned",       # K2：无人应答自动再分配通知
                    "typing",                 # Phase 11：多坐席打字状态协同
                    "anomaly_alert",          # P24：异常告警通知
                    "sla_alert",             # P24：SLA 告警通知
                    "conv_note",             # P25：协作注解实时同步
                    "queue_alert",           # P32：SLA 坐席定向告警
                    "stage_advance",         # P43/P46：关系阶段确认进阶
                    "stage_advance_pending", # P46：待确认进阶
                    "stage_downgrade",       # P46：手动降级
                    "stage_reunion",         # P46：确认回暖
                    "stage_sync",            # P50/P53：客户级阶段对齐
                    "workflow_step",         # P44：工作链步骤通知
                    "workflow_execution_completed",  # P47
                    "workflow_execution_failed",
                    "workflow_execution_cancelled",
                }
                for evt in bus.recent_events(30):
                    if evt.get("type") in _sse_types:
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                        _maybe_push_notif(evt)
                # 连接建立即推一轮当前严重超时 + 升级（无需等首个心跳）
                for fr in _sla_pushes():
                    yield fr
                for fr in _esc_pushes():
                    yield fr
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        if evt.get("type") in _sse_types:
                            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                            _maybe_push_notif(evt)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        for fr in _sla_pushes():
                            yield fr
                        for fr in _esc_pushes():
                            yield fr
                        # K1/K2 SLAWatcher 状态（主管专属）：通过常规 SSE 帧告知在线人数
                        try:
                            _watcher = getattr(request.app.state, "sla_watcher", None)
                            if _watcher is not None and _is_supervisor(request):
                                snap = _watcher.status_snapshot()
                                yield "data: " + _json.dumps({
                                    "type": "sla_watcher_status",
                                    "data": snap,
                                }, ensure_ascii=False) + "\n\n"
                        except Exception:
                            pass
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

    @app.post("/api/workspace/typing")
    async def api_workspace_typing(request: Request, _=Depends(api_auth)):
        """Phase 11：多坐席打字状态协同 — 向同一对话的其他坐席发送实时 typing 事件。"""
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            raise HTTPException(400, "conversation_id 必填")
        agent = _session_agent(request)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("typing", {
                "conversation_id": conversation_id,
                "agent_id": agent["agent_id"],
                "agent_name": agent["display_name"],
                "ts": time.time(),
            })
        except Exception:
            logger.debug("typing 事件发布失败", exc_info=True)
        return {"ok": True}

    @app.get("/api/unified-inbox/chats")
    async def api_unified_inbox_chats(request: Request, limit: int = 30):
        api_auth(request)
        limit = max(5, min(100, int(limit or 30)))
        chats = _chats_for_listing(request, limit=limit)
        # 平台运行状态（A2：收敛到各渠道适配器 status，与 collect/send 对称）
        platform_status: Dict[str, Any] = status_via_adapters(request, _INBOX_ADAPTERS)
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
        # T1：批量挂载会话标签 + 归档状态
        try:
            _ibx2 = _inbox_store(request)
            if _ibx2 is not None and chats:
                _cids2 = [str(c.get("conversation_id") or "") for c in chats if c.get("conversation_id")]
                _tags_map = _ibx2.list_conv_tags_map(_cids2)
                for c in chats:
                    cid2 = str(c.get("conversation_id") or "")
                    meta2 = _tags_map.get(cid2, {})
                    c["conv_tags"] = meta2.get("tags", [])
                    c["archived"] = meta2.get("archived", False)
        except Exception:
            logger.debug("会话列表 tags 加载失败（已忽略）", exc_info=True)
        return {
            "ok": True,
            "ts": time.time(),
            "chats": chats,
            "platform_status": platform_status,
        }

    # ── 平台扫码登录（P3/M1：多方式并存 · 无限扫码 / 多账号接入 + 自助重连） ──
    def _platform_login_cfg() -> Dict[str, Any]:
        try:
            if config_manager is None:
                return {}
            return (config_manager.config or {}).get("platform_login", {}) or {}
        except Exception:
            return {}

    def _platform_login_enabled() -> bool:
        return bool(_platform_login_cfg().get("enabled", True))

    def _login_qr_data_url(qr_url: str) -> str:
        """把 tg://login?token=… 等登录 URL 服务端渲染为 base64 PNG data URL。

        令牌不出本机（避免泄露给第三方 QR 服务）。qrcode/PIL 缺失或失败时返回空串，
        前端回落为显示链接 / 设备端指引。
        """
        text = str(qr_url or "")
        if not text:
            return ""
        try:
            import base64
            import io
            import qrcode
            img = qrcode.make(text)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + \
                base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            logger.debug("登录二维码服务端渲染失败", exc_info=True)
            return ""

    def _ensure_login_providers() -> None:
        """按需注册真实 per-(platform,mode) provider（幂等、全程降级）。"""
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        try:
            from src.integrations.telegram_protocol_login import maybe_register as _tg_reg
            _tg_reg(cfg)
        except Exception:
            logger.debug("注册 telegram protocol provider 失败", exc_info=True)
        try:
            from src.integrations.whatsapp_baileys_login import maybe_register as _wa_reg
            _wa_reg(cfg)
        except Exception:
            logger.debug("注册 whatsapp baileys provider 失败", exc_info=True)

    def _persist_login_account(platform: str, account_id: str, sess: Any) -> None:
        """登录成功后把账号 + mode + 代理 + 指纹 + 备注落库，并把代理标记为已分配。"""
        try:
            get_account_registry().upsert(
                platform, account_id, mode=getattr(sess, "mode", "device"),
                status="online",
                label=(getattr(sess, "label", "") or None),
                proxy_id=(getattr(sess, "proxy_id", "") or None),
                fingerprint_id=(getattr(sess, "fingerprint_id", "") or None),
            )
            if getattr(sess, "proxy_id", ""):
                get_proxy_pool().assign(sess.proxy_id, f"{platform}:{account_id}")
        except Exception:
            logger.debug("账号注册表上线 upsert 失败", exc_info=True)

    @app.get("/api/platforms/{platform}/modes")
    async def api_platform_login_modes(platform: str, request: Request):
        api_auth(request)
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": f"不支持的平台: {platform}"}
        _ensure_login_providers()
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        return {"ok": True, "platform": platform, "modes": list_modes(platform, platform_cfg)}

    @app.post("/api/platforms/{platform}/login/start")
    async def api_platform_login_start(platform: str, request: Request):
        api_auth(request)
        if not _platform_login_enabled():
            return {"ok": False, "detail": "扫码登录功能未启用（platform_login.enabled）"}
        platform = str(platform or "").lower()
        if platform not in SUPPORTED_PLATFORMS:
            return {"ok": False, "detail": f"不支持的平台: {platform}"}
        if platform == "web":
            return {"ok": False, "detail": "网页客服为服务端原生渠道，无需扫码登录。"}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        account_id = str((body or {}).get("account_id") or "")
        # M4：账号配置（防关联）
        cfg_label = str((body or {}).get("label") or "")
        cfg_group = str((body or {}).get("group") or "")
        cfg_proxy_id = str((body or {}).get("proxy_id") or "")
        cfg_use_fp = bool((body or {}).get("use_fingerprint") or False)
        _ensure_login_providers()
        # 解析登录方式：缺省取该平台默认 mode
        platform_cfg = _platform_login_cfg().get(platform, {}) or {}
        modes = list_modes(platform, platform_cfg)
        mode = str((body or {}).get("mode") or "").lower()
        if not mode:
            mode = next((m["mode"] for m in modes if m["recommended"]),
                        modes[0]["mode"] if modes else "device")
        if not mode_available(platform, mode):
            return {"ok": False, "detail": f"{platform} 的「{mode}」登录方式暂未启用"}

        status_map = status_via_adapters(request, _INBOX_ADAPTERS)
        baseline = online_account_keys(status_map, platform)
        # 重连场景：目标账号当前离线，从基线移除，使其上线时被判定为「新上线」
        if account_id:
            baseline.discard(account_id)

        # M4：解析代理 + 生成/绑定指纹，组装 provider 上下文
        fingerprint_id = ""
        login_ctx: Dict[str, Any] = {}
        if cfg_proxy_id:
            try:
                px = get_proxy_pool().get(cfg_proxy_id, mask=False)
                if px:
                    login_ctx["proxy"] = px
            except Exception:
                logger.debug("读取代理失败", exc_info=True)
        if cfg_use_fp:
            try:
                fp = get_fingerprint_store().create(seed=cfg_label or None,
                                                    label=cfg_label)
                fingerprint_id = fp["fingerprint_id"]
                login_ctx["fingerprint"] = fp["profile"]
            except Exception:
                logger.debug("生成指纹失败", exc_info=True)

        qr_url = qr_image = instruction = ""
        poll_fn = cancel_fn = provider_state = None
        provider = get_login_provider(platform, mode)
        if provider is not None:
            try:
                try:
                    info = provider(request, platform, mode, account_id, ctx=login_ctx)
                except TypeError:
                    info = provider(request, platform, mode, account_id)
                if inspect.isawaitable(info):
                    info = await info
                info = info or {}
                qr_url = str(info.get("qr_url") or "")
                qr_image = str(info.get("qr_image") or "")
                instruction = str(info.get("instruction") or "")
                account_id = str(info.get("account_id") or account_id)
                poll_fn = info.get("poll")
                cancel_fn = info.get("cancel")
                provider_state = info.get("state")
            except Exception:
                logger.debug("登录 provider[%s:%s] 失败（回落设备端指引）",
                             platform, mode, exc_info=True)

        sess = get_login_manager().create(
            platform, account_id, baseline, mode=mode,
            qr_url=qr_url, qr_image=qr_image, instruction=instruction,
            label=cfg_label, group=cfg_group,
            proxy_id=cfg_proxy_id, fingerprint_id=fingerprint_id,
            provider_state=provider_state, poll_fn=poll_fn, cancel_fn=cancel_fn,
        )
        # 落库：重连/已知账号即记录（mode + 代理 + 指纹持久化，供编排器重启后正确拉起）
        if account_id:
            try:
                get_account_registry().upsert(
                    platform, account_id, mode=mode, status="pending",
                    label=cfg_label or None, proxy_id=cfg_proxy_id or None,
                    fingerprint_id=fingerprint_id or None)
                if cfg_proxy_id:
                    get_proxy_pool().assign(cfg_proxy_id, f"{platform}:{account_id}")
            except Exception:
                logger.debug("账号注册表 upsert 失败", exc_info=True)
        return {
            "ok": True,
            "login_id": sess.login_id,
            "mode": sess.mode,
            "status": sess.status,
            "qr_url": sess.qr_url,
            "qr_image": sess.qr_image or _login_qr_data_url(sess.qr_url),
            "instruction": sess.instruction,
        }

    @app.get("/api/platforms/{platform}/login/{login_id}/status")
    async def api_platform_login_status(platform: str, login_id: str, request: Request):
        api_auth(request)
        platform = str(platform or "").lower()
        sess = get_login_manager().get(login_id)
        if sess is None:
            return {"ok": True, "status": "expired", "detail": "登录会话不存在或已过期"}
        if sess.status in ("authorized", "failed"):
            return {"ok": True, "status": sess.status, "detail": sess.detail}
        if sess.is_expired():
            sess.status = "expired"
            return {"ok": True, "status": "expired"}
        # provider 事件驱动（protocol/web）：直接问 provider 拿登录结果
        if sess.poll_fn is not None:
            try:
                res = sess.poll_fn(sess)
                if inspect.isawaitable(res):
                    res = await res
                res = res or {}
                st = str(res.get("status") or sess.status)
                sess.status = st
                if st == "authorized" and res.get("account_id"):
                    _persist_login_account(platform, str(res["account_id"]), sess)
                poll_qr = str(res.get("qr_url") or sess.qr_url)
                if poll_qr and not sess.qr_url:
                    sess.qr_url = poll_qr
                return {"ok": True, "status": st,
                        "detail": str(res.get("detail") or ""),
                        "qr_url": poll_qr,
                        "qr_image": str(res.get("qr_image") or "")
                        or _login_qr_data_url(poll_qr)}
            except Exception:
                logger.debug("provider poll 失败", exc_info=True)
                return {"ok": True, "status": sess.status}
        # 实时对比基线：检测到该平台有新账号上线 → 判定登录成功
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            online = online_account_keys(status_map, platform)
            new_accounts = online - sess.baseline
            if new_accounts:
                sess.status = "authorized"
                for aid in new_accounts:
                    _persist_login_account(platform, aid, sess)
                return {"ok": True, "status": "authorized"}
        except Exception:
            logger.debug("登录状态轮询失败", exc_info=True)
        return {"ok": True, "status": sess.status, "instruction": sess.instruction}

    @app.post("/api/platforms/{platform}/login/{login_id}/cancel")
    async def api_platform_login_cancel(platform: str, login_id: str, request: Request):
        api_auth(request)
        sess = get_login_manager().get(login_id)
        if sess is not None and sess.cancel_fn is not None:
            try:
                res = sess.cancel_fn(sess)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                logger.debug("provider cancel 失败", exc_info=True)
        get_login_manager().cancel(login_id)
        return {"ok": True}

    # ── 代理池（M4：用户自填，一号一代理） ──────────────────────────────────
    @app.get("/api/proxies")
    async def api_proxies_list(request: Request):
        api_auth(request)
        return {"ok": True, "proxies": get_proxy_pool().list()}

    @app.post("/api/proxies")
    async def api_proxies_add(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            entry = get_proxy_pool().add(
                scheme=str((body or {}).get("scheme") or "socks5"),
                host=str((body or {}).get("host") or "").strip(),
                port=int((body or {}).get("port") or 0),
                username=str((body or {}).get("username") or ""),
                password=str((body or {}).get("password") or ""),
                label=str((body or {}).get("label") or ""),
            )
            return {"ok": True, "proxy": entry}
        except (ValueError, TypeError) as ex:
            return {"ok": False, "detail": str(ex)}

    @app.delete("/api/proxies/{proxy_id}")
    async def api_proxies_remove(proxy_id: str, request: Request):
        api_auth(request)
        get_proxy_pool().remove(proxy_id)
        return {"ok": True}

    @app.post("/api/proxies/{proxy_id}/test")
    async def api_proxies_test(proxy_id: str, request: Request):
        api_auth(request)
        ok = await get_proxy_pool().test(proxy_id)
        return {"ok": True, "reachable": ok,
                "status": "ok" if ok else "fail"}

    # ── 指纹（M4：自研，一号一指纹） ────────────────────────────────────────
    @app.get("/api/fingerprints")
    async def api_fingerprints_list(request: Request):
        api_auth(request)
        items = get_fingerprint_store().list()
        for it in items:
            it["summary"] = fp_summarize(it.get("profile") or {})
        return {"ok": True, "fingerprints": items}

    @app.post("/api/fingerprints/generate")
    async def api_fingerprints_generate(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        fp = get_fingerprint_store().create(
            seed=str((body or {}).get("seed") or "") or None,
            label=str((body or {}).get("label") or ""),
        )
        fp["summary"] = fp_summarize(fp.get("profile") or {})
        return {"ok": True, **fp}

    # ── 账号池编排器（M5：多账号 7×24 在线，默认关） ────────────────────────
    def _orch():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        ensure_builtin_workers(cfg)
        return get_orchestrator(cfg)

    # M6①：注册 protocol→收件箱 入站 sink（worker 收到消息时落库；store 在 emit 时惰性取）
    def _register_protocol_sink() -> None:
        try:
            from src.integrations.protocol_bridge import (
                ingest_incoming, register_inbox_sink,
            )

            def _sink(m: Dict[str, Any]) -> None:
                store = getattr(app.state, "inbox_store", None)
                if store is None:
                    return
                ingest_incoming(store, **m)

            register_inbox_sink(_sink)
        except Exception:
            logger.debug("注册 protocol 收件箱 sink 失败", exc_info=True)

    _register_protocol_sink()

    # Phase 3：注册 protocol 自动回复 hook（hook 内自带双闸门，恒注册、运行时按需生效）
    def _register_protocol_autoreply() -> None:
        try:
            from src.integrations.protocol_autoreply import build_reply_hook
            from src.integrations.protocol_bridge import register_reply_hook
            register_reply_hook(build_reply_hook(app))
        except Exception:
            logger.debug("注册 protocol 自动回复 hook 失败", exc_info=True)

    _register_protocol_autoreply()

    @app.post("/api/internal/protocol/ingest")
    async def api_protocol_ingest(request: Request):
        """内部入站桥：Baileys(Node) 等外部 worker 把收到的消息 push 进统一收件箱。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, "inbox store 未就绪")
        from src.integrations.protocol_bridge import (
            ingest_incoming, make_message, maybe_auto_reply,
        )
        if not str((body or {}).get("chat_key") or ""):
            raise HTTPException(400, "chat_key 不能为空")
        direction = str((body or {}).get("direction") or "in")
        cid = ingest_incoming(
            store,
            platform=str((body or {}).get("platform") or ""),
            account_id=str((body or {}).get("account_id") or ""),
            chat_key=str((body or {}).get("chat_key") or ""),
            name=str((body or {}).get("name") or ""),
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=direction,
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
        )
        if direction == "in":
            await maybe_auto_reply(make_message(
                platform=str((body or {}).get("platform") or ""),
                account_id=str((body or {}).get("account_id") or ""),
                chat_key=str((body or {}).get("chat_key") or ""),
                name=str((body or {}).get("name") or ""),
                text=str((body or {}).get("text") or ""),
                ts=float((body or {}).get("ts") or 0),
                msg_id=str((body or {}).get("msg_id") or ""),
            ))
        return {"ok": bool(cid), "conversation_id": cid or ""}

    def _collect_config_accounts(cfg: Dict[str, Any]) -> List[tuple]:
        """从 config.yaml 抽取各平台声明的账号 → (platform, account_id, mode, label)。

        覆盖 telegram.accounts[]（+ 扁平单号）与 line/messenger/whatsapp_rpa.accounts[]
        （+ 扁平 enabled 单号）。形态各异，全程防御式读取。
        """
        out: List[tuple] = []
        tg = cfg.get("telegram") or {}
        if isinstance(tg, dict):
            if tg.get("api_id") or tg.get("session_name"):
                out.append(("telegram", str(tg.get("session_name") or "default"),
                            "protocol", str(tg.get("label") or "Telegram")))
            for a in tg.get("accounts") or []:
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("id") or a.get("session_name") or "")
                if aid:
                    out.append(("telegram", aid, "protocol", str(a.get("label") or aid)))
        for plat, key in (("line", "line_rpa"), ("messenger", "messenger_rpa"),
                          ("whatsapp", "whatsapp_rpa")):
            block = cfg.get(key) or {}
            if not isinstance(block, dict):
                continue
            accs = block.get("accounts") or []
            if isinstance(accs, list) and accs:
                for a in accs:
                    if not isinstance(a, dict):
                        continue
                    aid = str(a.get("account_id") or a.get("id") or "")
                    if aid:
                        out.append((plat, aid, "device", str(a.get("label") or aid)))
            elif block.get("enabled"):
                aid = str(block.get("account_id") or f"{plat}_default")
                out.append((plat, aid, "device", str(block.get("label") or aid)))
        return out

    @app.get("/api/accounts")
    async def api_accounts_list(request: Request):
        """统一账号清单（Phase 2）：合并 registry + config.yaml + 运行时健康。

        让桌面端 / web 后台用同一份数据渲染「账号管理」面板，不必再拼 4 个接口。
        返回每个账号的 ``platform/account_id/mode/label/status/running/proxy_id/
        fingerprint_id/sources``（sources 标出来源：registry/config/runtime）。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        merged: Dict[tuple, Dict[str, Any]] = {}

        def _ensure(platform: str, account_id: str) -> Dict[str, Any]:
            k = (str(platform or "").lower(), str(account_id or ""))
            if k not in merged:
                merged[k] = {
                    "platform": k[0], "account_id": k[1], "mode": "",
                    "label": "", "status": "unknown", "running": False,
                    "proxy_id": "", "fingerprint_id": "", "auto_reply": False,
                    "auto_reply_override": {}, "sources": [],
                }
            return merged[k]

        # 1) 注册表（登录/编排/桌面 ingest 落库的权威账号）
        try:
            for row in get_account_registry().list():
                r = _ensure(row.get("platform"), row.get("account_id"))
                r["mode"] = row.get("mode") or r["mode"]
                r["label"] = row.get("label") or r["label"]
                r["status"] = row.get("status") or r["status"]
                r["proxy_id"] = row.get("proxy_id") or r["proxy_id"]
                r["fingerprint_id"] = row.get("fingerprint_id") or r["fingerprint_id"]
                meta = row.get("meta") or {}
                r["auto_reply"] = bool(meta.get("auto_reply"))
                r["auto_reply_override"] = dict(meta.get("autoreply_override") or {})
                if "registry" not in r["sources"]:
                    r["sources"].append("registry")
        except Exception:
            logger.debug("[accounts] registry 读取失败", exc_info=True)

        # 2) config.yaml 声明的账号（boot 时拉起的 RPA / 协议号）
        for platform, account_id, mode, label in _collect_config_accounts(cfg):
            r = _ensure(platform, account_id)
            if not r["mode"]:
                r["mode"] = mode
            if not r["label"]:
                r["label"] = label
            if "config" not in r["sources"]:
                r["sources"].append("config")

        # 3) 运行时健康（适配器在线状态）
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            for k, v in (status_map or {}).items():
                if not isinstance(v, dict):
                    continue
                platform = v.get("platform")
                account_id = v.get("account_id") or k
                if not platform:
                    continue
                r = _ensure(platform, account_id)
                r["running"] = bool(v.get("running"))
                if v.get("running"):
                    r["status"] = "online"
                if "runtime" not in r["sources"]:
                    r["sources"].append("runtime")
        except Exception:
            logger.debug("[accounts] 运行时状态读取失败", exc_info=True)

        # 4) 自动回复配额/熔断快照（Phase 5，仅协议号或已开自动回复的号）
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            from src.integrations.protocol_autoreply_settings import (
                cfg_with_settings,
            )
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            for a in merged.values():
                if a.get("auto_reply") or a.get("mode") == "protocol":
                    ov_rate = (a.get("auto_reply_override") or {}).get("rate") or {}
                    a["auto_reply_quota"] = lim.snapshot(
                        f"{a['platform']}:{a['account_id']}",
                        hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
        except Exception:
            logger.debug("[accounts] 配额快照读取失败", exc_info=True)

        accounts = sorted(merged.values(),
                          key=lambda x: (x["platform"], x["account_id"]))
        return {"ok": True, "accounts": accounts, "count": len(accounts)}

    @app.get("/api/accounts/orchestrator")
    async def api_orchestrator_status(request: Request):
        api_auth(request)
        return {"ok": True, **_orch().status()}

    @app.get("/api/accounts/protocol/readiness")
    async def api_protocol_readiness(request: Request):
        """协议栈联调自检：配置/依赖/服务可达性/编排器/入站 sink 的结构化就绪报告。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_diagnostics import readiness
        report = await readiness(cfg)
        return {"ok": True, **report}

    @app.post("/api/accounts/orchestrator/sync")
    async def api_orchestrator_sync(request: Request):
        api_auth(request)
        orch = _orch()
        await orch.sync()
        await orch.tick()
        return {"ok": True, **orch.status()}

    @app.post("/api/accounts/{platform}/{account_id}/start")
    async def api_account_start(platform: str, account_id: str, request: Request):
        api_auth(request)
        orch = _orch()
        acc = get_account_registry().get(platform, account_id) or {
            "platform": platform, "account_id": account_id}
        ok = await orch.start_account(acc)
        return {"ok": ok}

    @app.post("/api/accounts/{platform}/{account_id}/stop")
    async def api_account_stop(platform: str, account_id: str, request: Request):
        api_auth(request)
        await _orch().stop_account(_acct_key(platform, account_id))
        return {"ok": True}

    @app.post("/api/accounts/{platform}/{account_id}/restart")
    async def api_account_restart(platform: str, account_id: str, request: Request):
        api_auth(request)
        ok = await _orch().restart_account(_acct_key(platform, account_id))
        return {"ok": ok}

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply")
    async def api_account_auto_reply(platform: str, account_id: str, request: Request):
        """切换某协议账号的 7×24 自动回复（账号闸门，写入 registry meta.auto_reply）。

        注意这是「账号闸门」；真正自动发还需全局 ``config.protocol_autoreply.enabled``
        同时打开（双闸门）。body: {enabled: bool}
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool((body or {}).get("enabled"))
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, "账号不存在")
        meta = dict(row.get("meta") or {})
        was = bool(meta.get("auto_reply"))
        meta["auto_reply"] = enabled
        reg.upsert(platform, account_id, meta=meta)
        if was != enabled:
            try:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="toggle",
                    platform=platform, account_id=account_id,
                    changes=[{"key": "auto_reply", "old": was, "new": enabled}])
            except Exception:
                logger.debug("[autoreply] 开关审计失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "platform": platform, "account_id": account_id,
                "auto_reply": enabled, "global_enabled": global_on,
                "effective": enabled and global_on}

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply/override")
    async def api_account_auto_reply_override(
        platform: str, account_id: str, request: Request,
    ):
        """按账号覆盖自动回复参数(配额/营业时段/延迟)，写 registry meta.autoreply_override。

        body: {rate?, hours?, delay?}（白名单深合并到现有覆盖）；
        或 {reset: true} 清空该账号覆盖（回落全局）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, "账号不存在")
        from src.integrations.protocol_autoreply_settings import (
            deep_merge, diff_settings, sanitize_override,
        )
        meta = dict(row.get("meta") or {})
        before_ov = dict(meta.get("autoreply_override") or {})
        if (body or {}).get("reset"):
            meta.pop("autoreply_override", None)
            override: Dict[str, Any] = {}
        else:
            clean = sanitize_override(body or {})
            override = deep_merge(dict(meta.get("autoreply_override") or {}), clean)
            meta["autoreply_override"] = override
        reg.upsert(platform, account_id, meta=meta)
        try:
            changes = diff_settings(before_ov, override)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="account",
                    platform=platform, account_id=account_id, changes=changes)
        except Exception:
            logger.debug("[autoreply] 覆盖审计失败", exc_info=True)
        return {"ok": True, "platform": platform, "account_id": account_id,
                "override": override}

    @app.get("/api/accounts/auto-reply/audit")
    async def api_account_auto_reply_audit(request: Request):
        """自动回复实时流（Phase 4）：最近 N 条决策 + 窗口统计 + 全局闸门状态。

        query: limit(默认50,≤500) / platform / account_id / since(秒,默认24h)
        """
        api_auth(request)
        qp = request.query_params
        try:
            limit = int(qp.get("limit") or 50)
        except Exception:
            limit = 50
        platform = qp.get("platform") or None
        account_id = qp.get("account_id") or None
        try:
            since_sec = float(qp.get("since") or 86400)
        except Exception:
            since_sec = 86400
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        audit = get_autoreply_audit()
        items = audit.recent(limit=limit, platform=platform, account_id=account_id)
        stats = audit.stats(since_ts=time.time() - max(0.0, since_sec))
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "items": items, "stats": stats,
                "global_enabled": global_on, "count": len(items)}

    @app.get("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_get(request: Request):
        """读自动回复全局有效设置（config.yaml 基底 + JSON 覆盖）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.post("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_set(request: Request):
        """改自动回复全局设置（白名单校验落盘 + 热更新限流器，无需重启）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, diff_settings, effective_settings, save,
        )
        cfg0 = (config_manager.config if config_manager is not None else {}) or {}
        before = effective_settings(cfg0)
        merged = save(body or {})
        after = effective_settings(cfg0)
        try:
            changes = diff_settings(before, after)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="global",
                    changes=changes)
        except Exception:
            logger.debug("[autoreply] 配置变更审计失败", exc_info=True)
        # 热更新限流器阈值
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            cfg = (config_manager.config if config_manager is not None else {}) or {}
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            rate = merged.get("rate") or {}
            brk = merged.get("breaker") or {}
            lim.configure(
                hourly=rate.get("hourly"), daily=rate.get("daily"),
                breaker_threshold=brk.get("threshold"),
                breaker_cooldown=brk.get("cooldown_sec"),
            )
        except Exception:
            logger.debug("[autoreply] 限流器热更新失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.get("/api/accounts/auto-reply/health")
    async def api_account_auto_reply_health(request: Request):
        """自动回复一键体检：全局/账号开关、配额余量、熔断、webhook、SkillManager 就绪
        + 最近配置变更，聚合成一张放量前自检表。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, effective_settings,
        )
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        eff = effective_settings(cfg)
        global_on = bool(eff.get("enabled", False))
        lim = get_autoreply_limiter(cfg_with_settings(cfg))

        accounts_on = 0
        protocol_n = 0
        circuit_open: List[str] = []
        try:
            for row in get_account_registry().list():
                meta = row.get("meta") or {}
                if row.get("mode") == "protocol":
                    protocol_n += 1
                if meta.get("auto_reply"):
                    accounts_on += 1
                ov_rate = (meta.get("autoreply_override") or {}).get("rate") or {}
                snap = lim.snapshot(
                    f"{row.get('platform')}:{row.get('account_id')}",
                    hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
                if snap.get("circuit_open"):
                    circuit_open.append(f"{row.get('platform')}:{row.get('account_id')}")
        except Exception:
            logger.debug("[autoreply-health] registry 读取失败", exc_info=True)

        # webhook 是否订阅了 autoreply_alert（用有效列表：覆盖层优先）
        webhook_on = False
        try:
            from src.integrations.notify_webhooks_store import effective_webhooks
            for wh in effective_webhooks(cfg):
                if wh.get("enabled") is False:
                    continue
                evs = wh.get("events") or []
                if "all" in evs or "autoreply_alert" in evs:
                    webhook_on = True
                    break
        except Exception:
            pass

        sm_ready = getattr(app.state, "skill_manager", None) is not None
        stats = get_autoreply_audit().stats(since_ts=time.time() - 86400)

        warnings: List[str] = []
        if global_on and accounts_on == 0:
            warnings.append("全局已开，但没有任何账号开启自动回复")
        if not global_on and accounts_on > 0:
            warnings.append(f"{accounts_on} 个账号已开自动回复，但全局闸门关闭，不会自动发")
        if global_on and not sm_ready:
            warnings.append("SkillManager 未就绪，无法生成回复")
        if circuit_open:
            warnings.append(f"{len(circuit_open)} 个账号处于熔断中")
        if (global_on or accounts_on) and not webhook_on:
            warnings.append("未配置 autoreply_alert webhook，熔断/配额告警不会外推")

        return {
            "ok": True,
            "healthy": len(warnings) == 0,
            "global_enabled": global_on,
            "skill_manager_ready": sm_ready,
            "webhook_alert_configured": webhook_on,
            "accounts": {"auto_reply_on": accounts_on, "protocol": protocol_n},
            "circuit_open": circuit_open,
            "limits": {
                "hourly": lim.hourly, "daily": lim.daily,
                "breaker_threshold": lim.breaker_threshold,
                "breaker_cooldown": lim.breaker_cooldown,
            },
            "stats_24h": stats,
            "warnings": warnings,
            "recent_changes": get_autoreply_audit().recent_config_changes(limit=10),
        }

    @app.get("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_get(request: Request):
        """读有效告警渠道列表（脱敏 token/secret）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask,
        )
        items = effective_webhooks(cfg)
        return {"ok": True, "webhooks": mask(items), "count": len(items)}

    @app.post("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_set(request: Request):
        """整段保存告警渠道列表（白名单校验落盘 + 热更 WebhookNotifier，免重启）。
        token/secret 留空 → 沿用同名旧值（前端展示是脱敏的，避免覆盖真实密钥）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        incoming = body.get("webhooks") if isinstance(body, dict) else body
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask, sanitize_list, save_list,
        )
        # 按 name 保留旧密钥（前端回传脱敏值或空时不覆盖真实 token/secret）
        old_by_name = {w.get("name"): w for w in effective_webhooks(cfg)}
        cleaned = sanitize_list(incoming)
        for w in cleaned:
            old = old_by_name.get(w.get("name")) or {}
            for k in ("token", "secret"):
                nv = str(w.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    w[k] = str(old.get(k) or "")
        saved = save_list(cleaned)
        # 热更运行中的 notifier
        try:
            notifier = getattr(app.state, "webhook_notifier", None)
            if notifier is not None and hasattr(notifier, "reload"):
                notifier.reload(saved)
        except Exception:
            logger.debug("[autoreply] webhook notifier 热更失败", exc_info=True)
        return {"ok": True, "webhooks": mask(saved), "count": len(saved)}

    @app.post("/api/accounts/auto-reply/webhooks/test")
    async def api_account_auto_reply_webhooks_test(request: Request):
        """对单条渠道即时发一条测试告警（连通性检查）。
        body: {index} 走有效列表第 index 条；或直接传 {webhook:{...}}。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, sanitize_webhook,
        )
        items = effective_webhooks(cfg)
        wh: Dict[str, Any] = {}
        if isinstance(body, dict) and body.get("webhook"):
            wh = sanitize_webhook(body.get("webhook"))
            # 测试时若 token 脱敏/空，回退同名已存配置
            old = next((w for w in items if w.get("name") == wh.get("name")), {})
            for k in ("token", "secret"):
                nv = str(wh.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    wh[k] = str(old.get(k) or "")
        else:
            idx = int((body or {}).get("index", -1))
            if 0 <= idx < len(items):
                wh = items[idx]
        if not wh:
            raise HTTPException(400, "未指定有效的 webhook（index 或 webhook）")

        notifier = getattr(app.state, "webhook_notifier", None)
        if notifier is None:
            from src.inbox.webhook_notifier import WebhookNotifier
            notifier = WebhookNotifier(config=[])
        try:
            res = await notifier.send_test(wh)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return res

    @app.get("/api/accounts/auto-reply/stream")
    async def api_account_auto_reply_stream(request: Request):
        """自动回复实时流 SSE：record() 经进程内事件总线即时推送，零轮询零延迟。
        （先订阅再取最新 id 为游标：订阅后的事件必进队列，游标仅用于对订阅/
        取游标竞态窗口内的事件去重，从而不漏不重。）"""
        api_auth(request)
        from starlette.responses import StreamingResponse
        from src.integrations.protocol_autoreply_audit import (
            get_autoreply_audit, subscribe, unsubscribe,
        )
        audit = get_autoreply_audit()
        # 先订阅再取游标：订阅后产生的事件进队列，游标用于补播订阅前的增量并去重
        queue = subscribe()
        seed = audit.recent(limit=1)
        cursor = int(seed[0]["id"]) if seed else 0

        async def _gen():
            import asyncio as _aio
            last_id = cursor
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        row = await _aio.wait_for(queue.get(), timeout=15.0)
                    except _aio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": heartbeat\n\n"
                        continue
                    rid = int(row.get("id") or 0)
                    if rid and rid <= last_id:
                        continue  # 已补播/已推过，去重
                    last_id = rid
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            finally:
                unsubscribe(queue)

        return StreamingResponse(_gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @app.on_event("startup")
    async def _orchestrator_autostart():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if not orchestrator_enabled(cfg):
            return
        try:
            ensure_builtin_workers(cfg)
            await get_orchestrator(cfg).start_loop()
            logger.info("账号池编排器已随启动开启")
        except Exception:
            logger.debug("编排器自启动失败", exc_info=True)

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

        # 操作员打开会话时把较完整历史落库（best-effort，先于 store 读以保证新鲜）
        _ingest_thread_best_effort(request, target, messages)

        cid = _conv_id(platform, account_id, chat_key)
        out_msgs = messages[-limit:]
        # A1 读路径收尾：flag on + store 可用时，会话历史改读持久事实源
        # （跨重启/跨平台、稳定 id 去重）；store 无该会话消息则回落实时聚合结果。
        if _read_from_store_enabled(request):
            stored_msgs = _thread_messages_from_store(request, cid, limit=limit)
            if stored_msgs:
                out_msgs = stored_msgs
            # 实时源已无该会话（未在最近聚合窗口内）但 store 有持久档 → 兜底 header
            if target is None:
                target = _store_conv_as_chat(request, cid)
        # M6①：protocol 账号无实时源（消息由 worker push 落库），线程历史固定读 store
        elif not out_msgs and _is_protocol_account(request, platform, account_id):
            stored_msgs = _thread_messages_from_store(request, cid, limit=limit)
            if stored_msgs:
                out_msgs = stored_msgs
            if target is None:
                target = _store_conv_as_chat(request, cid)

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

    @app.get("/api/workspace/me")
    async def api_workspace_me(request: Request):
        """当前坐席身份 + 角色能力（前端按 is_supervisor 显隐管理向 UI）。"""
        api_auth(request)
        a = _session_agent(request)
        return {"ok": True, "agent_id": a["agent_id"],
                "display_name": a["display_name"], "role": a.get("role", ""),
                "is_supervisor": _is_supervisor(request)}

    @app.get("/api/workspace/escalations")
    async def api_workspace_escalations(request: Request):
        """升级告警源（无人有效处理的严重超时；全局口径，不受个人静默影响）。"""
        api_auth(request)
        return _escalation_snapshot(request)

    @app.get("/api/workspace/escalations/mine")
    async def api_workspace_escalations_mine(
        request: Request, days: int = 7,
    ):
        """我的指派升级列表（当前坐席被指派为责任主管的升级，含接管时延）。
        主管专属；非主管返回空列表（不报 403，前端可安全轮询）。
        """
        api_auth(request)
        if not _is_supervisor(request):
            return {"ok": True, "items": [], "total": 0}
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "items": [], "total": 0}
        agent_id = _session_agent(request)["agent_id"]
        since_ts = time.time() - int(max(1, min(90, days))) * 86400
        items = inbox.list_my_escalations(
            agent_id, since_ts=since_ts, limit=100)
        return {"ok": True, "items": items, "total": len(items)}

    @app.post("/api/workspace/escalation/{esc_id}/assign")
    async def api_workspace_escalation_assign(
        request: Request, esc_id: int,
    ):
        """主管手动将某条升级指派给另一位主管（reassign）。主管专属。
        Body JSON: {"agent_id": "<target_supervisor_agent_id>"}
        """
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            raise HTTPException(503, "inbox 存储不可用")
        body = await request.json()
        target = str(body.get("agent_id") or "").strip()
        if not target:
            raise HTTPException(400, "agent_id 不能为空")
        ok = inbox.set_escalation_assigned(esc_id, target)
        if not ok:
            raise HTTPException(404, f"升级记录 {esc_id} 不存在")
        return {"ok": True, "esc_id": esc_id, "assigned_to": target}

    @app.get("/api/workspace/escalation-log")
    async def api_workspace_escalation_log(request: Request, days: int = 7):
        """升级历史 + 接管时延（复盘安全网成效）：升级→首个人工接管。主管专属。"""
        api_auth(request)
        _require_supervisor(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "days": 7, "items": [], "stats": {}}
        span = 30 if int(days or 7) >= 30 else 7
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        convs = {str(c.get("conversation_id") or ""): c
                 for c in inbox.list_conversations(limit=500)}
        rows = inbox.escalation_takeovers(since, limit=500)
        taken_n = 0
        dly_sum = 0.0
        reasons: Dict[str, int] = {}
        items: List[Dict[str, Any]] = []
        for r in rows:
            c = convs.get(r["conversation_id"]) or {}
            delay = (int(r["taken_ts"] - r["ts"])
                     if r["taken_ts"] is not None else None)
            if delay is not None:
                taken_n += 1
                dly_sum += delay
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
            items.append({
                **r,
                "platform": str(c.get("platform") or ""),
                "name": str(c.get("display_name") or c.get("chat_key")
                            or r["conversation_id"]),
                "takeover_sec": delay,
            })
        total = len(items)
        return {"ok": True, "days": span, "items": items, "stats": {
            "total": total, "taken": taken_n,
            "taken_rate": round(taken_n / total * 100, 1) if total else 0.0,
            "avg_takeover_sec": int(dly_sum / taken_n) if taken_n else 0,
            "reasons": reasons,
        }}

    @app.get("/api/workspace/prefs")
    async def api_workspace_prefs_get(request: Request):
        """当前坐席告警偏好 + 全局默认阈值（供设置面板回显）。"""
        api_auth(request)
        glob = _sla_cfg(request)
        inbox = _inbox_store(request)
        agent = _session_agent(request)
        prefs = (inbox.get_agent_prefs(agent["agent_id"])
                 if inbox is not None else
                 {"warn_sec": 0, "crit_sec": 0, "muted": 0,
                  "dnd_start": -1, "dnd_end": -1})
        return {"ok": True, "prefs": prefs,
                "global_warn_sec": glob["warn"], "global_crit_sec": glob["crit"],
                "effective": _agent_sla_cfg(request)}

    @app.post("/api/workspace/prefs")
    async def api_workspace_prefs_set(request: Request):
        """保存当前坐席告警偏好：{warn_sec,crit_sec,muted,dnd_start,dnd_end}。

        warn_sec/crit_sec=0 表示沿用全局；dnd_start/dnd_end 为本地分钟(0-1439)，
        -1=关闭免打扰。
        """
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": False, "error": "inbox_disabled"}
        body = await request.json()

        def _int(key: str, default: int = 0) -> int:
            try:
                return int(body.get(key, default))
            except (TypeError, ValueError):
                return default

        def _clamp_min(v: int) -> int:
            return v if v == -1 else max(0, min(1439, v))

        agent = _session_agent(request)
        prefs = inbox.set_agent_prefs(
            agent["agent_id"],
            warn_sec=max(0, _int("warn_sec")),
            crit_sec=max(0, _int("crit_sec")),
            muted=1 if body.get("muted") else 0,
            dnd_start=_clamp_min(_int("dnd_start", -1)),
            dnd_end=_clamp_min(_int("dnd_end", -1)),
        )
        return {"ok": True, "prefs": prefs}

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

    @app.post("/api/workspace/sla/create-task")
    async def api_workspace_sla_create_task(request: Request):
        """SLA 超时会话一键生成跟进任务（告警→行动闭环）。

        body: {platform, chat_key, conversation_id?, name?, wait_sec?,
               due_in_hours?(默认2), assignee?(默认本人), note?}
        会话经 (platform, chat_key) 解析 contact，note 预填 SLA 上下文。
        """
        api_auth(request)
        body = await request.json()
        store = _contacts_store(request)
        gw = _contacts_gateway(request)
        if store is None or gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        platform = str(body.get("platform") or "").strip()
        chat_key = str(body.get("chat_key") or "").strip()
        conv = str(body.get("conversation_id") or "").strip()
        if (not platform or not chat_key) and conv:
            parts = conv.split(":")
            if len(parts) >= 3:
                platform = platform or parts[0]
                chat_key = chat_key or ":".join(parts[2:])
        if not platform or not chat_key:
            raise HTTPException(400, "缺少 platform/chat_key 或 conversation_id")
        cmap = store.resolve_contacts_by_external([(platform, chat_key)])
        contact_id = cmap.get((platform, chat_key))
        if not contact_id:
            return {"ok": False, "error": "contact_not_found"}
        try:
            due_in_hours = float(body.get("due_in_hours") or 2)
        except (TypeError, ValueError):
            due_in_hours = 2.0
        due_in_hours = max(0.0, min(24.0 * 30, due_in_hours))
        due_at = int(time.time() + due_in_hours * 3600)
        agent = _session_agent(request)
        assignee = str(body.get("assignee") or "").strip() or agent["agent_id"]
        wait_sec = 0
        try:
            wait_sec = int(body.get("wait_sec") or 0)
        except (TypeError, ValueError):
            wait_sec = 0
        prefix = ("SLA 超时未回复 %d 分钟，请尽快跟进" % (wait_sec // 60)
                  if wait_sec > 0 else "SLA 超时未回复，请尽快跟进")
        extra = str(body.get("note") or "").strip()
        note = prefix + ("；" + extra if extra else "")
        out = gw.add_follow_up_task(
            contact_id, due_at=due_at, note=note,
            assignee=assignee, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("added", contact_id=contact_id,
                               task_id=out.get("task_id") or "", assignee=assignee)
            out["contact_id"] = contact_id
            out["due_at"] = due_at
        return out

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

    def _agent_daily_report_rows(
        request: Request, span: int, agent: str,
    ) -> List[Dict[str, Any]]:
        """某坐席逐日个人绩效：首响数/均值/达标率 + 发送量 + 完成任务数。

        首响按"响应日(resp_ts)"归属（即坐席当日实际动作）；frt=resp_ts-t_in。
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
            k: {"date": k, "first_responded": 0, "frt_avg_sec": 0,
                "frt_attain_rate": 0.0, "sends": 0, "tasks_done": 0}
            for k in days_keys}
        inbox = _inbox_store(request)
        if inbox is not None:
            try:
                fr_acc: Dict[str, List[float]] = {}
                for r in inbox.agent_first_responses(since):
                    if r["resp_ts"] is None or r["agent_id"] != agent:
                        continue
                    day = time.strftime("%Y-%m-%d", time.localtime(r["resp_ts"]))
                    acc = fr_acc.setdefault(day, [0.0, 0.0, 0.0])  # n, sum, attain
                    frt = max(0.0, r["resp_ts"] - r["t_in"])
                    acc[0] += 1
                    acc[1] += frt
                    if frt <= sla["warn"]:
                        acc[2] += 1
                for k, (n, s, att) in fr_acc.items():
                    if k not in rows:
                        continue
                    rows[k]["first_responded"] = int(n)
                    rows[k]["frt_avg_sec"] = int(s / n) if n else 0
                    rows[k]["frt_attain_rate"] = round(att / n * 100, 1) if n else 0.0
                for k, n in inbox.count_agent_sends_by_day(agent, since).items():
                    if k in rows:
                        rows[k]["sends"] = int(n)
            except Exception:
                logger.debug("agent daily-report inbox 统计失败（已忽略）", exc_info=True)
        store = _contacts_store(request)
        if store is not None:
            try:
                for k, n in store.count_tasks_done_by_day(agent, since).items():
                    if k in rows:
                        rows[k]["tasks_done"] = int(n)
            except Exception:
                logger.debug("agent daily-report tasks 统计失败（已忽略）", exc_info=True)
        return [rows[k] for k in days_keys]

    @app.get("/api/workspace/daily-report.csv")
    async def api_workspace_daily_report(
        request: Request, days: int = 7, agent: str = "",
    ):
        """逐日经营日报 CSV（历史回看）：days=7/30，每行一天，含汇总行。

        传 agent → 该坐席个人绩效日报（首响/发送量/完成任务）。
        """
        api_auth(request)
        span = 30 if int(days or 7) >= 30 else 7
        agent = str(agent or "").strip()
        # 团队日报(无 agent)或他人个人日报 → 主管专属；本人个人日报放行
        if not agent or agent != _session_agent(request)["agent_id"]:
            _require_supervisor(request)
        if agent:
            import csv
            import io
            data = _agent_daily_report_rows(request, span, agent)
            buf = io.StringIO()
            buf.write("\ufeff")
            w = csv.writer(buf)
            w.writerow(["date", "first_responded", "frt_avg_sec",
                        "frt_attain_rate_pct", "sends", "tasks_done"])
            tot = {"fr": 0, "sends": 0, "tasks": 0, "frt_sum": 0, "attain": 0}
            for r in data:
                w.writerow([r["date"], r["first_responded"], r["frt_avg_sec"],
                            r["frt_attain_rate"], r["sends"], r["tasks_done"]])
                tot["fr"] += r["first_responded"]
                tot["sends"] += r["sends"]
                tot["tasks"] += r["tasks_done"]
                tot["frt_sum"] += r["frt_avg_sec"] * r["first_responded"]
                tot["attain"] += round(r["frt_attain_rate"] / 100 * r["first_responded"])
            frt_avg = int(tot["frt_sum"] / tot["fr"]) if tot["fr"] else 0
            attain = round(tot["attain"] / tot["fr"] * 100, 1) if tot["fr"] else 0.0
            w.writerow(["合计", tot["fr"], frt_avg, attain, tot["sends"], tot["tasks"]])
            fname = "agent-report-%s-%dd-%s.csv" % (
                agent, span, time.strftime("%Y%m%d", time.localtime()))
            return Response(
                content=buf.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=" + fname},
            )
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

    @app.get("/api/workspace/tag-stats")
    async def api_workspace_tag_stats(request: Request):
        """T2：会话级标签统计（count / unread / platforms），用于概览 strip。"""
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": True, "stats": []}
        try:
            stats = inbox.tag_stats()
        except Exception:
            logger.debug("tag-stats 失败（已忽略）", exc_info=True)
            stats = []
        return {"ok": True, "stats": stats}

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

    # ── T1: 会话级标签 + 归档 API ─────────────────────────────────────

    @app.post("/api/workspace/conv/{conversation_id}/summarize")
    async def api_conv_summarize(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """Phase 19：为会话生成 AI 摘要并持久化到 conversation_meta.summary。

        调用 ChatAssistantService.analyze（与 inbox/analyze 同服务），
        以会话最近30条消息作为上下文，生成一句话概括。结果写库后返回。
        """
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        msgs = store.list_recent_messages(conversation_id, limit=30)
        if not msgs:
            return {"ok": True, "summary": ""}
        msg_objs = [store_message_to_obj(r) for r in msgs]
        # 取最后一条入站文字为代表性文本
        last_in = next((m for m in reversed(msg_objs) if m.get("direction") == "in"
                        and m.get("text")), None)
        text = str((last_in or msg_objs[-1]).get("text") or "")
        try:
            svc = _get_chat_assistant_service(request)
            analysis = await svc.analyze(text=text, messages=msg_objs)
            summary = str(getattr(analysis, "summary", "") or "").strip()
            if not summary:
                # Fallback: truncate last user message as summary
                summary = text[:80] + ("…" if len(text) > 80 else "")
        except Exception:
            logger.debug("conv summarize AI 调用失败（已忽略）", exc_info=True)
            summary = text[:80] + ("…" if len(text) > 80 else "")
        store.save_conv_summary(conversation_id, summary)
        return {"ok": True, "summary": summary}

    @app.get("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_get(conversation_id: str, request: Request):
        """T1：获取单个会话的标签列表。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "tags": []}
        return {"ok": True, "tags": store.get_conv_tags(conversation_id)}

    @app.put("/api/workspace/conv/{conversation_id}/tags")
    async def api_conv_tags_put(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：覆写会话标签列表，广播 conv_tagged 事件供 Webhook 外发。"""
        body = await request.json()
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        ok = store.set_conv_tags(conversation_id, tags)
        # P28：广播标签变更事件
        if ok:
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_tagged", {
                    "conversation_id": conversation_id,
                    "tags": tags,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "tags": tags}

    @app.patch("/api/workspace/conv/{conversation_id}/archive")
    async def api_conv_archive(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """T1+P28：归档/取消归档会话，并广播 conv_archived 事件供 Webhook 外发。"""
        body = await request.json()
        archived = bool(body.get("archived", True))
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        ok = store.set_conv_archived(conversation_id, archived)
        if ok:
            # P34：归档时自动触发 QA 评分计算（异步非阻塞）
            if archived:
                try:
                    import asyncio as _aio
                    _aio.get_event_loop().run_in_executor(
                        None, store.compute_and_store_qa_score, conversation_id
                    )
                except Exception:
                    pass
            # P28：广播会话归档事件（修正 EventBus API 调用签名）
            try:
                from src.integrations.shared.event_bus import get_event_bus
                import time as _t
                get_event_bus().publish("conv_archived", {
                    "conversation_id": conversation_id,
                    "archived": archived,
                    "ts": _t.time(),
                })
            except Exception:
                pass
        return {"ok": ok, "archived": archived}

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

    @app.get("/workspace/escalations", response_class=HTMLResponse)
    async def workspace_escalations_page(request: Request, _=Depends(page_auth)):
        if not _is_supervisor(request):
            from starlette.responses import RedirectResponse
            return RedirectResponse("/workspace/dash", status_code=307)
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
        return templates.TemplateResponse(request, "escalation_log.html", ctx)

    @app.get("/api/unified-inbox/templates")
    async def api_unified_inbox_templates(request: Request):
        """快捷回复模板（workspace + messenger approval + templates.yaml）。"""
        api_auth(request)
        tpls = _collect_quick_templates(config_manager)
        return {"ok": True, "templates": tpls, "count": len(tpls)}

    @app.get("/api/unified-inbox/search-messages")
    async def api_unified_inbox_search_messages(
        request: Request,
        q: str = "",
        limit: int = 20,
        platform: str = "",
    ):
        """Phase 21：跨会话消息全文检索（SQLite LIKE），供坐席工作台搜索消息内容。

        返回：[{message_id, conversation_id, text, ts, direction, platform, display_name}]
        """
        api_auth(request)
        query = str(q or "").strip()
        if not query or len(query) < 2:
            return {"ok": True, "results": [], "q": query}
        limit = max(1, min(50, int(limit or 20)))
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "results": [], "error": "inbox_store 不可用"}
        try:
            hits = store.search_messages(query, limit=limit, platform=str(platform or ""))
        except Exception:
            logger.debug("search_messages 失败", exc_info=True)
            return {"ok": False, "results": [], "error": "search_failed"}
        # 构造归一化格式：同时补充 conversation_id 对应的 conv_id（供前端导航）
        results = []
        for r in hits:
            cid = str(r.get("conversation_id") or "")
            txt = str(r.get("text") or "")
            results.append({
                "message_id": str(r.get("message_id") or ""),
                "conversation_id": cid,
                "text": txt,
                "text_snippet": txt[:120] + ("…" if len(txt) > 120 else ""),
                "ts": r.get("ts") or 0,
                "direction": r.get("direction") or "in",
                "platform": str(r.get("platform") or ""),
                "display_name": str(r.get("display_name") or ""),
            })
        return {"ok": True, "results": results, "q": query, "count": len(results)}

    @app.get("/api/unified-inbox/kb-search")
    async def api_unified_inbox_kb_search(
        request: Request,
        q: str = "",
        limit: int = 5,
        platform: str = "",
        intent: str = "",
        auto: str = "",
    ):
        """KB 内联检索：坐席在工作台快速查话术/知识条目。

        新增参数（Phase 17）：
          platform  — 当前会话平台，用于 platform 字段加权
          intent    — 当前会话意图（AI 分析结果），用于 category/keyword 加权
          auto=1    — 自动触发模式，limit 降为 3，只返回高置信条目
        """
        api_auth(request)
        query = str(q or "").strip()
        is_auto = str(auto or "").lower() in ("1", "true", "yes")
        if is_auto:
            limit = max(1, min(4, int(limit or 3)))
        else:
            limit = max(1, min(10, int(limit or 5)))
        kb = getattr(request.app.state, "kb_store", None)
        if kb is None:
            return {"ok": False, "entries": [], "error": "kb_unavailable"}
        if not query:
            return {"ok": True, "entries": [], "search_mode": "none"}
        fetch_k = min(limit * 3, 20)  # 先多取，后重排
        try:
            result = kb.search(query, top_k=fetch_k)
        except Exception:
            logger.debug("kb-search 失败", exc_info=True)
            return {"ok": False, "entries": [], "error": "search_failed"}
        raw_entries: List[Dict[str, Any]] = result.get("entries") or []

        # Phase 17: context re-ranking
        # 规则：平台匹配 +0.15，意图关键词命中 +0.10，有 example_reply_zh +0.05
        plat_ctx = str(platform or "").lower()
        intent_ctx = str(intent or "").lower()
        scored: List[tuple] = []
        for row in raw_entries:
            base = float(row.get("_score") or 0.5)
            boost = 0.0
            row_plat = str(row.get("platform") or "").lower()
            if row_plat and plat_ctx and row_plat == plat_ctx:
                boost += 0.15
            row_kws = " ".join([
                str(row.get("category") or ""),
                str(row.get("keywords") or ""),
                str(row.get("scenario") or ""),
                str(row.get("title") or ""),
            ]).lower()
            if intent_ctx and intent_ctx in row_kws:
                boost += 0.10
            if row.get("example_reply_zh"):
                boost += 0.05
            scored.append((base + boost, row))

        scored.sort(key=lambda x: -x[0])
        entries: List[Dict[str, Any]] = []
        for score, row in scored[:limit]:
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
                "score": round(score, 3),
                "search_mode": row.get("_mode") or result.get("search_mode"),
                "auto": is_auto,
            })
        return {
            "ok": True,
            "entries": entries,
            "search_mode": result.get("search_mode") or "bm25",
            "context_reranked": bool(plat_ctx or intent_ctx),
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

    @app.post("/api/desktop/smart-reply")
    async def api_desktop_smart_reply(request: Request, _=Depends(api_auth)):
        """桌面壳（嵌官方 web 客户端）专用：**人设化**智能回复。

        走与 ``/api/chat/test`` 同一条产线（SkillManager → 意图 → 策略 → KB →
        ``AIClient.generate_reply_with_intent``），由 PersonaManager 注入后台人设
        （domain ``conversion`` 的「线上陪伴」或 account_persona_id 指定的画像），
        因此回复带人设口吻、禁用「作为AI」等机器措辞、并融合知识库——而非通用提示词。

        body: {messages:[{direction,text}], persona_id?, platform?, chat_key?, target_lang?}
        返回: {ok, reply, persona?, intent?, translated?}
        """
        body = await request.json()
        msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
        target_lang = str(body.get("target_lang") or "").strip()
        persona_id = str(body.get("persona_id") or "").strip()
        platform = str(body.get("platform") or "telegram").strip()
        chat_key = str(body.get("chat_key") or "").strip()

        # 归一对话历史（OpenAI 风格）+ 取最后一条入站消息作为「待回复」
        history: List[Dict[str, str]] = []
        last_inbound = ""
        for m in msgs:
            if not isinstance(m, dict):
                continue
            t = str(m.get("text") or "").strip()
            if not t:
                continue
            is_in = m.get("direction") in ("in", "inbound")
            history.append({"role": "user" if is_in else "assistant", "content": t})
            if is_in:
                last_inbound = t
        if not last_inbound:
            last_inbound = history[-1]["content"] if history else ""
        if not last_inbound:
            return {"ok": False, "detail": "无可用对话上下文"}

        sm = getattr(request.app.state, "skill_manager", None)
        if sm is None:
            _tc = getattr(request.app.state, "telegram_client", None)
            sm = getattr(_tc, "skill_manager", None) if _tc is not None else None
        ai = getattr(request.app.state, "ai_client", None)

        reply = None
        used_persona = ""
        used_intent = ""
        # 主路径：人设 + KB + 策略（与 /api/chat/test 一致）
        if sm is not None and getattr(sm, "ai_client", None) is not None:
            try:
                user_id = f"desktop:{platform}:{chat_key}" or "__desktop__"
                intent = sm._recognize_intent(last_inbound)
                used_intent = intent
                try:
                    strategy, _sid = sm.get_strategy_for_intent(intent, user_id)
                except Exception:
                    strategy = {}
                kb_context = ""
                kb = getattr(request.app.state, "kb_store", None)
                if kb is not None:
                    try:
                        _res = kb.search(last_inbound, top_k=3, lang="zh")
                        kb_context = kb.build_ai_context_from_result(_res, lang="zh")
                    except Exception:
                        kb_context = ""
                ctx: Dict[str, Any] = {
                    "user_id": user_id,
                    "chat_id": chat_key or user_id,
                    "channel": "desktop",
                    "platform": platform,
                    "intent": intent,
                    "current_intent": intent,
                    "_reply_strategy": strategy or {},
                    "reply_lang": target_lang or "zh",
                }
                if persona_id:
                    ctx["account_persona_id"] = persona_id
                if len(history) > 1:
                    hist = history[:-1] if history[-1]["role"] == "user" else history
                    ctx["_conversation_history"] = hist[-20:]
                if kb_context:
                    ctx["kb_context"] = kb_context
                so: Dict[str, Any] = {}
                for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                    if _sk in (strategy or {}):
                        so[_sk] = strategy[_sk]
                reply = await sm.ai_client.generate_reply_with_intent(
                    user_message=last_inbound,
                    intent=intent,
                    user_context=ctx,
                    strategy_overrides=so or None,
                )
                used_persona = persona_id or "domain"
            except Exception:
                logger.debug("[desktop] 人设 smart-reply 失败，回落通用", exc_info=True)
                reply = None

        # 兜底：SkillManager 不可用时退回通用提示词（保证至少有草稿）
        if not reply and ai is not None:
            lines = [
                ("客户：" if m.get("direction") in ("in", "inbound") else "我：") + str(m.get("text") or "")
                for m in msgs[-12:] if isinstance(m, dict) and str(m.get("text") or "").strip()
            ]
            prompt = (
                "你是温暖、自然、像真人一样的线上陪伴/客服。基于以下对话，草拟我的下一条回复。"
                "口吻自然口语化，禁止出现「作为AI/作为一个AI/有什么可以帮您」等机器措辞，"
                "只输出回复正文。\n\n对话：\n" + "\n".join(lines) + "\n\n我的回复："
            )
            try:
                reply = await ai.chat(prompt)
            except Exception:
                logger.debug("[desktop] 兜底 smart-reply 失败", exc_info=True)
                reply = None

        reply = (reply or "").strip()
        # P1：让徽标说真话——返回「实际解析到的人设」而非「请求的 id」。
        # 会话绑定/账号人设/domain 谁生效就报谁；解析失败回落到旧值。
        persona_tier = ""
        if reply:
            try:
                from src.utils.persona_manager import PersonaManager
                _pm = PersonaManager.get_instance()
                _resolved, _tier = _pm.get_persona_with_tier(chat_key or "", persona_id)
                persona_tier = _tier
                # 部分 profile 字典内无 'id' 字段（id 只是 YAML key），故按 tier 推导：
                # account_profile 层 → 用请求的 persona_id；chat_binding → 用解析到的 id；
                # domain/default → "domain"。让徽标与「实际生效」一致。
                _rid = str((_resolved or {}).get("id") or "")
                if _tier == "account_profile":
                    used_persona = _rid or persona_id or "domain"
                elif _tier == "chat_binding":
                    used_persona = _rid or "domain"
                else:
                    used_persona = "domain"
            except Exception:
                logger.debug("[desktop] persona tier 解析失败", exc_info=True)
        out: Dict[str, Any] = {"ok": bool(reply), "reply": reply,
                               "persona": used_persona, "persona_tier": persona_tier,
                               "intent": used_intent}
        if reply and target_lang:
            try:
                svc = _get_translation_service(request)
                res = await svc.translate(reply, target_lang=target_lang, style="chat")
                if res.ok:
                    _rd = res.to_dict()
                    out["translated"] = _rd.get("translated_text") or _rd.get("text") or ""
            except Exception:
                logger.debug("[desktop] smart-reply 译文失败", exc_info=True)
        return out

    @app.post("/api/desktop/guard-check")
    async def api_desktop_guard_check(request: Request, _=Depends(api_auth)):
        """桌面壳「填入并发送」前风控护栏（规则层，零 LLM 成本，毫秒级）。

        复用 ``src.inbox.drafts.keyword_risk_level``（支付/密码/账号安全=high→拦截；
        优惠/投诉/法律=medium→提醒），并检测「作为AI」等机器措辞（可能露馅）。
        body: {text}
        返回: {ok, risk: high|medium|low, block, hits:[{term,level}], robotic:[...]}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text") or "")
        risk = "low"
        hits: List[Dict[str, str]] = []
        robotic: List[str] = []
        try:
            from src.inbox.drafts import keyword_risk_level, _SENSITIVE_PATTERNS
            risk = keyword_risk_level(text) or "low"
            for pattern, level in _SENSITIVE_PATTERNS:
                m = pattern.search(text)
                if m:
                    hits.append({"term": m.group(0), "level": level})
        except Exception:
            logger.debug("[desktop] guard-check 规则层失败", exc_info=True)
        _ROBOTIC = [
            "作为AI", "作为一个AI", "作为人工智能", "我是语言模型", "我是机器人",
            "有什么可以帮您", "很高兴为您服务", "请问有什么可以帮",
        ]
        for ph in _ROBOTIC:
            if ph in text:
                robotic.append(ph)
        return {"ok": True, "risk": risk, "block": risk == "high",
                "hits": hits, "robotic": robotic}

    @app.post("/api/desktop/ingest")
    async def api_desktop_ingest(request: Request, _=Depends(api_auth)):
        """桌面壳同步桥（P1）：把官方 web 客户端 DOM 抓到的消息回流统一收件箱。

        与 ``/api/internal/protocol/ingest``（Baileys 等真 worker）不同：桌面账号无服务端
        worker、不被编排器接管，故首次同步即把账号以 ``mode="desktop"`` 落 registry——
        让收件箱列表（ProtocolInboxAdapter）与线程（_is_protocol_account）按 store 读出，
        而 ``worker_supported`` 不含 desktop 模式，编排器自动跳过、不会尝试拉起 worker。
        body: {platform, account_id, chat_key, name?, text?, ts?, msg_id?, direction?,
               media_type?, media_ref?}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, "inbox store 未就绪")
        platform = str((body or {}).get("platform") or "").lower()
        account_id = str((body or {}).get("account_id") or "")
        chat_key = str((body or {}).get("chat_key") or "")
        if not platform or not account_id:
            raise HTTPException(400, "platform / account_id 不能为空")
        if not chat_key:
            raise HTTPException(400, "chat_key 不能为空")
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
            row = reg.get(platform, account_id)
            if not row:
                reg.upsert(platform, account_id, mode="desktop",
                           label=str((body or {}).get("name") or account_id),
                           status="online")
        except Exception:
            logger.debug("[desktop] registry upsert 失败（已忽略）", exc_info=True)
        from src.integrations.protocol_bridge import ingest_incoming
        cid = ingest_incoming(
            store,
            platform=platform,
            account_id=account_id,
            chat_key=chat_key,
            name=str((body or {}).get("name") or ""),
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=str((body or {}).get("direction") or "in"),
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
        )
        return {"ok": bool(cid), "conversation_id": cid or ""}

    @app.get("/api/unified-inbox/translation-engines")
    async def api_unified_inbox_translation_engines(
        request: Request, target_lang: str = "zh", _=Depends(api_auth)
    ):
        """指定目标语的引擎能力矩阵：让坐席在切换目标语时即知主引擎是否兜底。"""
        svc = _get_translation_service(request)
        return {"ok": True, "matrix": svc.engine_matrix(target_lang)}

    @app.post("/api/unified-inbox/translate-image")
    async def api_unified_inbox_translate_image(request: Request, _=Depends(api_auth)):
        """P58：图片 OCR → 翻译。前端传 base64 图片，返回逐字 OCR 文本 + 译文。

        body: {image_b64, target_lang?, source_lang?, style?}
        复用 vision 栈（Ollama→智谱），无可用后端时返回明确提示。
        """
        import os as _os

        from src.ai.image_translate import (
            ImageTranslateService,
            build_vision_ocr_fn,
            decode_image_to_temp,
        )

        body = await request.json()
        image_b64 = str(body.get("image_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        vision_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            vision_cfg = dict(full.get("vision") or {})
        except Exception:
            vision_cfg = {}
        if not vision_cfg.get("enabled", False):
            return {"ok": False, "reason": "vision_disabled",
                    "message": "图像识别未启用（config.vision.enabled）"}

        try:
            from src.vision_client import has_any_vision_backend
            if not has_any_vision_backend(vision_cfg, vision_cfg):
                return {"ok": False, "reason": "no_vision_backend",
                        "message": "未配置可用的图像识别后端（Ollama base_url 或智谱 api_key）"}
        except Exception:
            pass

        path, reason = decode_image_to_temp(image_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"图片无效：{reason}"}
        try:
            svc = ImageTranslateService(
                _get_translation_service(request),
                build_vision_ocr_fn(vision_cfg, vision_cfg),
            )
            return await svc.translate_image(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    @app.post("/api/unified-inbox/translate-voice")
    async def api_unified_inbox_translate_voice(request: Request, _=Depends(api_auth)):
        """P58-2：语音转写(ASR) → 翻译。前端传 base64 音频，返回转写文本 + 译文。

        body: {audio_b64, target_lang?, source_lang?, style?}
        复用 AudioPipeline（faster-whisper/在线 ASR）。未启用/无后端返回明确提示。
        """
        import os as _os

        from src.ai.voice_translate import (
            VoiceTranslateService,
            build_audio_transcribe_fn,
            decode_audio_to_temp,
        )

        body = await request.json()
        audio_b64 = str(body.get("audio_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        audio_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            audio_cfg = dict(full.get("audio_pipeline") or {})
        except Exception:
            audio_cfg = {}
        if not audio_cfg.get("enabled", False):
            return {"ok": False, "reason": "asr_disabled",
                    "message": "语音转写未启用（config.audio_pipeline.enabled）"}

        path, reason = decode_audio_to_temp(audio_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"音频无效：{reason}"}
        try:
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            return await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    def _media_base_dirs(request: Request) -> list:
        """媒体解析白名单根目录（config.media.base_dirs）。仅在白名单内的文件可被读取。"""
        cm = getattr(request.app.state, "config_manager", None)
        try:
            full = getattr(cm, "config", None) or {}
            dirs = list((full.get("media") or {}).get("base_dirs") or [])
        except Exception:
            dirs = []
        return [str(d) for d in dirs if str(d or "").strip()]

    def _remote_fetch_cfg(request: Request) -> dict:
        """config.media.remote_fetch（受控远程媒体下载，默认关）。"""
        cm = getattr(request.app.state, "config_manager", None)
        try:
            full = getattr(cm, "config", None) or {}
            return dict((full.get("media") or {}).get("remote_fetch") or {})
        except Exception:
            return {}

    def _within_base_dirs(path: str, base_dirs: list) -> bool:
        """容纳检查：resolved 真实路径必须落在某个白名单根内（防路径穿越）。
        未配置白名单时放行（media_ref 来自我方 store/平台，非终端用户输入）。"""
        if not base_dirs:
            return True
        try:
            rp = os.path.realpath(path)
            for b in base_dirs:
                br = os.path.realpath(str(b))
                if rp == br or rp.startswith(br + os.sep):
                    return True
        except Exception:
            return False
        return False

    def _lookup_stored_media(request: Request, conversation_id: str, message_id: str):
        """从 store 按 message_id 取该消息的 (media_type, media_ref)。取不到返回 ('','')。"""
        store = _inbox_store(request)
        if store is None or not conversation_id:
            return "", ""
        try:
            rows = store.list_messages(conversation_id, limit=500)
        except Exception:
            return "", ""
        mid = str(message_id or "")
        for r in rows:
            if mid and str(r.get("platform_msg_id") or "") == mid:
                return str(r.get("media_type") or ""), str(r.get("media_ref") or "")
        return "", ""

    @app.post("/api/unified-inbox/translate-message-media")
    async def api_unified_inbox_translate_message_media(request: Request, _=Depends(api_auth)):
        """P61-2：会话内媒体一键翻译（可解析则免上传）。

        body: {conversation_id, message_id, media_ref?, media_type?, target_lang?, source_lang?, style?}
        优先从 store 按 message_id 取受信 media_ref；解析到本进程可读本地文件 →
        直接复用 ImageTranslateService/VoiceTranslateService（免上传/免 base64 往返）。
        不可解析（远程/找不到/无引用/不支持）→ 返回 reason，前端回落到上传组件。
        """
        from src.inbox.media_resolver import resolve_for_translate

        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        # 受信优先：先查 store（平台落库的 media_ref），回落 body 携带值
        media_type, media_ref = _lookup_stored_media(request, conversation_id, message_id)
        if not media_ref:
            media_ref = str(body.get("media_ref") or "")
            media_type = media_type or str(body.get("media_type") or "")

        base_dirs = _media_base_dirs(request)
        # M6⑤：protocol 媒体以 /static URL 落库 → 映射回本地文件，并把其根纳入白名单
        # （仅对 protocol 媒体扩展 base_dirs，不影响其它平台的容纳检查行为）
        try:
            from src.integrations.protocol_bridge import (
                protocol_media_root, static_media_ref_to_path,
            )
            _local = static_media_ref_to_path(media_ref)
            if _local:
                media_ref = _local
                base_dirs = base_dirs + [str(protocol_media_root())]
        except Exception:
            logger.debug("protocol 媒体路径映射失败", exc_info=True)

        message = {"media_type": media_type, "media_ref": media_ref}
        path, kind, reason = resolve_for_translate(message, base_dirs=base_dirs)

        # C-2：远程媒体受控下载（默认关；SSRF 防护 + 大小/超时 + 域名白名单）。
        # 仅当解析判定为 remote_unsupported（此时 kind 已校验为 image/voice）时尝试。
        _tmp_download: Optional[str] = None
        if reason == "remote_unsupported":
            _rf = _remote_fetch_cfg(request)
            if _rf.get("enabled", False):
                from src.inbox.media_fetch import fetch_remote_media
                _dl_path, _dl_reason = await fetch_remote_media(
                    media_ref,
                    kind=kind,
                    max_bytes=int(_rf.get("max_mb", 10) or 10) * 1024 * 1024,
                    timeout_sec=float(_rf.get("timeout_sec", 8) or 8),
                    allow_domains=list(_rf.get("allow_domains") or []),
                )
                if _dl_path:
                    path, reason, _tmp_download = _dl_path, "ok", _dl_path
                else:
                    return {"ok": False, "reason": _dl_reason, "fallback": "upload",
                            "message": "远程媒体下载失败，请上传文件"}

        if reason != "ok":
            msg = {
                "no_ref": "该消息无媒体引用",
                "remote_unsupported": "媒体为远程链接，暂不支持免上传翻译，请上传文件",
                "not_found": "未找到本地媒体文件，请上传文件",
                "unsupported_kind": "暂不支持该媒体类型翻译",
            }.get(reason, reason)
            return {"ok": False, "reason": reason, "fallback": "upload", "message": msg}

        # 下载得到的临时文件是我方可信路径，不参与 base_dirs 容纳检查（仅对平台落库路径校验）。
        if _tmp_download is None and not _within_base_dirs(path, base_dirs):
            return {"ok": False, "reason": "outside_base_dirs", "fallback": "upload",
                    "message": "媒体文件不在允许目录内"}

        try:
            if kind == "image":
                from src.ai.image_translate import ImageTranslateService, build_vision_ocr_fn
                cm = getattr(request.app.state, "config_manager", None)
                try:
                    vision_cfg = dict((getattr(cm, "config", None) or {}).get("vision") or {})
                except Exception:
                    vision_cfg = {}
                if not vision_cfg.get("enabled", False):
                    return {"ok": False, "reason": "vision_disabled",
                            "message": "图像识别未启用（config.vision.enabled）"}
                try:
                    from src.vision_client import has_any_vision_backend
                    if not has_any_vision_backend(vision_cfg, vision_cfg):
                        return {"ok": False, "reason": "no_vision_backend",
                                "message": "未配置可用的图像识别后端"}
                except Exception:
                    pass
                svc = ImageTranslateService(
                    _get_translation_service(request),
                    build_vision_ocr_fn(vision_cfg, vision_cfg),
                )
                out = await svc.translate_image(
                    path, target_lang=target_lang, source_lang=source_lang, style=style,
                )
                out["media_kind"] = "image"
                out["from_upload"] = False
                out["from_remote"] = _tmp_download is not None
                return out

            # kind == "voice"
            from src.ai.voice_translate import VoiceTranslateService, build_audio_transcribe_fn
            cm = getattr(request.app.state, "config_manager", None)
            try:
                audio_cfg = dict((getattr(cm, "config", None) or {}).get("audio_pipeline") or {})
            except Exception:
                audio_cfg = {}
            if not audio_cfg.get("enabled", False):
                return {"ok": False, "reason": "asr_disabled",
                        "message": "语音转写未启用（config.audio_pipeline.enabled）"}
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            out = await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
            out["media_kind"] = "voice"
            out["from_upload"] = False
            out["from_remote"] = _tmp_download is not None
            return out
        finally:
            # 清理下载的临时文件（无论成功/异常）
            if _tmp_download:
                try:
                    os.unlink(_tmp_download)
                except Exception:
                    pass

    @app.post("/api/unified-inbox/mark-conversion")
    async def api_unified_inbox_mark_conversion(request: Request, _=Depends(api_auth)):
        """阶段 E：人工标记会话所属客户为 成交(BONDED)/已转化(CONVERTED)。

        修复"空心漏斗"：此前 gateway 自动流转最高只到 LINE_ENGAGED，BONDED/CONVERTED
        作为终点存在却无任何代码路径写入 → 转化漏斗终点 KPI 永远为 0、不可达。
        本端点让坐席可手动闭环成交，FSM 守卫非法转移、落 stage_change event（记录操作人）。

        body: {conversation_id, stage?(BONDED|CONVERTED), contact_id?, note?}
        需启用 contacts 子系统（config.contacts.enabled）。
        """
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        target = str(body.get("stage") or "BONDED").strip().upper()
        note = str(body.get("note") or "")
        if target not in ("BONDED", "CONVERTED"):
            return {"ok": False, "reason": "bad_stage",
                    "message": "stage 仅支持 BONDED(成交) 或 CONVERTED(已转化)"}

        cstore = _contacts_store(request)
        if cstore is None:
            return {"ok": False, "reason": "contacts_disabled",
                    "message": "客户旅程子系统未启用（config.contacts.enabled）"}

        # 解析 journey：优先 body.contact_id，其次会话 meta 关联的 contact_id
        contact_id = str(body.get("contact_id") or "")
        if not contact_id and conversation_id:
            istore = _inbox_store(request)
            try:
                meta = istore.get_conv_meta(conversation_id) if istore else {}
                contact_id = str((meta or {}).get("contact_id") or "")
            except Exception:
                contact_id = ""
        if not contact_id:
            return {"ok": False, "reason": "no_contact",
                    "message": "该会话尚未关联客户，无法标记成交"}

        try:
            journey = cstore.get_journey_by_contact(contact_id)
        except Exception:
            journey = None
        if journey is None:
            return {"ok": False, "reason": "no_journey",
                    "message": "未找到该客户的旅程记录"}

        try:
            agent_id, _agent_name = _agent_from_request(request)
        except Exception:
            agent_id = ""
        from src.contacts.journey_fsm import transit as _fsm_transit_fn
        ok = _fsm_transit_fn(
            cstore, journey_id=journey.journey_id, to_stage=target,
            payload={"manual": True, "by": agent_id or "agent", "note": note},
        )
        if not ok:
            try:
                cur = cstore.get_journey(journey.journey_id)
                cur_stage = cur.funnel_stage if cur else journey.funnel_stage
            except Exception:
                cur_stage = journey.funnel_stage
            return {
                "ok": False, "reason": "transition_blocked",
                "current_stage": cur_stage,
                "current_stage_label": FUNNEL_STAGE_LABELS.get(cur_stage, cur_stage),
                "message": f"不能从「{FUNNEL_STAGE_LABELS.get(cur_stage, cur_stage)}」"
                           f"直接标记为「{FUNNEL_STAGE_LABELS.get(target, target)}」",
            }
        try:
            j2 = cstore.get_journey(journey.journey_id)
            new_stage = j2.funnel_stage if j2 else target
        except Exception:
            new_stage = target
        return {
            "ok": True,
            "funnel_stage": new_stage,
            "funnel_stage_label": FUNNEL_STAGE_LABELS.get(new_stage, new_stage),
        }

    @app.post("/api/unified-inbox/outreach/preview")
    async def api_unified_inbox_outreach_preview(request: Request, _=Depends(api_auth)):
        """P61-3：分组批量触达 dry-run 预览（只读不发）。

        body: {platform?, tags_any?[], rel_stages?[], min_silent_days?, max_silent_days?,
               exclude_archived?, limit?}
        返回命中人数、可触达名单、跳过原因（cooldown/account_cap）、每账号分布、预计耗时。
        """
        from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner

        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store", "message": "持久层未挂载"}

        body = await request.json()
        cm = getattr(request.app.state, "config_manager", None)
        try:
            ocfg = dict((getattr(cm, "config", None) or {}).get("outreach") or {})
        except Exception:
            ocfg = {}

        filters = OutreachFilters(
            platform=str(body.get("platform") or ""),
            tags_any=[str(t) for t in (body.get("tags_any") or []) if str(t).strip()],
            rel_stages=[str(s) for s in (body.get("rel_stages") or []) if str(s).strip()],
            min_silent_days=float(body.get("min_silent_days") or 0),
            max_silent_days=float(body.get("max_silent_days") or 0),
            exclude_archived=bool(body.get("exclude_archived", True)),
            limit=int(body.get("limit") or 500),
        )
        planner = OutreachPlanner(
            store,
            limiter=getattr(request.app.state, "account_limiter", None),
            cooldown_days=float(ocfg.get("cooldown_days", 14)),
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            default_account_cap=int(ocfg.get("default_account_cap", 30)),
        )
        plan = planner.build_plan(filters)
        out = plan.to_dict()
        out["ok"] = True
        return out

    @app.post("/api/unified-inbox/outreach/execute")
    async def api_unified_inbox_outreach_execute(request: Request, _=Depends(api_auth)):
        """P61-4：分组批量触达执行（真实发送）。需 feature-flag + 二次确认。

        body: {filters{}, template, confirm:true, max_send?, batch_id?}
        服务端按 filters 重建 plan（不信任客户端名单）→ 真实扣配额 → RPA 发送 →
        落回执。受 config.outreach.enabled 门禁与 config.outreach.max_batch 硬上限保护。
        """
        from src.inbox.outreach_executor import OutreachExecutor
        from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner

        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store", "message": "持久层未挂载"}

        cm = getattr(request.app.state, "config_manager", None)
        try:
            ocfg = dict((getattr(cm, "config", None) or {}).get("outreach") or {})
        except Exception:
            ocfg = {}
        if not ocfg.get("enabled", False):
            return {"ok": False, "reason": "outreach_disabled",
                    "message": "批量触达未启用（config.outreach.enabled）"}

        body = await request.json()
        if body.get("confirm") is not True:
            return {"ok": False, "reason": "confirm_required",
                    "message": "执行批量发送需显式 confirm=true"}
        template = str(body.get("template") or "").strip()
        if not template:
            return {"ok": False, "reason": "empty_template", "message": "消息模板不能为空"}

        fb = body.get("filters") or {}
        filters = OutreachFilters(
            platform=str(fb.get("platform") or ""),
            tags_any=[str(t) for t in (fb.get("tags_any") or []) if str(t).strip()],
            rel_stages=[str(s) for s in (fb.get("rel_stages") or []) if str(s).strip()],
            min_silent_days=float(fb.get("min_silent_days") or 0),
            max_silent_days=float(fb.get("max_silent_days") or 0),
            exclude_archived=bool(fb.get("exclude_archived", True)),
            limit=int(fb.get("limit") or 500),
        )
        limiter = getattr(request.app.state, "account_limiter", None)
        planner = OutreachPlanner(
            store, limiter=limiter,
            cooldown_days=float(ocfg.get("cooldown_days", 14)),
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            default_account_cap=int(ocfg.get("default_account_cap", 30)),
        )
        plan = planner.build_plan(filters)

        hard_cap = max(1, int(ocfg.get("max_batch", 50)))
        req_max = int(body.get("max_send") or 0)
        max_send = min(hard_cap, req_max) if req_max > 0 else hard_cap

        async def _send_fn(target, text):
            return await send_via_adapters(
                request, target.platform, target.account_id, target.chat_key,
                text, _INBOX_ADAPTERS,
            )

        executor = OutreachExecutor(
            store, _send_fn, limiter=limiter,
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            sleep_fn=asyncio.sleep,
        )
        result = await executor.execute(
            plan.eligible, template,
            batch_id=str(body.get("batch_id") or ""), max_send=max_send,
        )
        result["planned_eligible"] = len(plan.eligible)
        return result

    @app.get("/api/unified-inbox/outreach/batch")
    async def api_unified_inbox_outreach_batch(
        request: Request, batch_id: str = "", response_window_days: float = 0,
        _=Depends(api_auth),
    ):
        """P61-4/5：查某批次回执统计（成功/失败计数 + P61-5 回复率）。"""
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store"}
        stats = store.outreach_batch_stats(batch_id)
        cm = getattr(request.app.state, "config_manager", None)
        try:
            ocfg = dict((getattr(cm, "config", None) or {}).get("outreach") or {})
        except Exception:
            ocfg = {}
        win = float(response_window_days) if response_window_days else float(ocfg.get("response_window_days", 7))
        stats["response"] = store.outreach_response_stats(batch_id, response_window_days=win)
        stats["ok"] = True
        return stats

    @app.post("/api/unified-inbox/analyze")
    async def api_unified_inbox_analyze(request: Request, _=Depends(api_auth)):
        """P30：升级版 AI 分析（多轮历史 + 风险预判 + 阶梯式话术建议）。

        新增字段：
          analysis.risk_signals   — 风险信号列表（price_negotiation/complaint/churn/etc）
          analysis.suggested_replies — [{text, rationale, risk_level}] 多档话术
          analysis.context_summary — 最近 10 轮对话摘要（LLM 生成或规则兜底）
        """
        body = await request.json()
        text = str(body.get("text") or "")
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        chat = body.get("chat") if isinstance(body.get("chat"), dict) else {}

        # P30：截取最近 10 条消息作为多轮上下文（比原来 8 条多 2 条，覆盖更长对话）
        ctx_messages = [m for m in messages if isinstance(m, dict)][-10:]

        if not text and ctx_messages:
            last = next((m for m in reversed(ctx_messages) if m.get("text")), {})
            text = str(last.get("text") or "")
        svc = _get_chat_assistant_service(request)
        analysis = await svc.analyze(text=text, messages=ctx_messages, chat=chat)
        out = analysis.to_dict()

        # C1 订单号提取
        order_no = str(getattr(analysis, "order_no", "") or "").strip() or _extract_order_no(text)
        out["order_no"] = order_no

        # P33：语种检测（优先检测最后入站消息，回落当前文本）
        _lang_text = text
        for _m in reversed(ctx_messages):
            if _m.get("direction") in ("in", "inbound") and _m.get("text"):
                _lang_text = str(_m["text"])
                break
        detected_lang = _detect_language(_lang_text)
        out["detected_lang"] = detected_lang

        # P30-A：规则级风险信号检测（快速、不消耗 LLM token）
        out["risk_signals"] = _detect_risk_signals(text, ctx_messages)

        # P30-B / P33：阶梯式话术建议（若 LLM 分析已提供 suggested_reply，基于它衍生多档，含语种适配）
        if out.get("suggested_reply") and not out.get("suggested_replies"):
            out["suggested_replies"] = _derive_tiered_replies(
                out["suggested_reply"], out.get("risk_signals", []), lang=detected_lang
            )

        # P30-C：多轮摘要（若消息够多，生成简短上下文摘要供坐席快速了解背景）
        if len(ctx_messages) >= 4:
            out["context_summary"] = _build_context_summary(ctx_messages)

        result: Dict[str, Any] = {"ok": True, "analysis": out}

        # Phase D：订单查询
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
            # Phase 6：坐席接管发出消息 → 清除自动回复打的「需人工」标签（闭环收口）
            try:
                from src.integrations.protocol_autoreply import clear_needs_human
                clear_needs_human(ibx, cid)
            except Exception:
                logger.debug("清除 needs-human 标签失败（已忽略）", exc_info=True)

        # A2 写路径收尾：发送收敛到各渠道适配器（与 collect/status 对称）。
        # 跨切面（坐席首响归属打点）统一留在路由，按 result.conversation_id 归属。
        try:
            result = await send_via_adapters(
                request, platform, account_id, chat_key, text, _INBOX_ADAPTERS,
            )
        except ChannelSendError as ex:
            raise HTTPException(ex.status_code, ex.detail)
        cid = (result.get("conversation_id") if isinstance(result, dict) else None) \
            or _conv_id(platform, account_id, chat_key)
        _mark_send(cid)
        copilot_meta = body.get("copilot_meta")
        if copilot_meta and cid:
            ibx = _inbox_store(request)
            _record_copilot_adopt_from_send(
                ibx, cid, _send_agent["agent_id"], text, copilot_meta,
            )
        return {"ok": True, "result": result}

    @app.post("/api/unified-inbox/send-media")
    async def api_unified_inbox_send_media(request: Request, _=Depends(page_auth)):
        """M6⑥：坐席从收件箱发送媒体（图片/语音/视频/文件）。

        multipart: file + platform/account_id/chat_key/caption。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA 发送）。
        发送成功后媒体以 /static URL 回写线程，坐席侧立即可见。
        """
        form = await request.form()
        platform = str(form.get("platform") or "").lower()
        account_id = str(form.get("account_id") or "default")
        chat_key = str(form.get("chat_key") or "")
        caption = str(form.get("caption") or "")
        upload = form.get("file")
        if not chat_key or upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, "file 和 chat_key 不能为空")

        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        if not orch.owns_media(platform, account_id):
            raise HTTPException(501, "该账号不支持从收件箱发送媒体（需 protocol 多开且在线）")

        data = await upload.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "文件过大（上限 25MB）")

        from src.integrations.protocol_bridge import save_outbound_media
        local, url, mtype = save_outbound_media(
            platform, account_id, upload.filename, data)
        _send_agent = _session_agent(request)
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type=mtype, caption=caption)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"媒体发送失败: {ex}")
        cid = _conv_id(platform, account_id, chat_key)
        try:
            ibx = _inbox_store(request)
            if ibx is not None:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
        except Exception:
            logger.debug("record_agent_send(media) 失败", exc_info=True)
        return {"ok": True, "result": res, "media_ref": url, "media_type": mtype}

    @app.post("/api/unified-inbox/send-voice")
    async def api_unified_inbox_send_voice(request: Request, _=Depends(page_auth)):
        """坐席发送语音回复：回复文本 → （可声音克隆）TTS 合成 → 作为语音消息发送。

        Body: { platform, account_id, chat_key, text, persona_id?, caption?, voice_cfg_override? }
        声音克隆复用 voice_profile（telegram.voice_reply / personas.*.voice_profile，
        backend=voice_clone_command/coqui_http 等）；合成后转 OGG/Opus 以"语音消息"形态发出。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA voice_output）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        persona_id = body.get("persona_id") or None
        caption = str(body.get("caption") or "")
        cfg_override = body.get("voice_cfg_override")
        if not chat_key or not text:
            raise HTTPException(400, "chat_key 和 text 不能为空")
        if len(text) > 1000:
            raise HTTPException(400, "文本过长（语音上限 1000 字）")

        orch = get_orchestrator()
        if not orch.owns_media(platform, account_id):
            raise HTTPException(501, "该账号不支持从收件箱发送语音（需 protocol 多开且在线）")

        # 解析语音配置（含声音克隆 voice_profile），允许调用方临时覆盖
        cm = getattr(request.app.state, "config_manager", None)
        raw_cfg = (getattr(cm, "config", None) or {}) if cm else {}
        from src.ai.persona_voice import resolve_voice_cfg
        voice_cfg = resolve_voice_cfg(persona_id, raw_cfg)
        if isinstance(cfg_override, dict):
            voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
        voice_cfg["enabled"] = True

        # 合成到临时目录
        import tempfile
        from pathlib import Path as _Path
        out_dir = _Path(tempfile.gettempdir()) / "unified_voice_send"
        voice_cfg["out_dir"] = str(out_dir)
        from src.ai.tts_pipeline import TTSPipeline
        try:
            tts = TTSPipeline(voice_cfg)
            result = await tts.synthesize(text, timeout_sec=45.0)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音合成失败: {ex}")
        if not result.ok or not result.audio_path:
            return {"ok": False, "reason": result.error or "tts_failed",
                    "message": f"语音合成失败：{result.error or 'unknown'}"}

        # 转 OGG/Opus，使其在 Telegram/WhatsApp 呈现为"语音消息"（ffmpeg 缺失则原样发）
        audio_path = result.audio_path
        try:
            from src.client.voice_sender import convert_to_ogg_opus
            converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
            if converted:
                audio_path = converted
        except Exception:
            logger.debug("OGG 转码失败，按原格式发送", exc_info=True)

        # 落到出站媒体目录（线程回写可见）+ 发送（强制 media_type=voice）
        try:
            with open(audio_path, "rb") as fh:
                data = fh.read()
            from src.integrations.protocol_bridge import save_outbound_media
            local, url, _mt = save_outbound_media(
                platform, account_id, os.path.basename(audio_path), data)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音落盘失败: {ex}")
        finally:
            try:
                os.remove(audio_path)
            except Exception:
                pass

        _send_agent = _session_agent(request)
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type="voice", caption=caption)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音发送失败: {ex}")
        cid = _conv_id(platform, account_id, chat_key)
        try:
            ibx = _inbox_store(request)
            if ibx is not None:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
        except Exception:
            logger.debug("record_agent_send(voice) 失败", exc_info=True)
        return {
            "ok": True, "result": res, "media_ref": url, "media_type": "voice",
            "duration_sec": getattr(result, "duration_sec", -1.0),
            "provider": getattr(result, "provider", ""),
            "voice": getattr(result, "voice", ""),
        }

    # ── I1 对话智能元数据 API ──────────────────────────────────────

    @app.get("/api/unified-inbox/conv-meta")
    async def api_conv_meta(request: Request, conversation_id: str = ""):
        """I1：获取对话智能元数据（最近意图、情绪趋势、风险、历史窗口）。

        返回：{ok, found, meta: {last_intent, last_emotion, emotion_trend,
                                  last_risk, msg_count, intent_history,
                                  emotion_history, updated_at}}
        """
        api_auth(request)
        store = _inbox_store(request)
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, "conversation_id 不能为空")
        if store is None:
            return {"ok": True, "found": False, "conversation_id": cid, "meta": None}
        meta = store.get_conv_meta(cid)
        return {
            "ok": True,
            "found": meta is not None,
            "conversation_id": cid,
            "meta": meta,
        }

    # ── K3：客户画像聚合 API ───────────────────────────────────────

    @app.get("/api/unified-inbox/contact-profile")
    async def api_contact_profile(
        request: Request,
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        """K3：聚合客户画像数据（对话智能 + CRM 档案 + 近期草稿决策）。

        返回：{ok, conversation_id, conv_meta, contact, recent_decisions}
        - conv_meta: 来自 I1 conversation_meta（最近意图/情绪/历史）
        - contact:   来自 contacts 子系统（标签/漏斗阶段/跟进状态）
        - recent_decisions: 来自 draft_audit_log（最近 5 条草稿决策记录）
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, "conversation_id 不能为空")

        store = _inbox_store(request)
        result: Dict[str, Any] = {
            "ok": True,
            "conversation_id": cid,
            "conv_meta": None,
            "contact": None,
            "recent_decisions": [],
        }

        # ① 对话智能元数据（I1）
        if store is not None:
            try:
                result["conv_meta"] = store.get_conv_meta(cid)
            except Exception:
                pass

        # ② CRM 联系人档案（contacts 子系统，可选）
        try:
            _cstore = _contacts_store(request)
            if _cstore is not None:
                # 从 conversation_id 反推平台/chat_key（格式: platform:account:chat_key）
                parts = cid.split(":", 2)
                if len(parts) == 3:
                    platform_key, account_id_key, chat_key_key = parts
                    ci = _cstore.get_ci_by_external(platform_key, account_id_key, chat_key_key)
                    if ci is not None:
                        contact_id = str(ci.contact_id if hasattr(ci, "contact_id") else (ci.get("contact_id") or ""))
                        if contact_id:
                            contact = _cstore.get_contact(contact_id)
                            attrs = _cstore.get_contact_attributes(contact_id) or {}
                            if contact:
                                result["contact"] = {
                                    "contact_id": contact_id,
                                    "name": str(getattr(contact, "display_name", "") or contact.get("display_name") or ""),
                                    "tags": attrs.get("tags", []),
                                    "funnel_stage": attrs.get("funnel_stage") or attrs.get("funnel"),
                                    "follow_up_overdue": cid in (_cstore.overdue_contact_ids() or set()),
                                    "note": attrs.get("note") or attrs.get("notes") or "",
                                }
        except Exception:
            pass

        # ③ 近期草稿决策（draft_audit_log，最近 5 条）
        if store is not None:
            try:
                logs = store.list_draft_audit(limit=200)
                recent = [
                    {
                        "draft_id": row.get("draft_id", ""),
                        "action": row.get("action", ""),
                        "agent_id": row.get("agent_id", ""),
                        "risk_level": row.get("risk_level", ""),
                        "autopilot_level": row.get("autopilot_level", ""),
                        "ts": row.get("ts", 0),
                        "reason": row.get("reason", ""),
                    }
                    for row in logs
                    if str(row.get("conversation_id") or "") == cid
                ][:5]
                result["recent_decisions"] = recent
            except Exception:
                pass

        # ④ N1: 跨平台会话归档（同一 contact_id 的所有历史对话）
        if store is not None:
            try:
                conv_meta = result.get("conv_meta") or {}
                linked_contact_id = str(conv_meta.get("contact_id") or "")
                # 也尝试从 CRM contact 获取 contact_id
                if not linked_contact_id and result.get("contact"):
                    linked_contact_id = str(result["contact"].get("contact_id") or "")
                if linked_contact_id:
                    cross_sessions = store.get_contact_sessions(linked_contact_id, limit=20)
                    # 排除当前 conversation_id
                    cross_sessions = [s for s in cross_sessions if s.get("conversation_id") != cid]
                    contact_csat_avg = store.get_contact_csat_avg(linked_contact_id)
                    result["cross_platform"] = {
                        "contact_id": linked_contact_id,
                        "session_count": len(cross_sessions),
                        "sessions": cross_sessions[:10],  # 最多返回 10 条
                        "contact_csat_avg": contact_csat_avg,
                    }
                else:
                    result["cross_platform"] = None
            except Exception:
                result["cross_platform"] = None

        # ⑤ Q1: 对话摘要（最新生成的 summary，主管画像核心字段）
        if store is not None:
            try:
                _meta = result.get("conv_meta") or {}
                _summary = str(_meta.get("summary") or "").strip()
                result["conv_summary"] = _summary if _summary else None
            except Exception:
                result["conv_summary"] = None

        return result

    # ── I3 模板库 API ─────────────────────────────────────────────

    def _template_store(request: Request):
        s = _inbox_store(request)
        if s is None:
            raise HTTPException(503, "模板库未启用（需 inbox_store）")
        return s

    @app.get("/api/reply-templates")
    async def api_templates_list(
        request: Request,
        language: str = "",
        platform: str = "",
        scene: str = "",
        search: str = "",
        limit: int = 100,
    ):
        """I3：列出回复模板（支持语言/平台/场景/关键词过滤）。"""
        api_auth(request)
        ts = _template_store(request)
        templates = ts.list_templates(
            language=language, platform=platform, scene=scene,
            search=search, limit=min(200, max(1, int(limit or 100)))
        )
        return {"ok": True, "templates": templates, "count": len(templates)}

    @app.post("/api/reply-templates")
    async def api_templates_create(request: Request):
        """I3：创建新模板（坐席/主管均可，主管审核后启用）。"""
        api_auth(request)
        ts = _template_store(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "请求体解析失败")
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "").strip()
        if not title or not content:
            raise HTTPException(400, "title 和 content 不能为空")
        tid = ts.create_template(
            title=title,
            content=content,
            language=str(body.get("language") or "zh"),
            platform=str(body.get("platform") or ""),
            scene=str(body.get("scene") or ""),
            created_by=str(body.get("created_by") or "agent"),
        )
        return {"ok": True, "id": tid, "title": title}

    @app.put("/api/reply-templates/{template_id}")
    async def api_templates_update(request: Request, template_id: str):
        """I3：更新模板字段（仅主管可完全编辑；普通坐席不能修改）。"""
        api_auth(request)
        ts = _template_store(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "请求体解析失败")
        updated = ts.update_template(
            template_id,
            title=body.get("title"),
            content=body.get("content"),
            language=body.get("language"),
            platform=body.get("platform"),
            scene=body.get("scene"),
            is_active=body.get("is_active"),
        )
        if not updated:
            raise HTTPException(404, "模板不存在")
        return {"ok": True, "id": template_id}

    @app.delete("/api/reply-templates/{template_id}")
    async def api_templates_delete(request: Request, template_id: str):
        """I3：软删除模板（主管专属）。"""
        api_auth(request)
        # 主管校验
        role = request.scope.get("session", {}).get("role", "")
        if role not in {"master", "admin"}:
            try:
                role = request.session.get("role", "")
            except Exception:
                role = ""
        if role not in {"master", "admin"}:
            raise HTTPException(403, "删除模板需要主管权限")
        ts = _template_store(request)
        deleted = ts.delete_template(template_id)
        if not deleted:
            raise HTTPException(404, "模板不存在")
        return {"ok": True, "id": template_id, "deleted": True}

    @app.post("/api/reply-templates/{template_id}/use")
    async def api_templates_use(request: Request, template_id: str):
        """I3：记录模板使用（用量统计，best-effort）。"""
        api_auth(request)
        ts = _template_store(request)
        ts.increment_template_usage(template_id)
        return {"ok": True, "id": template_id}

    # ─── Phase 23: 批量操作 ─────────────────────────────────────────────────

    @app.post("/api/workspace/batch/archive")
    async def api_batch_archive(request: Request, _=Depends(api_auth)):
        """P23：批量归档/取消归档会话。

        Body: {conversation_ids: [str, ...], archived: bool}
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        archived = bool(body.get("archived", True))
        if not cids:
            return {"ok": False, "error": "conversation_ids 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        updated = 0
        for cid in cids[:200]:  # 单次上限 200 条
            try:
                ok = store.set_conv_archived(cid, archived)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "archived": archived}

    @app.post("/api/workspace/batch/tags")
    async def api_batch_tags(request: Request, _=Depends(api_auth)):
        """P23：批量修改会话标签。

        Body: {conversation_ids: [str, ...], tags: [str, ...],
               mode: 'set'|'add'|'remove'}
          mode=set  → 替换全部标签
          mode=add  → 追加（去重）
          mode=remove → 删除指定标签
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        tags = [str(t) for t in (body.get("tags") or []) if str(t).strip()]
        mode = str(body.get("mode", "add")).lower()
        if mode not in ("set", "add", "remove"):
            mode = "add"
        if not cids:
            return {"ok": False, "error": "conversation_ids 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        import json as _json
        updated = 0
        for cid in cids[:200]:
            try:
                current = store.get_conv_tags(cid) or []
                if mode == "set":
                    new_tags = tags
                elif mode == "add":
                    new_tags = list(dict.fromkeys(current + tags))  # 保序去重
                else:  # remove
                    rm = set(tags)
                    new_tags = [t for t in current if t not in rm]
                ok = store.set_conv_tags(cid, new_tags)
                if ok:
                    updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "mode": mode}

    @app.post("/api/workspace/batch/assign")
    async def api_batch_assign(request: Request, _=Depends(api_auth)):
        """P23：批量分配会话给坐席。

        Body: {conversation_ids: [str, ...], agent_id: str}
        返回: {ok: true, updated: int}
        """
        body = await request.json()
        cids = [str(x) for x in (body.get("conversation_ids") or []) if x]
        agent_id = str(body.get("agent_id") or "").strip()
        if not cids or not agent_id:
            return {"ok": False, "error": "conversation_ids / agent_id 不能为空"}
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        updated = 0
        for cid in cids[:200]:
            try:
                store.update_conv_meta(cid, {"claimed_by": agent_id})
                updated += 1
            except Exception:
                pass
        return {"ok": True, "updated": updated, "agent_id": agent_id}

    # ─── Phase 24: 通知中心（SSE 事件广播） ───────────────────────────────

    @app.get("/api/workspace/notifications")
    async def api_workspace_notifications(
        request: Request,
        limit: int = 50,
    ):
        """P24：获取最近通知（SSE 事件历史，存于内存队列）。

        前端在 SSE 断线重连后调用此接口同步缺漏事件。
        """
        # 通知队列挂在 app.state.notif_queue（由 SSE 推送时顺带写入）
        queue: list = getattr(request.app.state, "notif_queue", [])
        limit = max(1, min(200, int(limit or 50)))
        return {"ok": True, "notifications": queue[-limit:]}

    @app.post("/api/workspace/notifications/read")
    async def api_workspace_notifications_read(request: Request, _=Depends(api_auth)):
        """P24：标记所有通知为已读（仅清除前端 badge，不删除历史）。"""
        return {"ok": True, "read_at": int(__import__("time").time() * 1000)}

    # ─── Phase 29: Queue Monitor 实时看板 ──────────────────────────────

    @app.get("/api/workspace/queue-monitor")
    async def api_queue_monitor(request: Request):
        """P29：实时运营看板——每坐席工作量快照 + 全局队列指标。

        返回：
          agents: [{agent_id, agent_name, status, open_convs, unread_total,
                    avg_wait_sec, oldest_wait_sec, load_pct}]
          queue:  {total_open, total_unread, avg_wait_sec, crit_count,
                   unassigned_count}
          ts: float  — 快照时间戳
        """
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}

        import time as _t
        now = _t.time()
        stale_sec = _presence_stale_sec(request)

        # 1. 在线坐席
        presence = store.list_agent_presence(active_within_sec=stale_sec) if store else []
        agent_map: Dict[str, Dict[str, Any]] = {
            p["agent_id"]: {
                "agent_id": p["agent_id"],
                "agent_name": p.get("display_name") or p["agent_id"],
                "status": p.get("status", "offline"),
                "open_convs": 0,
                "unread_total": 0,
                "wait_secs": [],
                "avg_wait_sec": 0,
                "oldest_wait_sec": 0,
                "load_pct": 0,
            }
            for p in presence
        }
        # 未认领的虚拟坐席桶
        agent_map["__unassigned__"] = {
            "agent_id": "__unassigned__",
            "agent_name": "（未认领）",
            "status": "virtual",
            "open_convs": 0, "unread_total": 0,
            "wait_secs": [], "avg_wait_sec": 0,
            "oldest_wait_sec": 0, "load_pct": 0,
        }

        # 2. 遍历全量未归档会话（limit 500）
        convs = store.list_conversations(limit=500)
        total_unread = 0
        crit_count = 0
        unassigned_count = 0
        all_wait_secs: list = []

        for c in convs:
            # 跳过已归档
            meta = store.get_conv_meta(c.get("conversation_id", "")) or {}
            if meta.get("archived"):
                continue

            claimed = str(c.get("claimed_by") or "").strip()
            bucket = claimed if claimed in agent_map else "__unassigned__"
            if not claimed:
                unassigned_count += 1

            agent_map[bucket]["open_convs"] += 1
            unread = int(c.get("unread") or 0)
            agent_map[bucket]["unread_total"] += unread
            total_unread += unread

            wait = int(c.get("unanswered_sec") or 0)
            if wait > 0:
                agent_map[bucket]["wait_secs"].append(wait)
                all_wait_secs.append(wait)
            if c.get("sla_level") == "crit":
                crit_count += 1

        # 3. 计算每坐席统计
        max_open = max((a["open_convs"] for a in agent_map.values()), default=1) or 1
        for a in agent_map.values():
            ws = a.pop("wait_secs")
            a["avg_wait_sec"] = int(sum(ws) / len(ws)) if ws else 0
            a["oldest_wait_sec"] = int(max(ws)) if ws else 0
            a["load_pct"] = round(a["open_convs"] / max_open * 100)

        # 排序：在线 → 忙碌 → 离线；同状态按工作量降序
        _status_order = {"online": 0, "busy": 1, "offline": 2, "virtual": 3}
        agents_list = sorted(
            agent_map.values(),
            key=lambda a: (_status_order.get(a["status"], 9), -a["open_convs"]),
        )

        avg_wait_global = int(sum(all_wait_secs) / len(all_wait_secs)) if all_wait_secs else 0

        return {
            "ok": True,
            "agents": agents_list,
            "queue": {
                "total_open": sum(a["open_convs"] for a in agents_list),
                "total_unread": total_unread,
                "avg_wait_sec": avg_wait_global,
                "crit_count": crit_count,
                "unassigned_count": unassigned_count,
            },
            "ts": now,
        }

    # ─── Phase 28: Webhook 外发运行时配置 ─────────────────────────────

    @app.get("/api/workspace/webhook-outbound")
    async def api_webhook_outbound_list(request: Request, _=Depends(api_auth)):
        """P28：列出当前已配置的出站 Webhook（含事件别名、格式）。"""
        notifier = getattr(request.app.state, "webhook_notifier", None)
        if notifier is None:
            return {"ok": True, "webhooks": [], "note": "WebhookNotifier 未启动"}
        # 脱敏 secret
        hooks = []
        for m in getattr(notifier, "_matchers", []):
            hooks.append({
                "url": m.get("url", ""),
                "name": m.get("name", ""),
                "fmt": m.get("fmt", "json"),
                "types": list(m.get("types") or ["all"]),
                "has_secret": bool(m.get("secret")),
            })
        return {
            "ok": True,
            "webhooks": hooks,
            "total_sent": getattr(notifier, "total_sent", 0),
            "total_errors": getattr(notifier, "total_errors", 0),
        }

    @app.post("/api/workspace/webhook-outbound/test")
    async def api_webhook_outbound_test(request: Request, _=Depends(api_auth)):
        """P28：向所有已配置 Webhook 发送测试事件（ping）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限")
        import time as _t
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish({
                "type": "report",
                "subtype": "webhook_test",
                "data": {"message": "Webhook 测试 Ping", "ts": _t.time()},
                "ts": _t.time(),
            })
            return {"ok": True, "message": "测试事件已发布到事件总线"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/workspace/queue-monitor/reassign")
    async def api_queue_reassign(request: Request, _=Depends(api_auth)):
        """P29：将指定会话重新分配给另一坐席（主管操作）。

        Body: {conversation_id: str, to_agent_id: str}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, "需要主管权限")
        body = await request.json()
        cid = str(body.get("conversation_id") or "").strip()
        to_agent = str(body.get("to_agent_id") or "").strip()
        if not cid or not to_agent:
            raise HTTPException(422, "conversation_id / to_agent_id 不能为空")
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        store.update_conv_meta(cid, {"claimed_by": to_agent})
        # 事件总线广播（通知目标坐席）
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish({
                "type": "conversation_claim",
                "data": {"conversation_id": cid, "agent_id": to_agent,
                         "action": "reassigned_by_supervisor"},
                "ts": _t.time(),
            })
        except Exception:
            pass
        return {"ok": True, "conversation_id": cid, "to_agent_id": to_agent}

    # ─── Phase 25: 坐席协作注解 ─────────────────────────────────────────

    # ─── Phase 48: @mention 智能路由 ─────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/mention-suggestions")
    async def api_conv_mention_suggestions(
        conversation_id: str,
        request: Request,
        q: str = "",
        limit: int = 8,
    ):
        """P48：按关系阶段 + 负荷 + QA 推荐 @ 坐席。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "suggestions": [], "auto_cc": []}
        from src.inbox.mention_router import MentionRouter
        from src.workspace.agent_coordinator import AgentCoordinator

        mctx = _mention_context_for_conv(request, conversation_id, store)
        coord = AgentCoordinator.from_request(request, config_manager)
        users = []
        us = _user_store_from_config(config_manager)
        if us is not None:
            try:
                users = us.list_users()
            except Exception:
                pass
        router = MentionRouter.from_store(
            store, presence=coord.list_presence(), users=users,
        )
        me = _session_agent(request)["agent_id"]
        result = router.suggest(
            stage=mctx["stage"],
            stage_label=mctx["stage_label"],
            churn_level=mctx["churn_level"],
            claim_agent_id=mctx["claim_agent_id"],
            overdue_chain=mctx["overdue_chain"],
            exclude_agent_id=me,
            query=q,
            limit=limit,
        )
        return {"ok": True, "conversation_id": conversation_id, **result, "context": mctx}

    @app.get("/api/workspace/conv/{conversation_id}/notes")
    async def api_conv_notes_list(conversation_id: str, request: Request, limit: int = 50):
        """V1：获取会话内部注解列表（坐席可见，客户不可见）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        notes = store.list_conv_notes(conversation_id, limit=limit)
        return {"ok": True, "notes": notes, "count": len(notes)}

    @app.post("/api/workspace/conv/{conversation_id}/notes")
    async def api_conv_notes_add(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：在会话中添加内部注解。

        Body: {body: str, mentions: [agent_id, ...] (可选)}
        """
        body_data = await request.json()
        text = str(body_data.get("body", "")).strip()
        if not text:
            raise HTTPException(422, "body 不能为空")
        mentions = [str(m) for m in (body_data.get("mentions") or []) if str(m).strip()]
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        # P48：高流失 + 高阶段自动抄送主管（若尚未 @）
        auto_cc_applied: List[Dict[str, Any]] = []
        try:
            from src.inbox.mention_router import MentionRouter
            from src.workspace.agent_coordinator import AgentCoordinator
            mctx = _mention_context_for_conv(request, conversation_id, store)
            users = []
            us = _user_store_from_config(config_manager)
            if us is not None:
                users = us.list_users()
            coord = AgentCoordinator.from_request(request, config_manager)
            router = MentionRouter.from_store(
                store, presence=coord.list_presence(), users=users,
            )
            sugg = router.suggest(
                stage=mctx["stage"],
                stage_label=mctx["stage_label"],
                churn_level=mctx["churn_level"],
                claim_agent_id=mctx["claim_agent_id"],
                overdue_chain=mctx["overdue_chain"],
            )
            mention_set = set(mentions)
            for cc in sugg.get("auto_cc") or []:
                cc_id = str(cc.get("agent_id") or "")
                if cc_id and cc_id not in mention_set:
                    mentions.append(cc_id)
                    mention_set.add(cc_id)
                    auto_cc_applied.append(cc)
        except Exception:
            pass
        # 从 session 取当前坐席身份
        agent_id = str(request.session.get("user_name") or request.session.get("username") or "")
        agent_name = str(request.session.get("display_name") or agent_id)
        try:
            note = store.add_conv_note(
                conversation_id, text,
                agent_id=agent_id, agent_name=agent_name, mentions=mentions,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        # P25：通过事件总线广播注解事件（@提及 → SSE 通知目标坐席）
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            # P45：@mention 时附带协作上下文（阶段 + 推荐话题）
            collab_hint = ""
            try:
                ctx = _conv_relationship_context(request, conversation_id, store)
                from src.inbox.relationship_stage import compute_relationship_stage
                rel = compute_relationship_stage(
                    exchange_count=ctx["exchange_count"],
                    intimacy_score=ctx["intimacy_score"],
                )
                collab_hint = rel.get("stage_label") or ""
            except Exception:
                pass
            get_event_bus().publish("conv_note", {
                **note,
                "conversation_id": conversation_id,
                "stage_label": collab_hint,
                "ts": _t.time(),
            })
        except Exception:
            pass
        return {"ok": True, "note": note, "auto_cc": auto_cc_applied}

    @app.patch("/api/workspace/conv/{conversation_id}/notes/{note_id}")
    async def api_conv_notes_edit(
        conversation_id: str, note_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：编辑注解内容。"""
        body_data = await request.json()
        text = str(body_data.get("body", "")).strip()
        if not text:
            raise HTTPException(422, "body 不能为空")
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        agent_id = str(request.session.get("user_name") or "")
        ok = store.edit_conv_note(note_id, text, agent_id=agent_id)
        if not ok:
            raise HTTPException(404, "注解不存在")
        return {"ok": True, "note_id": note_id}

    @app.delete("/api/workspace/conv/{conversation_id}/notes/{note_id}")
    async def api_conv_notes_delete(
        conversation_id: str, note_id: str, request: Request, _=Depends(api_auth),
    ):
        """V1：删除注解。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        agent_id = str(request.session.get("user_name") or "")
        ok = store.delete_conv_note(note_id, agent_id=agent_id)
        if not ok:
            raise HTTPException(404, "注解不存在")
        return {"ok": True, "note_id": note_id}

    # ─── Phase 27: 客户活跃时段热力图 ───────────────────────────────────

    # ─── Phase 31: 客户 360° 时间轴 ────────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/timeline")
    async def api_contact_timeline(
        contact_id: str, request: Request, limit: int = 100
    ):
        """X1：获取客户完整互动时间轴（消息/注解/归档/摘要，跨会话聚合）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        limit = max(1, min(500, int(limit or 100)))
        events = store.get_contact_timeline(contact_id, limit=limit)
        return {"ok": True, "contact_id": contact_id, "events": events, "count": len(events)}

    # ─── Phase 43: 关系阶段可视化 + 进阶提醒 ───────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/relationship-stage")
    async def api_conv_relationship_stage(conversation_id: str, request: Request):
        """DD1/P46：关系阶段进度条 + 待确认进阶（坐席确认制）。"""
        api_auth(request)
        store = _inbox_store(request)
        result = _build_relationship_stage_payload(
            request, conversation_id, store, emit_pending_event=True,
        )
        return {"ok": True, "conversation_id": conversation_id, **result}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/confirm")
    async def api_conv_relationship_stage_confirm(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：坐席确认关系进阶。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "")
        target = str(body.get("stage") or payload.get("pending_stage") or payload.get("computed_stage") or "")
        from src.utils.companion_relationship import STAGE_ORDER
        if target not in STAGE_ORDER:
            raise HTTPException(422, "无效目标阶段")
        if confirmed and STAGE_ORDER.index(target) <= STAGE_ORDER.index(confirmed):
            raise HTTPException(422, "目标阶段必须高于当前确认阶段")
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str((payload.get("context") or {}).get("contact_id") or "")
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        store.record_draft_audit(
            "", action="stage_confirm", agent_id=agent_id,
            reason=f"{prev_label} → {target}",
            conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            from src.utils.companion_relationship import STAGE_LABEL_ZH
            import time as _t
            get_event_bus().publish("stage_advance", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "confirmed": True,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "previous_stage": confirmed,
                "previous_stage_label": prev_label,
                "stage": target,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/downgrade")
    async def api_conv_relationship_stage_downgrade(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：坐席手动降级关系阶段（附原因）。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        body = await request.json()
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise HTTPException(422, "reason 不能为空")
        from src.inbox.relationship_stage import downgrade_stage_one_level
        from src.utils.companion_relationship import STAGE_ORDER, STAGE_LABEL_ZH
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "initial")
        target = str(body.get("stage") or downgrade_stage_one_level(confirmed))
        if target not in STAGE_ORDER:
            raise HTTPException(422, "无效目标阶段")
        if STAGE_ORDER.index(target) >= STAGE_ORDER.index(confirmed):
            raise HTTPException(422, "目标阶段必须低于当前确认阶段")
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str((payload.get("context") or {}).get("contact_id") or "")
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        note_body = f"[关系降级] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{reason}"
        store.add_conv_note(
            conversation_id, note_body,
            agent_id=agent_id, agent_name=agent_name,
        )
        store.record_draft_audit(
            "", action="stage_downgrade", agent_id=agent_id,
            reason=note_body, conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("stage_downgrade", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "agent_name": agent_name,
                "previous_stage_label": prev_label,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.post("/api/workspace/conv/{conversation_id}/relationship-stage/reunion")
    async def api_conv_relationship_stage_reunion(
        conversation_id: str, request: Request, _=Depends(api_auth),
    ):
        """P46：确认久别重逢 — 将确认阶段同步至亲密度阶段并推荐回暖话题。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        from src.utils.companion_relationship import derive_stage_from_intimacy, STAGE_LABEL_ZH
        payload = _build_relationship_stage_payload(request, conversation_id, store)
        if not payload.get("reunion"):
            raise HTTPException(422, "当前会话未检测到久别重逢信号")
        ctx = payload.get("context") or {}
        intim = ctx.get("intimacy_score")
        target = derive_stage_from_intimacy(float(intim)) if intim is not None else str(payload.get("computed_stage") or "initial")
        confirmed = str(payload.get("confirmed_stage") or payload.get("stage") or "")
        prev_label = payload.get("confirmed_stage_label") or payload.get("stage_label")
        contact_id = str(ctx.get("contact_id") or "")
        import time as _t
        reunion_ts = _t.time()
        agent_id, agent_name = _agent_from_request(request)
        if contact_id:
            store.confirm_rel_stage_with_contact(
                conversation_id, contact_id, target,
                updated_by=agent_id, sync_all_convs=True,
            )
            store.set_contact_rel_stage(
                contact_id, target, updated_by=agent_id, reunion_ack_ts=reunion_ts,
            )
        else:
            store.confirm_rel_stage(conversation_id, target)
        store.ack_rel_reunion(conversation_id, ts=reunion_ts)
        note = str(body.get("note") or "已确认回暖，采用自然问候策略").strip()
        store.add_conv_note(
            conversation_id, f"[关系回暖] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{note}",
            agent_id=agent_id, agent_name=agent_name,
        )
        reunion_reason = (
            f"[关系回暖] {prev_label} → {STAGE_LABEL_ZH.get(target, target)}：{note}"
        )
        store.record_draft_audit(
            "", action="stage_reunion", agent_id=agent_id,
            reason=reunion_reason, conversation_id=conversation_id,
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("stage_reunion", {
                "conversation_id": conversation_id,
                "contact_id": contact_id,
                "agent_name": agent_name,
                "stage_label": STAGE_LABEL_ZH.get(target, target),
                "note": note,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_relationship_stage_payload(request, conversation_id, store)
        return {"ok": True, "conversation_id": conversation_id, **refreshed}

    @app.get("/api/workspace/contact/{contact_id}/relationship-stage")
    async def api_contact_relationship_stage(contact_id: str, request: Request):
        """P50：客户级关系阶段（含多会话冲突检测）。"""
        api_auth(request)
        store = _inbox_store(request)
        result = _build_contact_relationship_payload(request, contact_id, store)
        return {"ok": True, "contact_id": contact_id, **result}

    @app.post("/api/workspace/contact/{contact_id}/relationship-stage/sync")
    async def api_contact_relationship_stage_sync(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """P50：一键对齐多会话阶段（to_contact | to_highest）。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        mode = str(body.get("mode") or "to_contact").strip()
        from src.inbox.contact_rel_stage import highest_stage
        from src.utils.companion_relationship import STAGE_ORDER

        contact_rec = store.get_contact_rel_stage(contact_id) or {}
        contact_stage = str(contact_rec.get("confirmed_stage") or "")
        conv_stages = store.list_conv_rel_stages_for_contact(contact_id)
        if mode == "to_highest":
            target = highest_stage([s for s in conv_stages.values() if s] + ([contact_stage] if contact_stage else []))
        else:
            target = contact_stage or highest_stage([s for s in conv_stages.values() if s])
        if not target or target not in STAGE_ORDER:
            raise HTTPException(422, "无可对齐的目标阶段")
        agent_id, _ = _agent_from_request(request)
        if not contact_stage:
            store.set_contact_rel_stage(contact_id, target, updated_by=agent_id)
        elif mode == "to_highest" and STAGE_ORDER.index(target) > STAGE_ORDER.index(contact_stage):
            store.set_contact_rel_stage(contact_id, target, updated_by=agent_id)
        synced = store.sync_convs_to_stage(contact_id, target)
        store.record_draft_audit(
            f"contact:{contact_id}", action="stage_sync", agent_id=agent_id,
            reason=f"对齐至 {target}（{mode}，{synced} 会话）",
            conversation_id="",
        )
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            from src.utils.companion_relationship import STAGE_LABEL_ZH
            get_event_bus().publish("stage_sync", {
                "contact_id": contact_id,
                "agent_id": agent_id,
                "target_stage": target,
                "target_stage_label": STAGE_LABEL_ZH.get(target, target),
                "mode": mode,
                "synced": synced,
                "ts": _t.time(),
            })
        except Exception:
            pass
        refreshed = _build_contact_relationship_payload(request, contact_id, store)
        return {"ok": True, "contact_id": contact_id, "synced": synced, "target_stage": target, **refreshed}

    @app.get("/api/workspace/contact/{contact_id}/stage-timeline")
    async def api_contact_stage_timeline(
        contact_id: str, request: Request, limit: int = 50,
    ):
        """P51：客户关系阶段演进时间轴（确认/降级/回暖/对齐）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        from src.inbox.stage_timeline import (
            build_contact_stage_summary,
            enrich_stage_audit_row,
        )
        lim = max(1, min(200, int(limit or 50)))
        rows = store.list_contact_stage_audits(contact_id, limit=lim)
        events = [enrich_stage_audit_row(r) for r in rows]
        contact_rec = store.get_contact_rel_stage(contact_id)
        summary = build_contact_stage_summary(events, contact_rec=contact_rec)
        return {
            "ok": True,
            "contact_id": contact_id,
            "events": events,
            "count": len(events),
            "summary": summary,
        }

    # ─── Phase 45: 多坐席协作剧本上下文 ─────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/collab-context")
    async def api_contact_collab_context(contact_id: str, request: Request):
        """EE1：客户级协作上下文（统一阶段 + 积分 + 话题 + 活跃工作链）。"""
        api_auth(request)
        store = _inbox_store(request)
        cs = _contacts_store(request)
        from src.inbox.conversation_script import ConversationScriptEngine
        from src.inbox.relationship_stage import compute_relationship_stage

        intimacy_score = None
        primary_name = ""
        if cs is not None:
            try:
                contact = cs.get_contact(contact_id)
                if contact:
                    primary_name = str(contact.primary_name or "")
                journey = cs.get_journey_by_contact(contact_id)
                if journey is not None:
                    intimacy_score = float(journey.intimacy_score or 0)
            except Exception:
                pass

        # 聚合该客户所有会话消息数
        message_count = 0
        conv_ids: List[str] = []
        if store is not None:
            try:
                rows = store._conn.execute(
                    "SELECT conversation_id FROM conversations WHERE contact_id = ? LIMIT 20",
                    (contact_id,),
                ).fetchall()
                conv_ids = [r["conversation_id"] for r in rows]
                if conv_ids:
                    ph = ",".join("?" * len(conv_ids))
                    message_count = store._conn.execute(
                        f"SELECT COUNT(*) as c FROM messages WHERE conversation_id IN ({ph})",
                        conv_ids,
                    ).fetchone()["c"]
            except Exception:
                pass

        rel_payload = _build_contact_relationship_payload(request, contact_id, store)
        rel = {k: v for k, v in rel_payload.items() if k not in (
            "stage_conflict", "stage_conflict_detail", "contact_updated_by",
        )}
        engine = ConversationScriptEngine()
        topics = engine.suggest_topics(
            rel.get("display_stage") or rel.get("confirmed_stage") or rel.get("stage") or "initial",
            custom_topics=store.list_script_topics() if store else [],
            limit=3,
        ).get("topics", [])

        engagement_raw = store.get_contact_engagement(contact_id) if store else None
        engagement = None
        if engagement_raw:
            from src.inbox.engagement_scorer import EngagementScorer
            ln, _ = EngagementScorer._level_for(int(engagement_raw.get("points") or 0))
            engagement = {**engagement_raw, "level_name": ln}
        active_chains: List[Dict[str, Any]] = []
        recent_notes: List[Dict[str, Any]] = []
        if store is not None and conv_ids:
            for cid in conv_ids[:5]:
                try:
                    for ex in store.get_conv_chain_executions(cid):
                        if ex.get("status") == "running":
                            active_chains.append(ex)
                    for note in store.list_conv_notes(cid, limit=5)[-3:]:
                        recent_notes.append(note)
                except Exception:
                    pass
            recent_notes.sort(key=lambda n: float(n.get("ts") or 0), reverse=True)
            recent_notes = recent_notes[:8]

        return {
            "ok": True,
            "contact_id": contact_id,
            "primary_name": primary_name,
            "relationship": rel,
            "contact_stage": rel_payload.get("contact_stage"),
            "contact_stage_label": rel_payload.get("contact_stage_label"),
            "stage_conflict": rel_payload.get("stage_conflict", False),
            "stage_conflict_detail": rel_payload.get("stage_conflict_detail"),
            "suggested_topics": topics,
            "engagement": engagement,
            "active_chains": active_chains[:10],
            "recent_notes": recent_notes,
            "conversation_ids": conv_ids,
        }

    @app.get("/api/workspace/conv/{conversation_id}/collab-context")
    async def api_conv_collab_context(conversation_id: str, request: Request):
        """EE1：会话级协作条（含 @mention 时附带阶段+话题）。"""
        api_auth(request)
        store = _inbox_store(request)
        rel = _build_relationship_stage_payload(request, conversation_id, store)
        ctx = rel.get("context") or {}
        contact_id = ctx.get("contact_id") or ""
        if contact_id:
            resp = await api_contact_collab_context(contact_id, request)
            resp["conversation_id"] = conversation_id
            resp["relationship"] = {k: v for k, v in rel.items() if k != "context"}
            return resp
        from src.inbox.conversation_script import ConversationScriptEngine
        engine = ConversationScriptEngine()
        topics = engine.suggest_topics(
            rel.get("display_stage") or rel.get("stage") or "initial",
            custom_topics=store.list_script_topics() if store else [],
            limit=3,
        ).get("topics", [])
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "relationship": {k: v for k, v in rel.items() if k != "context"},
            "suggested_topics": topics,
            "recent_notes": store.list_conv_notes(conversation_id, limit=5) if store else [],
        }

    # ─── Phase 40: 情感陪伴剧本引擎 ─────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/script-suggestions")
    async def api_conv_script_suggestions(conversation_id: str, request: Request):
        """CC1：按关系阶段推荐话题切入点（内置 + 自定义）。"""
        api_auth(request)
        store = _inbox_store(request)
        from src.inbox.conversation_script import ConversationScriptEngine

        message_count = 0
        last_msg_text = ""
        intimacy_score: Optional[float] = None
        exchange_count = 0
        reunion = False

        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
                message_count = store._conn.execute(
                    "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()["c"]
                if rows:
                    for r in rows:
                        if r["direction"] in ("in", "inbound"):
                            last_msg_text = str(r["text"] or "")
                            break
                meta = store.get_conv_meta(conversation_id) or {}
                contact_id = str(meta.get("contact_id") or "")
                if contact_id:
                    cs = _contacts_store(request)
                    if cs is not None:
                        try:
                            journey = cs.get_journey_by_contact(contact_id)
                            if journey is not None:
                                intimacy_score = float(journey.intimacy_score or 0)
                        except Exception:
                            pass
                exchange_count = max(0, message_count // 2)
            except Exception:
                logger.debug("script-suggestions 上下文失败", exc_info=True)

        custom = store.list_script_topics() if store else []
        engine = ConversationScriptEngine()
        stage = engine.derive_stage_from_signals(
            exchange_count=exchange_count, intimacy_score=intimacy_score,
        )
        result = engine.suggest_topics(
            stage,
            custom_topics=custom,
            last_msg_text=last_msg_text,
            message_count=message_count,
            reunion=reunion,
            limit=6,
        )
        return {"ok": True, "conversation_id": conversation_id, **result}

    @app.get("/api/workspace/script-topics")
    async def api_script_topics_list(request: Request, stage: str = ""):
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "topics": []}
        return {"ok": True, "topics": store.list_script_topics(stage=stage)}

    @app.post("/api/workspace/script-topics")
    async def api_script_topics_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        topic_id = store.upsert_script_topic(body)
        return {"ok": True, "topic_id": topic_id}

    @app.put("/api/workspace/script-topics/{topic_id}")
    async def api_script_topics_update(topic_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["topic_id"] = topic_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_script_topic(body)
        return {"ok": True, "topic_id": topic_id}

    @app.delete("/api/workspace/script-topics/{topic_id}")
    async def api_script_topics_delete(topic_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_script_topic(topic_id)
        return {"ok": ok}

    # ─── Phase 41: 客户互动积分与成就 ───────────────────────────────────

    @app.get("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_get(contact_id: str, request: Request):
        """CC1：读取客户互动积分（无则返回空）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "engagement": None}
        data = store.get_contact_engagement(contact_id)
        if data is None:
            return {"ok": True, "contact_id": contact_id, "engagement": None, "computed": False}
        from src.inbox.engagement_scorer import _ACHIEVEMENT_DEFS, EngagementScorer
        level_name, _ = EngagementScorer._level_for(int(data.get("points") or 0))
        ach_details = [
            {**_ACHIEVEMENT_DEFS.get(aid, {"name": aid, "icon": "🏅", "desc": ""}),
             "id": aid, "unlocked": True}
            for aid in (data.get("achievements") or [])
        ]
        for aid, defn in _ACHIEVEMENT_DEFS.items():
            if aid not in (data.get("achievements") or []):
                ach_details.append({**defn, "id": aid, "unlocked": False})
        return {
            "ok": True,
            "contact_id": contact_id,
            "computed": True,
            "engagement": {
                **data,
                "level_name": level_name,
                "achievement_details": ach_details,
                "is_vip": int(data.get("points") or 0) >= 600,
            },
        }

    @app.post("/api/workspace/contact/{contact_id}/engagement")
    async def api_contact_engagement_compute(contact_id: str, request: Request, _=Depends(api_auth)):
        """CC1：重新计算并存储互动积分。"""
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        result = store.compute_and_store_engagement(contact_id)
        return {"ok": True, "contact_id": contact_id, "engagement": result}

    # ─── Phase 42: 坐席 AI 副驾（打字辅助） ─────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/copilot-prefill")
    async def api_conv_copilot_prefill(
        conversation_id: str,
        request: Request,
        trigger: str = "open",
        workflow_text: str = "",
        workflow_chain_name: str = "",
        workflow_step: int = 0,
        mention_body: str = "",
        mention_from: str = "",
        polish: bool = True,
    ):
        """P49/P52：事件驱动 Copilot 预填（可选 LLM 润色）。"""
        api_auth(request)
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=trigger.strip(),
            workflow_text=workflow_text,
            workflow_chain_name=workflow_chain_name,
            workflow_step=workflow_step,
            mention_body=mention_body,
            mention_from=mention_from,
        )
        last_customer = ""
        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 20""",
                    (conversation_id,),
                ).fetchall()
                for r in rows:
                    if r["direction"] in ("in", "inbound") and r["text"]:
                        last_customer = str(r["text"])
                        break
            except Exception:
                pass
        templates: List[Dict[str, Any]] = []
        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass
        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text="",
            last_customer_msg=last_customer,
            stage=ctx["stage"],
            templates=templates,
            context=ctx,
            limit=4,
        )
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "trigger": ctx.get("trigger") or trigger,
            "stage": ctx["stage"],
            **result,
            "context": ctx,
        }
        payload = await _maybe_polish_copilot(
            request, payload,
            conversation_id=conversation_id,
            partial_text="",
            last_customer_msg=last_customer,
            polish_requested=bool(polish),
        )
        agent_id, _ = _agent_from_request(request)
        _record_copilot_impression_if_prefill(
            store, conversation_id, agent_id, payload, partial_text="",
        )
        return payload

    @app.post("/api/workspace/conv/{conversation_id}/reply-suggest")
    async def api_conv_reply_suggest(conversation_id: str, request: Request, _=Depends(api_auth)):
        """CC1/P49：实时回复补全（规则 + 模板 + 阶段/工作链/@mention 联动）。"""
        body = await request.json()
        partial = str(body.get("partial") or body.get("text") or "")
        recent = body.get("messages") if isinstance(body.get("messages"), list) else []

        last_customer = ""
        for m in reversed(recent):
            if isinstance(m, dict) and m.get("direction") in ("in", "inbound") and m.get("text"):
                last_customer = str(m["text"])
                break

        templates: List[Dict[str, Any]] = []
        store = _inbox_store(request)
        ctx = _build_copilot_context(
            request, conversation_id, store,
            trigger=str(body.get("trigger") or ""),
            workflow_text=str(body.get("workflow_text") or ""),
            workflow_chain_name=str(body.get("workflow_chain_name") or ""),
            workflow_step=int(body.get("workflow_step") or 0),
            mention_body=str(body.get("mention_body") or ""),
            mention_from=str(body.get("mention_from") or ""),
        ) if store is not None else {}

        if store is not None:
            try:
                templates = store.list_templates(limit=50, active_only=True)
            except Exception:
                pass

        from src.inbox.reply_copilot import ReplyCopilot
        result = ReplyCopilot().suggest(
            partial_text=partial,
            last_customer_msg=last_customer,
            stage=ctx.get("stage") or "initial",
            recent_messages=recent,
            templates=templates,
            context=ctx,
            limit=4 if not partial else 3,
        )
        polish_req = bool(body.get("polish"))
        payload = {
            "ok": True,
            "conversation_id": conversation_id,
            "partial": partial,
            "stage": ctx.get("stage") or "initial",
            **result,
            "context": ctx,
        }
        if polish_req and not partial.strip():
            payload = await _maybe_polish_copilot(
                request, payload,
                conversation_id=conversation_id,
                partial_text=partial,
                last_customer_msg=last_customer,
                polish_requested=True,
            )
        else:
            payload["polished"] = False
        if not partial.strip() and store is not None:
            agent_id, _ = _agent_from_request(request)
            _record_copilot_impression_if_prefill(
                store, conversation_id, agent_id, payload, partial_text=partial,
            )
        return payload

    # ─── Phase 37: 下一步动作推荐 + 自定义动作/工作链 ───────────────────

    @app.get("/api/workspace/conv/{conversation_id}/next-actions")
    async def api_conv_next_actions(conversation_id: str, request: Request):
        """AA1：推荐当前会话下一步动作（内置场景动作 + 用户自定义）。

        Query 参数可传入会话上下文加速推荐（否则从 store 自动拉取）：
          silence_hours, message_count, churn_risk_level
        """
        api_auth(request)
        store = _inbox_store(request)
        from src.inbox.next_action_recommender import NextActionRecommender

        # 拉取最新消息（用于信号检测）
        last_msg_text = ""
        last_msg_direction = "in"
        message_count = 0
        silence_hours = 0.0
        churn_risk_level = ""
        risk_signals: List[Dict[str, Any]] = []

        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
                if rows:
                    message_count = store._conn.execute(
                        "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()["c"]
                    last = rows[0]
                    last_msg_text = str(last["text"] or "")
                    last_msg_direction = str(last["direction"] or "in")
                    import time as _t
                    silence_hours = max(0.0, (_t.time() - float(last["ts"] or 0)) / 3600)

                # 读取流失风险
                meta = store.get_conv_meta(conversation_id) or {}
                churn_raw = str(meta.get("churn_risk") or "").strip()
                if churn_raw:
                    import json as _j
                    cd = _j.loads(churn_raw)
                    churn_risk_level = str(cd.get("level") or "")
            except Exception:
                logger.debug("next-actions 上下文拉取失败（已忽略）", exc_info=True)

        # 拉取自定义动作（已启用）
        custom_actions: List[Dict[str, Any]] = []
        if store is not None:
            try:
                raw = store.list_workflow_actions()
                for act in raw:
                    import json as _j
                    try:
                        cfg = _j.loads(act.get("config_json") or "{}")
                    except Exception:
                        cfg = {}
                    try:
                        triggers = _j.loads(act.get("trigger_conditions") or '["any"]')
                    except Exception:
                        triggers = ["any"]
                    custom_actions.append({**act, "config": cfg, "trigger_conditions": triggers})
            except Exception:
                pass

        rec = NextActionRecommender()
        actions = rec.recommend(
            risk_signals=risk_signals,
            last_msg_text=last_msg_text,
            last_msg_direction=last_msg_direction,
            message_count=message_count,
            silence_hours=silence_hours,
            churn_risk_level=churn_risk_level,
            custom_actions=custom_actions,
            limit=6,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "actions": actions,
            "context": {
                "message_count": message_count,
                "silence_hours": round(silence_hours, 1),
                "churn_risk_level": churn_risk_level,
                "last_direction": last_msg_direction,
            },
        }

    @app.post("/api/workspace/conv/{conversation_id}/execute-action")
    async def api_conv_execute_action(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：执行一个动作（发话术/创建任务/打标签/启动工作链）。"""
        body = await request.json()
        action_type = str(body.get("action_type") or "")
        config = body.get("config") or {}
        store = _inbox_store(request)
        import time as _t
        now = _t.time()
        result: Dict[str, Any] = {"ok": True, "action_type": action_type}

        if action_type == "task":
            # 创建跟进任务
            due_hours = float(config.get("due_hours") or 72)
            note = str(config.get("note") or "")
            contacts_store = _contacts_store(request)
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            if contacts_store:
                try:
                    meta = store.get_conv_meta(conversation_id) if store else {}
                    contact_id = (meta or {}).get("contact_id", "")
                    if contact_id:
                        contacts_store.add_follow_up_task(
                            contact_id, now + due_hours * 3600, note=note, assignee=agent_id
                        )
                        result["task_created"] = True
                except Exception:
                    pass

        elif action_type == "tag":
            # 添加标签
            tag = str(config.get("tag") or "")
            if tag and store:
                try:
                    existing_tags = store.get_conv_tags(conversation_id)
                    if tag not in existing_tags:
                        store.set_conv_tags(conversation_id, existing_tags + [tag])
                    result["tag"] = tag
                except Exception:
                    pass

        elif action_type == "note":
            # 添加内部注解
            body_text = str(config.get("note_body") or config.get("hint") or "")
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            agent_name = request.session.get("display_name") or agent_id
            if body_text and store:
                try:
                    store.add_conv_note(
                        conversation_id, body_text,
                        agent_id=agent_id, agent_name=agent_name,
                    )
                    result["note_added"] = True
                except Exception:
                    pass

        elif action_type == "chain":
            # 启动工作链
            chain_id = str(config.get("chain_id") or "")
            if chain_id and store:
                try:
                    exec_id = store.start_chain_execution(
                        chain_id, conversation_id,
                        {"agent": request.session.get("username")},
                        schedule_first_step=True,
                    )
                    result["exec_id"] = exec_id
                except Exception:
                    pass

        elif action_type == "escalate":
            # 发布升级事件
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("escalation", {
                    "conversation_id": conversation_id,
                    "reason": str(config.get("reason") or "human_escalate"),
                    "initiated_by": request.session.get("username") or "",
                    "ts": now,
                })
                result["escalated"] = True
            except Exception:
                pass

        return result

    # ── 自定义动作管理 ────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-actions")
    async def api_workflow_actions_list(request: Request):
        """AA1：列出所有自定义动作。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "actions": []}
        actions = store.list_workflow_actions()
        import json as _j
        for a in actions:
            try:
                a["config"] = _j.loads(a.get("config_json") or "{}")
            except Exception:
                a["config"] = {}
        return {"ok": True, "actions": actions}

    @app.post("/api/workspace/workflow-actions")
    async def api_workflow_actions_create(request: Request, _=Depends(api_auth)):
        """AA1：创建自定义动作。"""
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        action_id = store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.put("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_update(action_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["action_id"] = action_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.delete("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_delete(action_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_action(action_id)
        return {"ok": ok}

    # ── 工作链管理 ────────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-chains")
    async def api_workflow_chains_list(request: Request):
        """AA1：列出所有工作链。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "chains": []}
        import json as _j
        chains = store.list_workflow_chains()
        for c in chains:
            try:
                c["steps"] = _j.loads(c.get("steps_json") or "[]")
            except Exception:
                c["steps"] = []
            try:
                c["trigger_conditions"] = _j.loads(c.get("trigger_conditions") or "{}")
            except Exception:
                c["trigger_conditions"] = {}
        return {"ok": True, "chains": chains}

    @app.post("/api/workspace/workflow-chains")
    async def api_workflow_chains_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        chain_id = store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.put("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_update(chain_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["chain_id"] = chain_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.delete("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_delete(chain_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_chain(chain_id)
        return {"ok": ok}

    # ─── Phase 47: 工作链执行可视化 ─────────────────────────────────────

    @app.get("/api/workspace/chain-executions")
    async def api_chain_executions_list(
        request: Request,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ):
        """P47：全局工作链执行监控列表。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "count": 0}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            status=status.strip(), conversation_id=conversation_id.strip(), limit=limit,
        )
        enriched = enrich_executions(rows)
        running = sum(1 for e in enriched if e.get("status") == "running")
        return {
            "ok": True,
            "executions": enriched,
            "count": len(enriched),
            "running_count": running,
        }

    @app.get("/api/workspace/conv/{conversation_id}/chain-executions")
    async def api_conv_chain_executions(
        conversation_id: str, request: Request, status: str = "", limit: int = 20,
    ):
        """P47：会话级工作链执行记录。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "conversation_id": conversation_id}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            conversation_id=conversation_id, status=status.strip(), limit=limit,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "executions": enrich_executions(rows),
            "count": len(rows),
        }

    @app.post("/api/workspace/chain-executions/{exec_id}/cancel")
    async def api_cancel_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """P47：取消运行中的工作链执行。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        ex = store.get_workflow_execution(exec_id)
        if not ex:
            raise HTTPException(404, "执行记录不存在")
        if ex.get("status") != "running":
            raise HTTPException(422, "仅可取消运行中的工作链")
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        reason = str(body.get("reason") or "坐席手动取消").strip()
        agent_id, agent_name = _agent_from_request(request)
        ok = store.cancel_workflow_execution(exec_id)
        if not ok:
            raise HTTPException(422, "取消失败")
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("workflow_execution_cancelled", {
                "exec_id": exec_id,
                "conversation_id": ex.get("conversation_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        from src.inbox.workflow_monitor import enrich_execution
        refreshed = store.get_workflow_execution(exec_id)
        return {
            "ok": True,
            "exec_id": exec_id,
            "execution": enrich_execution(refreshed or ex),
        }

    @app.post("/api/workspace/conv/{conversation_id}/start-chain")
    async def api_conv_start_chain(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：为会话启动指定工作链。"""
        body = await request.json()
        chain_id = str(body.get("chain_id") or "")
        store = _inbox_store(request)
        if not chain_id or store is None:
            return {"ok": False, "error": "缺少 chain_id"}
        if store.has_running_chain(conversation_id, chain_id):
            return {"ok": False, "error": "该会话已有同链运行中"}
        exec_id = store.start_chain_execution(
            chain_id, conversation_id,
            {"agent": request.session.get("username") or ""},
            schedule_first_step=True,
        )
        return {"ok": True, "exec_id": exec_id, "conversation_id": conversation_id}

    # ─── Phase 38: 分流路由规则引擎 ─────────────────────────────────────

    @app.get("/api/workspace/routing-rules")
    async def api_routing_rules_list(request: Request):
        """BB1：列出所有分流路由规则（按优先级降序）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "rules": []}
        import json as _j
        rules = store.list_routing_rules()
        for r in rules:
            try:
                r["conditions"] = _j.loads(r.get("conditions") or "{}")
            except Exception:
                r["conditions"] = {}
        return {"ok": True, "rules": rules}

    @app.post("/api/workspace/routing-rules")
    async def api_routing_rules_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        rule_id = store.upsert_routing_rule(body)
        return {"ok": True, "rule_id": rule_id}

    @app.put("/api/workspace/routing-rules/{rule_id}")
    async def api_routing_rules_update(rule_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["rule_id"] = rule_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_routing_rule(body)
        return {"ok": True, "rule_id": rule_id}

    @app.delete("/api/workspace/routing-rules/{rule_id}")
    async def api_routing_rules_delete(rule_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_routing_rule(rule_id)
        return {"ok": ok}

    @app.post("/api/workspace/routing-rules/evaluate")
    async def api_routing_rules_evaluate(request: Request, _=Depends(api_auth)):
        """BB1：对给定会话评估所有路由规则，返回命中的规则和分配目标。"""
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "matched": []}
        import json as _j
        rules = store.list_routing_rules()
        conversation = body.get("conversation") or {}
        platform = str(conversation.get("platform") or "").lower()
        text = str(conversation.get("text") or "").lower()

        matched = []
        for rule in rules:
            if not rule.get("enabled"):
                continue
            try:
                conds = _j.loads(rule.get("conditions") or "{}")
            except Exception:
                conds = {}

            hit = False
            if conds.get("platform") and platform:
                if str(conds["platform"]).lower() == platform:
                    hit = True
            if conds.get("keyword") and text:
                if str(conds["keyword"]).lower() in text:
                    hit = True
            if not conds:
                hit = True  # 空条件 = 通配

            if hit:
                matched.append({
                    "rule_id": rule["rule_id"],
                    "name": rule["name"],
                    "assign_to": rule["assign_to"],
                    "priority": rule["priority"],
                })

        # 按优先级排序，取最高优先级命中
        matched.sort(key=lambda x: x["priority"], reverse=True)
        best = matched[0] if matched else None
        return {"ok": True, "matched": matched, "best_match": best}

    # ─── Phase 39: 全局跨资源搜索 ───────────────────────────────────────

    @app.get("/api/workspace/search")
    async def api_workspace_global_search(
        request: Request,
        q: str = "",
        types: str = "messages,contacts,notes",
        limit: int = 20,
    ):
        """CC1：全局搜索（消息/联系人/注解，结果合并按相关度排序）。"""
        api_auth(request)
        q = str(q or "").strip()
        if not q or len(q) < 2:
            return {"ok": True, "q": q, "results": [], "total": 0}
        limit = max(1, min(50, int(limit or 20)))
        search_types = set(str(types or "").split(","))
        store = _inbox_store(request)
        results: List[Dict[str, Any]] = []

        if store is not None:
            # 1. 消息搜索（FTS5 优先）
            if "messages" in search_types:
                try:
                    msg_results = store.search_messages(q, limit=limit)
                    for m in msg_results:
                        results.append({
                            "type": "message",
                            "icon": "💬",
                            "title": str(m.get("display_name") or m.get("conversation_id") or ""),
                            "preview": str(m.get("text") or "")[:100],
                            "ts": m.get("ts"),
                            "conversation_id": m.get("conversation_id"),
                            "platform": m.get("platform", ""),
                            "url": f"/workspace?focus={m.get('conversation_id', '')}",
                        })
                except Exception:
                    logger.debug("global search 消息搜索失败", exc_info=True)

            # 2. 注解搜索
            if "notes" in search_types:
                try:
                    with store._lock:
                        note_rows = store._conn.execute(
                            """SELECT n.note_id, n.conversation_id, n.body, n.ts, n.agent_name,
                                      c.display_name, c.platform
                               FROM conv_notes n
                               LEFT JOIN conversations c ON c.conversation_id = n.conversation_id
                               WHERE n.body LIKE ?
                               ORDER BY n.ts DESC LIMIT ?""",
                            (f"%{q}%", limit),
                        ).fetchall()
                    for r in note_rows:
                        results.append({
                            "type": "note",
                            "icon": "📝",
                            "title": f"注解 · {r['display_name'] or r['conversation_id']}",
                            "preview": str(r["body"] or "")[:100],
                            "ts": r["ts"],
                            "conversation_id": r["conversation_id"],
                            "platform": r.get("platform", ""),
                            "url": f"/workspace?focus={r['conversation_id']}",
                        })
                except Exception:
                    logger.debug("global search 注解搜索失败", exc_info=True)

        # 3. 联系人搜索
        if "contacts" in search_types:
            contacts_store = _contacts_store(request)
            if contacts_store is not None:
                try:
                    contacts, _ = contacts_store.list_contacts_overview(q=q, limit=limit)
                    for c in contacts:
                        results.append({
                            "type": "contact",
                            "icon": "👤",
                            "title": str(c.get("primary_name") or c.get("contact_id") or ""),
                            "preview": " / ".join(c.get("channels") or []),
                            "ts": c.get("last_seen_ts") or c.get("created_at"),
                            "contact_id": c.get("contact_id"),
                            "url": f"/workspace/contact/{c.get('contact_id', '')}",
                        })
                except Exception:
                    logger.debug("global search 联系人搜索失败", exc_info=True)

        # 全局按 ts 降序，截断
        results.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
        results = results[:limit]
        return {"ok": True, "q": q, "results": results, "total": len(results)}

    # ─── Phase 34: QA 质检评分 ───────────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/qa-score")
    async def api_conv_qa_score_get(conversation_id: str, request: Request):
        """Y1：读取已存储的质检评分（不触发重新计算）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        result = store.get_qa_score(conversation_id)
        if result is None:
            return {"ok": True, "conversation_id": conversation_id, "qa": None, "computed": False}
        return {"ok": True, "conversation_id": conversation_id, "qa": result, "computed": True}

    @app.post("/api/workspace/conv/{conversation_id}/qa-score")
    async def api_conv_qa_score_compute(conversation_id: str, request: Request):
        """Y1：立即计算并存储质检评分（可在归档/关闭时调用）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        result = store.compute_and_store_qa_score(conversation_id)
        return {"ok": True, "conversation_id": conversation_id, "qa": result}

    @app.get("/api/workspace/agent-qa-stats")
    async def api_agent_qa_stats(request: Request, days: int = 30):
        """Y1：聚合各坐席最近 N 天的质检评分统计（团队看板）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "agents": []}
        days = max(1, min(90, int(days or 30)))
        stats = store.batch_agent_qa_stats(days=days)
        return {"ok": True, "days": days, "agents": stats, "count": len(stats)}

    # ─── Phase 35: 流失预警 ──────────────────────────────────────────────

    @app.get("/api/workspace/churn-risks")
    async def api_churn_risks(
        request: Request,
        silence_days: int = 7,
        limit: int = 50,
    ):
        """Z1：返回高/中流失风险会话列表（按风险分降序）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "items": []}
        from src.inbox.churn_predictor import ChurnPredictor

        silence_days = max(1, min(60, int(silence_days or 7)))
        limit = max(1, min(200, int(limit or 50)))

        # 拉取候选（沉默时间 ≥ silence_days 的未归档会话）
        candidates = store.list_churn_risk_conversations(
            silence_days=silence_days, limit=limit * 3
        )

        # 补全 last_dir（用于判断末条是否入站）
        if candidates:
            cids = [c["conversation_id"] for c in candidates if c.get("conversation_id")]
            last_dirs = store.last_message_dirs(cids)
            for c in candidates:
                cid = c["conversation_id"]
                info = last_dirs.get(cid, {})
                c["last_dir"] = info.get("direction", "in")
                c["last_text"] = info.get("text", "")

        results = ChurnPredictor().batch_predict(candidates, silence_threshold_days=silence_days)
        results = results[:limit]

        # 持久化高风险结果到 conversation_meta
        for r in results:
            if r["risk_level"] == "high":
                try:
                    store.store_churn_risk(
                        r["conversation_id"], r["risk_level"], r["reasons"]
                    )
                except Exception:
                    pass

        return {
            "ok": True,
            "silence_days": silence_days,
            "items": results,
            "high_count": sum(1 for r in results if r["risk_level"] == "high"),
            "medium_count": sum(1 for r in results if r["risk_level"] == "medium"),
        }

    @app.get("/api/workspace/activity-heatmap")
    async def api_workspace_activity_heatmap(
        request: Request,
        days: int = 30,
        platform: str = "",
        direction: str = "inbound",
    ):
        """W1：获取最近 N 天消息量按星期×小时的分布矩阵（用于热力图可视化）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        days = max(1, min(365, int(days or 30)))
        data = store.activity_heatmap(days=days, platform=str(platform or ""), direction=str(direction or "inbound"))
        return {"ok": True, **data}
