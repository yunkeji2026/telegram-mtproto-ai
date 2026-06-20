"""P1 主动话题发起：从长期记忆挑一个"值得主动回访"的话题种子（确定性纯函数）。

陪伴型 AI 的差异化能力——沉默一段时间后，不是干巴巴"好久没聊"，而是自然回到对方
真正在意的事（"上次你说在备考，结果怎么样？"）。本模块只做**确定性选择 + 生成一行
prompt 指令**，真实文案由回复生成层产出；何时发由调度层决定（与 empathy_strategy
"选策略→注入一行指令"同构）。

设计取舍：
- **纯函数、零 IO、可单测**：不读 config、不调 LLM、不触网。
- **只回访高置信事实**：优先 ``user_stated`` / 已人工确认（R12/R15），**绝不拿
  ``ai_inferred`` 的"猜测"去回访**——猜错了一开口就尴尬，反噬陪伴信任。
- **排除 stale**：被 R10/R11 推翻的旧事实不再回访。
- **沉默不足不打扰**：活跃用户不主动插话；关系/记忆不足时退化为温和问候。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# 主动开场模式
MODE_NONE = ""                     # 不主动开场（沉默不足）
MODE_FOLLOW_UP = "follow_up"       # 回访某条高置信记忆
MODE_GENTLE_CHECKIN = "gentle_checkin"  # 无记忆钩子，温和问候

# 长别离阈值（超过则即便有记忆钩子也先柔和重连）
_LONG_ABSENCE_HOURS = 14 * 24

# P1b：除选中事实外，额外带几条高置信事实作"背景知识"（让开场更有"真记得你"
# 的质感，但只作背景、不罗列、不连环追问——见 directive 的克制约束）。
_DEFAULT_CONTEXT_FACTS = 2


def _eligible_facts(memory_facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛出可回访的高置信事实：非 stale、非 ai_inferred、有内容。"""
    out: List[Dict[str, Any]] = []
    for f in memory_facts or []:
        if not isinstance(f, dict):
            continue
        content = str(f.get("content") or "").strip()
        if not content:
            continue
        tier = str(f.get("tier") or "raw").strip().lower()
        if tier == "stale":
            continue
        # 缺省 source 视为 user_stated（兼容旧库）；ai_inferred 一律排除
        source = str(f.get("source") or "user_stated").strip().lower()
        if source == "ai_inferred":
            continue
        out.append(f)
    return out


def _fact_score(f: Dict[str, Any], now: float) -> tuple:
    """回访优先级：稳定层优先 → 复发多 → 越近越优先。返回可比较元组。"""
    tier = str(f.get("tier") or "raw").strip().lower()
    stable_bonus = 1 if tier == "stable" else 0
    try:
        hits = int(f.get("hits") or 1)
    except (TypeError, ValueError):
        hits = 1
    last_seen = f.get("last_seen") or f.get("created_at") or 0
    try:
        last_seen = float(last_seen)
    except (TypeError, ValueError):
        last_seen = 0.0
    return (stable_bonus, hits, last_seen)


def select_proactive_topic(
    memory_facts: List[Dict[str, Any]],
    *,
    silent_hours: float,
    stage: str = "",
    intimacy: float = 0.0,
    min_silent_hours: float = 24.0,
    max_context_facts: int = _DEFAULT_CONTEXT_FACTS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """选出一个主动开场话题种子（确定性）。

    Args:
        memory_facts: 该用户长期记忆条目，每项含 ``content`` 及可选 ``source/tier/hits/
            last_seen/created_at``（即 episodic ``list_rows`` 行）。
        silent_hours: 距上次互动的小时数。
        stage: 关系阶段（initial/warming/intimate/steady…），用于克制修饰。
        intimacy: 亲密度 0-100（预留）。
        min_silent_hours: 低于此沉默时长不主动开场（避免打扰活跃用户）。
        now: 注入"现在"时间戳（测试用）。

    Returns:
        ``{mode, fact, directive, context_facts, long_absence, silent_hours}``；
        ``mode==""`` 表示不开场。``context_facts`` 是除选中事实外的其他高置信事实
        （供回复层作背景，不罗列、不追问），无则为空 list。
    """
    now = now if now is not None else time.time()
    try:
        sh = float(silent_hours or 0)
    except (TypeError, ValueError):
        sh = -1.0
    empty = {
        "mode": MODE_NONE, "fact": "", "directive": "", "context_facts": [],
        "long_absence": False, "silent_hours": round(sh, 1) if sh >= 0 else 0.0,
    }
    if sh < 0:
        return empty
    if sh < float(min_silent_hours):
        return empty  # 沉默不足，不打扰

    long_absence = sh >= _LONG_ABSENCE_HOURS
    eligible = _eligible_facts(memory_facts)
    if eligible:
        best = max(eligible, key=lambda f: _fact_score(f, now))
        fact = str(best.get("content") or "").strip()
        # P1b：除选中事实外，再挑几条高置信事实作背景（按同一优先级排序，去重）。
        context_facts: List[str] = []
        if max_context_facts > 0:
            others = sorted(
                (f for f in eligible if f is not best),
                key=lambda f: _fact_score(f, now), reverse=True,
            )
            for f in others:
                c = str(f.get("content") or "").strip()
                if c and c != fact and c not in context_facts:
                    context_facts.append(c)
                if len(context_facts) >= int(max_context_facts):
                    break
        if long_absence:
            directive = (
                f"主动开场：先像久违的朋友自然问候一句，再顺势提起对方之前说过的"
                f"「{fact}」，关心一下后来怎么样；别一上来就追问，给对方主导节奏。"
            )
        else:
            directive = (
                f"主动开场：自然地回到对方之前提过的「{fact}」，关心一下进展或近况，"
                f"像朋友一直惦记着——别生硬罗列、别连环追问，一句关心即可。"
            )
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止，别显得过分热络或越界。）"
        return {
            "mode": MODE_FOLLOW_UP, "fact": fact, "directive": directive,
            "context_facts": context_facts,
            "long_absence": long_absence, "silent_hours": round(sh, 1),
        }

    # 无可回访记忆 → 温和问候
    directive = (
        "主动开场：好久没联系了，轻松自然地问候一句、关心对方最近怎么样，"
        "把话题主导权交给对方，不要强行找话题或显得刻意。"
    )
    return {
        "mode": MODE_GENTLE_CHECKIN, "fact": "", "directive": directive,
        "context_facts": [],
        "long_absence": long_absence, "silent_hours": round(sh, 1),
    }


def build_proactive_topic_block(
    memory_facts: List[Dict[str, Any]],
    *,
    silent_hours: float,
    stage: str = "",
    intimacy: float = 0.0,
    min_silent_hours: float = 24.0,
    now: Optional[float] = None,
) -> str:
    """组装【主动话题】prompt 块；无需开场时返回 ""（绝不抛）。"""
    try:
        sel = select_proactive_topic(
            memory_facts, silent_hours=silent_hours, stage=stage,
            intimacy=intimacy, min_silent_hours=min_silent_hours, now=now,
        )
    except Exception:
        return ""
    if not sel.get("mode") or not sel.get("directive"):
        return ""
    return f"【主动话题】{sel['directive']}"


__all__ = [
    "MODE_NONE", "MODE_FOLLOW_UP", "MODE_GENTLE_CHECKIN",
    "select_proactive_topic", "build_proactive_topic_block",
]
