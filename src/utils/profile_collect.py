"""主动画像采集框架（确定性纯函数）——把 Stage R 的「升级 bland 开场顺势问」泛化（Stage T）。

陪伴型 AI 要「越来越懂你」，就得在关系够深、却还缺某项高价值画像（生日 / 称呼 / …）时，
**借最没话说的 gentle_checkin 开场自然问一句**，而不是干巴巴「好久没聊」。本模块抽出这套
采集的**通用决策与文案**，让新增可采集槽位只需登记一条 directive + 一个「已知判定」即可。

约定：
- 只升级 ``gentle_checkin``（有记忆钩子时回访记忆更有价值，不打断）。
- 槽位已知 → 不问；关系不够深 → 不问；距上次问未过冷却 → 不问。
- 危机/低落由 skill 层情绪护栏另判（本模块只管「该不该借这次开场问」的结构性门槛）。
- 多槽位按 ``PROFILE_SLOTS`` 优先级择一（一次开场只问一个，不连环逼问）。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# 采集优先级（靠前先问）：生日 > 称呼。新增槽位在此登记顺序。
PROFILE_SLOTS: Tuple[str, ...] = ("birthday", "name")

# 各槽位的「主动问」directive（只学口吻、随口一问、不强求）。
_DIRECTIVES = {
    "birthday": (
        "主动开场：好久没聊了，先自然轻松地问候一句；再像朋友间随口好奇那样，"
        "顺势问一句还不知道TA生日是哪天呢——别像填表或查户口，问完顺其自然，"
        "TA不想说也别追。"),
    "name": (
        "主动开场：好久没聊了，先自然问候一句；再顺势轻松地问一句平时喜欢别人怎么称呼TA、"
        "想让我怎么叫TA——随口一问、别太正式，TA不想说也别勉强。"),
}

# 关系尚浅时追加的克制提示（避免显得在刻意收集信息）。
_WARMING_SUFFIX = {
    "birthday": "（关系还偏新：更随意带过、别显得刻意打听。）",
    "name": "（关系还偏新：随口带过、别显得刻意打听。）",
}


def ask_directive(slot: str, *, stage: str = "") -> str:
    """某槽位的主动采集 directive；未登记的槽位返回 ""。关系浅时追加克制提示。"""
    s = str(slot or "").strip().lower()
    d = _DIRECTIVES.get(s, "")
    if d and str(stage or "").strip().lower() in ("initial", "warming"):
        d += _WARMING_SUFFIX.get(s, "")
    return d


def is_collectable(slot: str) -> bool:
    return str(slot or "").strip().lower() in _DIRECTIVES


def should_ask_profile_slot(
    *,
    opener_mode: str,
    intimacy: float,
    min_intimacy: float,
    slot_known: bool,
    last_ask_ts: float,
    now: float,
    cooldown_days: float,
) -> bool:
    """是否该借这次主动开场顺势问某画像槽位（确定性纯函数）。

    只在最没话说的 bland 开场（``gentle_checkin``）上升级；槽位已知/关系不够深/未过冷却 → 否。
    """
    if str(opener_mode or "") != "gentle_checkin":
        return False
    if slot_known:
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


def select_missing_slot(slot_known: List[Tuple[str, bool]]) -> Optional[str]:
    """按给定顺序挑第一个「未知」的槽位 key；全已知 → None。纯函数，便于单测。"""
    for slot, known in slot_known or []:
        if not known:
            return str(slot)
    return None


__all__ = [
    "PROFILE_SLOTS",
    "ask_directive",
    "is_collectable",
    "should_ask_profile_slot",
    "select_missing_slot",
]
