"""生日抽取与当日判定（确定性纯函数，可单测）——Stage Q 生日仪式的取数底座。

生日是单用户**最高情感价值**的仪式节点，但前提是**可靠地知道 TA 的生日**。本模块从一条
记忆文本里保守地抽出 (月, 日)：**必须出现「生日/出生/birthday/born」等关键词**才解析日期，
避免把闲聊里的随便一个日期（如「3 月 5 日开会」）误当生日。只取月日（按年循环庆祝，忽略出生年）。

支持：
- 中文：「生日是 3 月 5 日 / 生日：3月5号 / 我生日 03-05 / 出生于 1995-03-05」
- 英文：「birthday is March 5 / born on 3/5」

保守原则：宁可漏判（不发），不误判（别在错的日子说生日快乐——比不发更尴尬）。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional, Tuple

# 必须命中其一才解析（否则不认为这条记忆在说生日）
_BDAY_KW = re.compile(r"生日|出生|诞辰|birth\s*day|born", re.IGNORECASE)

_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 1) 中文「X月Y日/号」（最可靠）
_CN_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 2) 含年的 YYYY-MM-DD（取月日）
_YMD = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
# 3) 裸 MM-DD / MM/DD（无年）
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
# 4) 英文月份名 + 日
_EN_MD = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})",
    re.IGNORECASE,
)


def _valid(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= 31


def extract_birthday(text: Any) -> Optional[Tuple[int, int]]:
    """从一条记忆文本抽 (月, 日)；无生日关键词或解析不出合法月日 → None。"""
    t = str(text or "").strip()
    if len(t) < 2 or not _BDAY_KW.search(t):
        return None

    m = _CN_MD.search(t)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if _valid(mo, da):
            return (mo, da)

    m = _YMD.search(t)
    if m:
        mo, da = int(m.group(2)), int(m.group(3))
        if _valid(mo, da):
            return (mo, da)

    m = _MD.search(t)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if _valid(mo, da):
            return (mo, da)

    m = _EN_MD.search(t)
    if m:
        mo = _EN_MONTHS.get(m.group(1).lower()[:3], 0)
        da = int(m.group(2))
        if _valid(mo, da):
            return (mo, da)

    return None


def should_ask_birthday(
    *,
    opener_mode: str,
    intimacy: float,
    min_intimacy: float,
    birthday_known: bool,
    last_ask_ts: float,
    now: float,
    cooldown_days: float,
) -> bool:
    """是否该借这次主动开场顺势问 TA 生日（确定性纯函数，Stage R）。

    Stage T：生日只是通用画像采集的一个槽位，逻辑下沉到 ``profile_collect.should_ask_profile_slot``，
    本函数保留为兼容入口（生日 = slot ``birthday``）。
    """
    from src.utils.profile_collect import should_ask_profile_slot
    return should_ask_profile_slot(
        opener_mode=opener_mode, intimacy=intimacy, min_intimacy=min_intimacy,
        slot_known=birthday_known, last_ask_ts=last_ask_ts, now=now,
        cooldown_days=cooldown_days)


def birthday_from_turn(user_msg: Any, reply: Any) -> Optional[Tuple[int, int]]:
    """从一轮对话（用户消息 + AI 回复）里抽生日 (月,日)，闭合「问→答→记」（Stage S）。

    两路自包含、无需跨边界状态：
    1. **用户原话**含生日（"我生日是3月5日"）→ 直接抽。
    2. **AI 回复**含生日确认（"记住啦，你3月5号生日"）→ 抽 AI 回复——这覆盖了用户只回一个
       裸日期（"3月5号"，无关键词不被路1命中）、而 AI 在本轮自然复述确认的情况。

    两路都要求**生日关键词**：AI 的「提问」回复（"你生日哪天呀？"）无日期 → 不会误抽；
    只有「确认」回复（带日期）才命中——天然区分问 vs 答，零误报。
    """
    bd = extract_birthday(user_msg)
    if bd is not None:
        return bd
    return extract_birthday(reply)


def birthday_fact_text(month: int, day: int) -> str:
    """规范化生日记忆文案：``用户的生日：M月D日``（含关键词，可被 extract_birthday 复解析）。"""
    return f"用户的生日：{int(month)}月{int(day)}日"


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def is_birthday_today(birthday: Any, now: float) -> bool:
    """今天（本地时区）是否是该生日。2月29生日在平年顺延到2月28庆祝。"""
    if not isinstance(birthday, (tuple, list)) or len(birthday) < 2:
        return False
    try:
        bmo, bday = int(birthday[0]), int(birthday[1])
    except (TypeError, ValueError):
        return False
    if not _valid(bmo, bday):
        return False
    lt = time.localtime(now)
    if lt.tm_mon == bmo and lt.tm_mday == bday:
        return True
    # 闰日生日（2/29）在平年顺延到 2/28
    if bmo == 2 and bday == 29 and not _is_leap(lt.tm_year):
        return lt.tm_mon == 2 and lt.tm_mday == 28
    return False


__all__ = [
    "extract_birthday",
    "is_birthday_today",
    "should_ask_birthday",
    "birthday_from_turn",
    "birthday_fact_text",
]
