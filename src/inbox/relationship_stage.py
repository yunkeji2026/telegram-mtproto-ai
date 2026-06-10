"""P43 — 关系阶段可视化与进阶检测。

与 companion_relationship / IntimacyEngine 双信号对齐：
  - exchange_count（轮次代理）
  - intimacy_score（0-100）

返回阶段进度条数据 + 是否刚进阶（供 toast / 话题包推荐）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.utils.companion_relationship import (
    INTIMACY_BAND_DEFAULTS,
    STAGE_LABEL_ZH,
    STAGE_ORDER,
    derive_stage_from_intimacy,
)

# 轮次阈值（与 companion_relationship._thresholds 默认一致）
_EXCHANGE_BOUNDS = {
    "initial": (0, 4),
    "warming": (4, 14),
    "intimate": (14, 35),
    "steady": (35, 60),
}

_INTIMACY_BOUNDS = {
    "initial": (0, 25),
    "warming": (25, 55),
    "intimate": (55, 80),
    "steady": (80, 100),
}


def _progress_in_range(value: float, lo: float, hi: float) -> int:
    if hi <= lo:
        return 100
    pct = (value - lo) / (hi - lo) * 100
    return max(0, min(100, round(pct)))


def compute_relationship_stage(
    *,
    exchange_count: int = 0,
    intimacy_score: Optional[float] = None,
    previous_stage: str = "",
) -> Dict[str, Any]:
    """计算关系阶段可视化数据。

    Returns:
        stage, stage_label, next_stage, next_stage_label,
        progress_pct, exchange_progress, intimacy_progress,
        advancement_ready, advanced, previous_stage,
        stages: [{id, label, active, done}]
    """
    ex = max(0, int(exchange_count or 0))
    intim = float(intimacy_score) if intimacy_score is not None else None

    # 双信号取较低阶段（与 fuse_with_intimacy 一致，新用户保护简化版）
    stage_from_ex = _stage_from_exchange(ex)
    stage_from_intim = derive_stage_from_intimacy(intim) if intim is not None else None

    if stage_from_intim is not None and ex >= _EXCHANGE_BOUNDS["initial"][1]:
        # 已过 warming 轮次阈值后启用 intimacy 降阶
        si = STAGE_ORDER.index(stage_from_intim)
        ei = STAGE_ORDER.index(stage_from_ex)
        stage = STAGE_ORDER[min(si, ei)]
    else:
        stage = stage_from_ex

    prev = str(previous_stage or "").strip()
    if prev and prev in STAGE_ORDER:
        advanced = STAGE_ORDER.index(stage) > STAGE_ORDER.index(prev)
    else:
        advanced = False

    # 阶段内进度（双信号均值）
    ex_lo, ex_hi = _EXCHANGE_BOUNDS.get(stage, (0, 4))
    ex_prog = _progress_in_range(ex, ex_lo, ex_hi)

    if intim is not None:
        in_lo, in_hi = _INTIMACY_BOUNDS.get(stage, (0, 25))
        in_prog = _progress_in_range(intim, in_lo, in_hi)
        progress_pct = round((ex_prog + in_prog) / 2)
    else:
        in_prog = 0
        progress_pct = ex_prog

    # 下一阶
    idx = STAGE_ORDER.index(stage)
    next_stage = STAGE_ORDER[idx + 1] if idx < len(STAGE_ORDER) - 1 else ""
    advancement_ready = progress_pct >= 85 and bool(next_stage)

    # 阶梯可视化
    stages_viz: List[Dict[str, Any]] = []
    for i, sid in enumerate(STAGE_ORDER):
        stages_viz.append({
            "id": sid,
            "label": STAGE_LABEL_ZH.get(sid, sid),
            "done": i < idx,
            "active": sid == stage,
        })

    return {
        "stage": stage,
        "stage_label": STAGE_LABEL_ZH.get(stage, stage),
        "next_stage": next_stage,
        "next_stage_label": STAGE_LABEL_ZH.get(next_stage, "") if next_stage else "",
        "progress_pct": progress_pct,
        "exchange_count": ex,
        "intimacy_score": intim,
        "exchange_progress": ex_prog,
        "intimacy_progress": in_prog,
        "advancement_ready": advancement_ready,
        "advanced": advanced,
        "previous_stage": prev if prev else None,
        "previous_stage_label": STAGE_LABEL_ZH.get(prev, "") if prev else None,
        "stages": stages_viz,
        "reunion": (
            intim is not None
            and stage_from_intim is not None
            and STAGE_ORDER.index(stage_from_intim) < STAGE_ORDER.index(stage_from_ex)
        ),
    }


def _stage_from_exchange(n: int) -> str:
    if n >= _EXCHANGE_BOUNDS["steady"][0]:
        return "steady"
    if n >= _EXCHANGE_BOUNDS["intimate"][0]:
        return "intimate"
    if n >= _EXCHANGE_BOUNDS["warming"][0]:
        return "warming"
    return "initial"


def _stage_index(stage: str) -> int:
    if stage in STAGE_ORDER:
        return STAGE_ORDER.index(stage)
    return 0


def downgrade_stage_one_level(stage: str) -> str:
    """P46：手动降级目标（降一阶，不低于 initial）。"""
    idx = _stage_index(stage)
    return STAGE_ORDER[max(0, idx - 1)]


def enrich_with_manual_state(
    computed: Dict[str, Any],
    *,
    confirmed_stage: str = "",
    pending_stage: str = "",
    reunion_ack_ts: float = 0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """P46：合并算法阶段与坐席确认状态（待确认进阶 / 回暖）。"""
    import time as _time

    confirmed = str(confirmed_stage or "").strip()
    pending = str(pending_stage or "").strip()
    computed_stage = str(computed.get("stage") or "initial")
    display_stage = confirmed if confirmed in STAGE_ORDER else computed_stage

    ci = _stage_index(computed_stage)
    di = _stage_index(display_stage)
    pending_advancement = bool(pending) or ci > di
    effective_pending = pending or (computed_stage if ci > di else "")

    idx = _stage_index(display_stage)
    stages_viz: List[Dict[str, Any]] = []
    for i, sid in enumerate(STAGE_ORDER):
        stages_viz.append({
            "id": sid,
            "label": STAGE_LABEL_ZH.get(sid, sid),
            "done": i < idx,
            "active": sid == display_stage,
            "pending": sid == effective_pending and pending_advancement,
        })

    ts_now = float(now if now is not None else _time.time())
    reunion_raw = bool(computed.get("reunion"))
    reunion_ack = reunion_ack_ts > 0 and (ts_now - reunion_ack_ts) < 7 * 86400

    return {
        **computed,
        "confirmed_stage": confirmed or None,
        "confirmed_stage_label": STAGE_LABEL_ZH.get(confirmed, "") if confirmed else None,
        "computed_stage": computed_stage,
        "computed_stage_label": computed.get("stage_label"),
        "display_stage": display_stage,
        "display_stage_label": STAGE_LABEL_ZH.get(display_stage, display_stage),
        "stage": display_stage,
        "stage_label": STAGE_LABEL_ZH.get(display_stage, display_stage),
        "pending_stage": effective_pending if pending_advancement else None,
        "pending_stage_label": (
            STAGE_LABEL_ZH.get(effective_pending, "")
            if pending_advancement and effective_pending else None
        ),
        "pending_advancement": pending_advancement,
        "needs_confirmation": pending_advancement and ci > di,
        "advancement_ready": bool(computed.get("advancement_ready")) and not pending_advancement,
        "advanced": False,
        "stages": stages_viz,
        "reunion": reunion_raw and not reunion_ack,
        "reunion_acknowledged": reunion_ack,
    }
