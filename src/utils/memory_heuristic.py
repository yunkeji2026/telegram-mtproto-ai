"""
Rule-based memory snippets from a single user message (no LLM).
"""

from __future__ import annotations

import re
from typing import List


def extract_heuristic_facts(text: str) -> List[str]:
    """Return 0+ short memory strings worth storing."""
    if not text or not isinstance(text, str):
        return []
    t = text.strip()
    if len(t) < 2:
        return []
    out: List[str] = []

    # Chinese: 叫我 / 称呼
    m = re.search(r"叫我\s*([^\s，。！？\n]{1,16})", t)
    if m:
        out.append(f"用户希望我称呼 TA：{m.group(1).strip()}")

    m = re.search(r"我是\s*([^\s，。！？\n]{1,20})", t)
    if m:
        out.append(f"用户自称：{m.group(1).strip()}")

    m = re.search(r"我不喜欢\s*([^。！？\n]{1,40})", t)
    if m:
        out.append(f"用户表示不喜欢：{m.group(1).strip()[:40]}")

    m = re.search(r"记住[：:]\s*([^。！？\n]{2,80})", t)
    if m:
        out.append(f"用户请我记得：{m.group(1).strip()[:80]}")

    # English
    m = re.search(r"(?i)call me\s+([A-Za-z][A-Za-z\s'.-]{0,24})", t)
    if m:
        out.append(f"User asked to be called: {m.group(1).strip()}")

    m = re.search(r"(?i)my name is\s+([A-Za-z][A-Za-z\s'.-]{0,24})", t)
    if m:
        out.append(f"User's name (EN): {m.group(1).strip()}")

    # Dedupe while preserving order
    seen = set()
    uniq: List[str] = []
    for x in out:
        k = x[:120]
        if k not in seen:
            seen.add(k)
            uniq.append(x[:500])
    return uniq


def matches_forget_intent(text: str, phrases: List[str]) -> bool:
    """True if user is asking to clear bot-side memory."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    for p in phrases:
        p = (p or "").strip()
        if not p:
            continue
        if p.lower() in low or raw.startswith(p):
            return True
    # Regex fallbacks (Chinese / EN)
    if re.match(
        r"^(忘掉|忘记|清除|删除|清空)(你)?(记住|的)?(的)?(东西|内容|话|记忆)?",
        raw,
    ):
        return True
    if re.match(
        r"(?i)^(forget|clear)\s+(what|everything|my)",
        low,
    ):
        return True
    return False
