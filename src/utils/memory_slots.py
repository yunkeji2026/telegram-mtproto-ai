"""R10 记忆槽位解析 —— 把一条事实归到"单值属性槽"，用于矛盾消解。

近义归并（R5）处理"同一件事的不同说法"，但还有一类是**相互冲突**的事实：
旧"住在北京" vs 新"住在上海"、旧"单身" vs 新"有对象了"、旧"喜欢猫" vs 新"讨厌猫"。
这些不能并（会丢信息）、也不能都留（AI 会自相矛盾），应**按新近择一、旧值标历史**。

本模块把事实解析成 ``(slot_key, value, polarity)``：
- 单值身份槽（``name`` / ``residence`` / ``relationship``）：同槽不同 value = 冲突；
- 偏好极性槽（``pref:对象``）：同对象相反 polarity = 冲突。

纯函数、平台无关、可单测。解析保守——宁可不归槽（漏判），不乱归槽（误杀好记忆）。
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

SLOT_NAME = "name"
SLOT_RESIDENCE = "residence"
SLOT_RELATIONSHIP = "relationship"

# 关系状态：关键词 → 规范值（单值：当前只可能处于其一）
_REL_MAP = [
    (re.compile(r"已婚|结婚了|领了?证|结过婚"), "married"),
    (re.compile(r"离婚|离了婚"), "divorced"),
    (re.compile(r"有(男朋友|女朋友|对象|男友|女友|伴侣)|谈恋爱|恋爱了|在一起了|脱单"), "partnered"),
    (re.compile(r"分手了?|刚分手"), "single"),
    (re.compile(r"单身|没有对象|没对象|还单着|目前单身"), "single"),
]

# 称呼/名字（含 heuristic 模板与自由表达）
_NAME_PATTERNS = [
    re.compile(r"称呼\s*TA\s*[：:]\s*(?P<v>[^\s，。！？、\n]{1,16})"),
    re.compile(r"用户自称\s*[：:]\s*(?P<v>[^\s，。！？、\n]{1,20})"),
    re.compile(r"(?:以后)?叫我\s*(?P<v>[^\s，。！？、\n]{1,16})"),
    re.compile(r"(?i)be\s+called\s*[:：]?\s*(?P<v>[A-Za-z][A-Za-z '.-]{0,24})"),
    re.compile(r"(?i)name\s*\(EN\)\s*[:：]\s*(?P<v>[A-Za-z][A-Za-z '.-]{0,24})"),
]

# 居住地（自由表达；value 取地名）
_RESIDENCE_PATTERNS = [
    re.compile(
        r"(?:现在)?(?:住在|住|家在|定居在?|搬到了?|搬去了?|居住在?)\s*"
        r"(?P<v>[\u4e00-\u9fa5]{2,8}|[A-Za-z][A-Za-z ]{1,20})"
    ),
]

# 偏好极性：先否定后肯定（避免"不喜欢"被肯定式吞掉）
_PREF_NEG = re.compile(r"(?:表示不喜欢\s*[：:]\s*|不喜欢|讨厌|不爱|很烦|受不了|恨)\s*(?P<v>[^\s，。！？、\n]{1,20})")
_PREF_POS = re.compile(r"(?<![不没])(?:喜欢|喜爱|超爱|爱吃|爱喝|爱)\s*(?P<v>[^\s，。！？、\n]{1,20})")

# 居住地误归保护：这些动词后接的不是地名（"住院/住手"等）
_RESIDENCE_BLOCK = re.compile(r"^(院|手|口|嘴|不|了)$")


def _norm_value(v: str) -> str:
    v = (v or "").strip().strip("：:，。！？、.!?").strip()
    return v.lower()


def _clean_place(v: str) -> str:
    # 去掉常见后缀，使"北京"/"北京市"可比
    v = (v or "").strip()
    v = re.sub(r"(市|区|县|省|那边|这边|那里|这里)$", "", v)
    return _norm_value(v)


def extract_slot(text: str) -> Optional[Tuple[str, str, int]]:
    """把一条事实解析为 ``(slot_key, value, polarity)``；无法归槽返回 None。

    polarity 仅偏好槽用（+1 喜欢 / -1 不喜欢），其余为 0。
    """
    t = (text or "").strip()
    if len(t) < 2:
        return None

    for pat in _NAME_PATTERNS:
        m = pat.search(t)
        if m:
            val = _norm_value(m.group("v"))
            if val:
                return (SLOT_NAME, val, 0)

    for pat in _REL_MAP:
        if pat[0].search(t):
            return (SLOT_RELATIONSHIP, pat[1], 0)

    for pat in _RESIDENCE_PATTERNS:
        m = pat.search(t)
        if m:
            place = _clean_place(m.group("v"))
            if place and not _RESIDENCE_BLOCK.match(place):
                return (SLOT_RESIDENCE, place, 0)

    mneg = _PREF_NEG.search(t)
    if mneg:
        obj = _norm_value(mneg.group("v"))
        if obj:
            return (f"pref:{obj}", "", -1)
    mpos = _PREF_POS.search(t)
    if mpos:
        obj = _norm_value(mpos.group("v"))
        if obj:
            return (f"pref:{obj}", "", 1)

    return None


def slots_conflict(
    a: Tuple[str, str, int], b: Tuple[str, str, int]
) -> bool:
    """两槽是否矛盾：同身份槽不同值，或同偏好对象相反极性。"""
    if a[0] != b[0]:
        return False
    if a[0].startswith("pref:"):
        return a[2] != b[2] and a[2] != 0 and b[2] != 0
    return a[1] != b[1]


__all__ = [
    "extract_slot",
    "slots_conflict",
    "SLOT_NAME",
    "SLOT_RESIDENCE",
    "SLOT_RELATIONSHIP",
]
