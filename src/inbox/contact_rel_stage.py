"""P50 — 客户级关系阶段同步与冲突检测。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.utils.companion_relationship import STAGE_LABEL_ZH, STAGE_ORDER


def stage_index(stage: str) -> int:
    if stage in STAGE_ORDER:
        return STAGE_ORDER.index(stage)
    return 0


def highest_stage(stages: List[str]) -> str:
    best = "initial"
    bi = -1
    for s in stages:
        if s in STAGE_ORDER:
            i = STAGE_ORDER.index(s)
            if i > bi:
                bi = i
                best = s
    return best


def detect_stage_conflict(
    contact_stage: str,
    conv_stages: Dict[str, str],
) -> Dict[str, Any]:
    """检测客户级与会话级阶段是否不一致。"""
    distinct = sorted({s for s in conv_stages.values() if s})
    has_conflict = False
    reasons: List[str] = []

    if len(distinct) > 1:
        has_conflict = True
        reasons.append(f"{len(distinct)} 个会话阶段不一致")
    if contact_stage and distinct and contact_stage not in distinct:
        has_conflict = True
        reasons.append("客户级与会话级不一致")
    if contact_stage:
        for cid, st in conv_stages.items():
            if st and st != contact_stage:
                has_conflict = True
                break

    highest = highest_stage(distinct + ([contact_stage] if contact_stage else []))
    hi = stage_index(highest)
    ci = stage_index(contact_stage)
    show_to_highest = bool(distinct) and (
        not contact_stage or hi > ci or len(distinct) > 1
    )

    return {
        "has_conflict": has_conflict,
        "contact_stage": contact_stage or None,
        "contact_stage_label": STAGE_LABEL_ZH.get(contact_stage, "") if contact_stage else None,
        "conv_stages": conv_stages,
        "distinct_stages": distinct,
        "distinct_labels": [STAGE_LABEL_ZH.get(s, s) for s in distinct],
        "reasons": reasons,
        "highest_stage": highest or None,
        "highest_stage_label": STAGE_LABEL_ZH.get(highest, "") if highest else None,
        "show_to_contact": bool(contact_stage),
        "show_to_highest": show_to_highest,
        "contact_lagging": bool(contact_stage and hi > ci),
    }


def enrich_with_contact_stage(
    result: Dict[str, Any],
    *,
    contact_stage: str = "",
    contact_updated_by: str = "",
    conflict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在会话级阶段响应上附加客户级元数据。"""
    out = {**result}
    cs = str(contact_stage or "").strip()
    out["contact_stage"] = cs or None
    out["contact_stage_label"] = STAGE_LABEL_ZH.get(cs, "") if cs else None
    out["contact_updated_by"] = contact_updated_by or None
    if conflict:
        out["stage_conflict"] = conflict.get("has_conflict", False)
        out["stage_conflict_detail"] = conflict
    else:
        out["stage_conflict"] = False
        out["stage_conflict_detail"] = None
    return out
