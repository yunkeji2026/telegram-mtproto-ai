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

    只在**最没话说的 bland 开场**（``gentle_checkin``，即无可回访记忆的温和问候）上升级——
    有记忆钩子时回访记忆更有价值，不打断。其余门槛：关系够深、生日未知、距上次问足够久。
    """
    if str(opener_mode or "") != "gentle_checkin":
        return False
    if birthday_known:
        return False
    try:
        if float(intimacy) < float(min_intimacy):
            return False
    except (TypeError, ValueError):
        return False
    try:
        last = float(last_ask_ts or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last > 0 and (float(now) - last) < float(cooldown_days) * 86400.0:
        return False
    return True


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


__all__ = ["extract_birthday", "is_birthday_today", "should_ask_birthday"]
