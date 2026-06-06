"""
Q2 — 草稿质量评分

纯规则 / 启发式质量评分（0-100），无 LLM 调用，亚毫秒级。
评分维度：
  1. 长度适中 (0-25分)：太短(<15字)或太长(>500字)扣分
  2. 礼貌性 (0-20分)：包含您好/感谢/请/谢谢等礼貌词
  3. 完整性 (0-20分)：有实质内容（非纯表情/数字），句子结构合理
  4. 语言一致性 (0-20分)：回复语言与客户来文语言一致
  5. 风险匹配 (0-15分)：高风险草稿但回复无安抚词时扣分

质量等级映射：
  ≥80 🟢 优秀  ≥60 🟡 良好  ≥40 🟠 一般  <40 🔴 待改进
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

# ── 礼貌词典（中文 / 英文 / 混用）─────────────────────────────────
_POLITE_ZH = ["您好", "感谢", "谢谢", "请问", "请稍", "非常抱歉", "请放心", "感谢您", "祝您"]
_POLITE_EN = ["thank", "please", "sorry", "apologize", "appreciate", "welcome", "certainly"]

# ── 安抚词（用于高风险回复检测）──────────────────────────────────
_SOOTHE = ["理解", "抱歉", "了解", "放心", "帮您", "核实", "处理", "解决", "道歉",
           "sorry", "understand", "resolve", "apologize"]

# ── 无效回复特征 ──────────────────────────────────────────────────
_INVALID_PATTERNS = [
    r"^[？?！!。.…]+$",          # 纯标点
    r"^[\d\s]+$",                # 纯数字/空格
    r"^[😀-🙏]{1,3}$",          # 纯表情
]


def _count_cjk(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def _detect_lang(text: str) -> str:
    """轻量语言检测（ZH/EN/OTHER）。"""
    cjk = _count_cjk(text)
    total = len(text.strip())
    if total == 0:
        return "UNKNOWN"
    return "ZH" if cjk / total > 0.3 else "EN"


def calculate_draft_quality(
    draft_text: str,
    peer_text: str = "",
    risk_level: str = "low",
    lang: str = "",
) -> Tuple[float, Dict[str, Any]]:
    """Q2：计算草稿质量分（0-100）及详细评分分解。

    返回：(score: float, breakdown: dict)
    breakdown 键：length, politeness, completeness, lang_match, risk_match, grade
    """
    text = str(draft_text or "").strip()
    peer = str(peer_text or "").strip()

    if not text:
        return 0.0, {"length": 0, "politeness": 0, "completeness": 0,
                     "lang_match": 0, "risk_match": 0, "grade": "🔴 待改进"}

    # ── 1. 长度评分 (0-25) ─────────────────────────────────────────
    length = len(text)
    if length < 5:
        len_score = 0
    elif length < 15:
        len_score = 8
    elif length < 50:
        len_score = 16
    elif length <= 200:
        len_score = 25
    elif length <= 400:
        len_score = 18
    else:
        len_score = 10  # 太长扣分

    # ── 2. 礼貌性 (0-20) ──────────────────────────────────────────
    t_lower = text.lower()
    polite_hits = (
        sum(1 for w in _POLITE_ZH if w in text) +
        sum(1 for w in _POLITE_EN if w in t_lower)
    )
    polite_score = min(20, polite_hits * 8)

    # ── 3. 完整性 (0-20) ──────────────────────────────────────────
    # 检查是否为无效回复
    is_invalid = any(re.match(p, text) for p in _INVALID_PATTERNS)
    if is_invalid:
        complete_score = 0
    else:
        # 有多个句子/标点 → 内容丰富
        sentences = re.split(r"[。！？.!?\n]", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 1]
        if len(sentences) >= 3:
            complete_score = 20
        elif len(sentences) >= 2:
            complete_score = 14
        elif len(text) > 10:
            complete_score = 8
        else:
            complete_score = 4

    # ── 4. 语言一致性 (0-20) ──────────────────────────────────────
    if peer:
        peer_lang = lang if lang else _detect_lang(peer)
        draft_lang = _detect_lang(text)
        if peer_lang == "UNKNOWN" or draft_lang == "UNKNOWN":
            lang_score = 10  # 无法判断，给中性分
        elif peer_lang == draft_lang:
            lang_score = 20
        else:
            lang_score = 4  # 语言不一致扣分
    else:
        lang_score = 10  # 无来文，给中性分

    # ── 5. 风险匹配 (0-15) ────────────────────────────────────────
    if risk_level in ("high", "critical"):
        soothe_hits = sum(1 for w in _SOOTHE if w in text or w in t_lower)
        if soothe_hits >= 2:
            risk_score = 15
        elif soothe_hits == 1:
            risk_score = 8
        else:
            risk_score = 2  # 高风险但无安抚语，扣分
    else:
        risk_score = 15  # 非高风险，满分

    total = len_score + polite_score + complete_score + lang_score + risk_score
    total = round(min(100.0, max(0.0, float(total))), 1)

    grade = (
        "🟢 优秀" if total >= 80 else
        "🟡 良好" if total >= 60 else
        "🟠 一般" if total >= 40 else
        "🔴 待改进"
    )

    return total, {
        "length": len_score,
        "politeness": polite_score,
        "completeness": complete_score,
        "lang_match": lang_score,
        "risk_match": risk_score,
        "grade": grade,
    }


def quality_to_badge(score: float) -> str:
    """将质量分转为 HTML badge 字符串（供 Copilot / draft_review 显示）。"""
    if score >= 80:
        color, label = "#16a34a", f"🟢 {score:.0f}"
    elif score >= 60:
        color, label = "#d97706", f"🟡 {score:.0f}"
    elif score >= 40:
        color, label = "#ea580c", f"🟠 {score:.0f}"
    else:
        color, label = "#dc2626", f"🔴 {score:.0f}"
    return (
        f'<span style="display:inline-block;padding:1px 7px;border-radius:10px;'
        f'background:{color}22;color:{color};font-size:11px;font-weight:700;'
        f'border:1px solid {color}44;">{label}</span>'
    )
