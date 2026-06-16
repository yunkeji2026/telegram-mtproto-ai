"""统一收件箱——纯工具 helper（巨石拆分 slice 1）。

从 ``unified_inbox_routes.py`` 抽出的**无状态**辅助函数：不依赖 ``request`` /
``app.state``，只吃显式参数 + 本模块常量 + 全局 ``detect_language``。

放在独立模块的目的：给 8000+ 行的 routes 巨石做第一刀减负，且这些函数本就可独立
单测（``_detect_language`` / ``_dnd_active`` 已有外部测试直接 import）。routes.py
通过 ``from .unified_inbox_helpers import *`` 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.ai.translation_service import detect_language

# 会话自动化模式取值域（slice 7 从 routes 下沉，供 routes 与 aggregate 共享）
AUTOMATION_MODES = {"manual", "review", "multi_choice", "auto_ai"}

# ─── 展示标签常量（slice 5 从 routes 下沉，供 routes 与 context 共享） ────────
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

# ─── P33：语种检测关键词（拉丁含糊文本打分） ──────────────────────────────
_ID_KEYWORDS = {"anda", "saya", "ini", "itu", "tidak", "dengan", "untuk", "yang",
                "bisa", "kami", "harga", "mau", "sudah", "belum", "tolong"}
_EN_KEYWORDS = {"the", "is", "are", "was", "were", "have", "has", "you", "your",
                "please", "sorry", "thank", "hello", "hi", "can", "how", "what"}

# ─── P30-A：规则驱动风险信号词典（零 LLM 消耗） ───────────────────────────
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

# ─── P33：多语言话术模板（缓和前缀 / 主动 CTA / 档位标签） ─────────────────
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
