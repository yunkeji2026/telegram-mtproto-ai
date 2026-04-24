"""
多语种寒暄词：用于意图识别（greeting）与 narrow_reply 放行。
含业务词（通道/订单等）时不视为纯寒暄，避免抢占 channel_info。
"""

from __future__ import annotations

import re
from typing import Any, List, Sequence

# 仅「在」+ 可选句读，表示「在不在/客服在吗」；与「现在」「在吗」等区分
_ZAI_STANDALONE_PATTERN = re.compile(
    r"^在\s*[。！？，、;；:：,.!?…~～]*\s*$",
)
# 与「在」连用则不是「一字在」问客服
_ZAI_MIXED_MARKERS = ("现在", "正在", "在于", "在线", "在吗", "在么", "在不", "在不在")


def is_standalone_zai_query(text: str) -> bool:
    """
    用户只发「在」或「在。」等，表示问客服在不在。
    与「现在」「在吗」「在线」等整句业务/时间语义区分（整句匹配，不含其它汉字）。
    """
    t = (text or "").strip()
    if not t or len(t) > 10:
        return False
    for m in _ZAI_MIXED_MARKERS:
        if m in t:
            return False
    if not _ZAI_STANDALONE_PATTERN.match(t):
        return False
    if "在" not in t:
        return False
    return True


# 整句即寒暄（小写 / 原文）
_EXACT_ONE_LINE = frozenset({
    "hi", "hey", "yo", "gm", "gn", "hola", "hallo", "ciao", "cya", "salut", "moin",
    "你好", "您好", "哈喽", "嗨喽", "嗨", "在吗", "在？", "在么", "早", "晚",
    "hello", "hi.", "hey.", "yo.", "sup", "wassup",
})

# 子串匹配（长短语放前）
_GREETING_PHRASES: tuple[str, ...] = (
    "good morning", "good afternoon", "good evening", "good night", "good day",
    "what's up", "whats up", "how are you", "howdy", "nice to meet",
    "buenos dias", "buenas tardes", "buenas noches", "buenas",
    "bom dia", "boa tarde", "boa noite",
    "guten tag", "guten morgen", "guten abend",
    "assalamualaikum", "assalamu alaikum",
    "مرحبا", "السلام عليكم",
    "bonjour", "bonsoir", "coucou",
    "merhaba", "selam",
    "privet", "zdravstvuyte",
    "konnichiwa", "ohayou", "ohayo",
    "annyeong", "annyeonghaseyo",
    "สวัสดี",
    "xin chào", "xin chao",
    "你好呀", "您好呀", "哈喽呀", "嗨喽呀", "早上好", "下午好", "晚上好", "晚安",
)


def _has_business_context(text: str) -> bool:
    raw = text or ""
    for x in (
        "通道", "订单", "单号", "成功率", "额度", "限额", "代收", "代付", "费率", "手续费",
        "查询", "查单", "查订单", "投诉", "退款", "凭证", "截图", "回调", "到账", "维护", "波动",
    ):
        if x in raw:
            return True
    u = f" {text.lower()} "
    return bool(
        re.search(
            r"\b(ep|jc|jazz|easypaisa|easypay|pix|order|orders|channel|channels|refund|payment|status|fee|rate|rates|query|inquiry)\b",
            u,
            re.I,
        )
    )


def is_greeting_message(text: str, *, max_len: int = 56) -> bool:
    """
    是否主要为寒暄问候（用于意图=greeting）。
    过长或含业务词时返回 False。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > max_len:
        return False
    if _has_business_context(raw):
        return False
    tl = raw.lower()
    if is_standalone_zai_query(raw):
        return True
    tl_compact = re.sub(r"[\s！!。.，,?？]+$", "", tl)
    tl_compact = re.sub(r"^[\s！!。.，,?？]+", "", tl_compact)
    if tl_compact in _EXACT_ONE_LINE or tl in _EXACT_ONE_LINE:
        return True
    for ph in _GREETING_PHRASES:
        if ph in tl or ph in raw:
            return True
    parts = tl.split()
    if len(parts) == 1 and len(tl) <= 6 and tl in _EXACT_ONE_LINE:
        return True
    if parts and len(raw) <= 22:
        first = parts[0].strip(".,!?！？。…")
        if first in (
            "hi", "hey", "yo", "gm", "hola", "hallo", "ciao", "salut", "sup", "moin",
            "ola", "coucou",
        ):
            return True
    for w in ("哈喽", "嗨喽", "你好", "您好"):
        if w in raw:
            return True
    return False


def merge_greeting_substrings(cfg_list: Sequence[Any]) -> List[str]:
    """配置中的 greeting_substrings + 内置短语，去重保持顺序。"""
    out: List[str] = []
    seen: set[str] = set()
    for group in (list(cfg_list or []), list(_GREETING_PHRASES), list(_EXACT_ONE_LINE)):
        for x in group:
            s = (x or "").strip()
            if not s:
                continue
            key = s.lower()
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out
