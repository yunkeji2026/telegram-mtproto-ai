"""Phase ② 关系成长系统（Bond Level）——把 intimacy_score 升华为「看得见的成长」。

竞品对标（星野/Replika/Talkie）：陪伴留存的核心是让用户**看见关系在变深**——
数字等级 + 升级进度条 + 里程碑 + 按等级解锁专属体验。本仓已有 intimacy_score
（0-100，IntimacyEngine）与规范阶段（companion_relationship），但只是**运营侧**信号；
缺的是端用户可见的成长机制。本模块在不另起炉灶的前提下补上这一层。

设计纪律（与 relationship_health / proactive_topic / empathy_strategy 一致）
────────────────────────────────────────────────────────────────────
- **纯函数、零 IO、可单测**：入参是已算好的标量，不触 DB/网络/LLM。
- **单一事实源**：阶段名 / 中文标签 / 阈值全部 import 自 ``companion_relationship``
  （``STAGE_ORDER`` / ``STAGE_LABEL_ZH`` / ``INTIMACY_BAND_DEFAULTS``），等级即「阶段
  的序号 + 进度」，阈值漂移单点收敛，绝不复刻第二套 0-25-55-80。
- **解锁是「预览」非「硬门控」**：``level_unlocks`` 由配置映射驱动，默认空=不解锁任何东西，
  且本模块**绝不**替代变现侧的 tier gating——它只回答「这段关系到了什么深度、配解锁什么」，
  是否真放行仍由 monetization 决定。避免把关系深度误用成绕过付费的后门。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.utils.companion_relationship import (
    INTIMACY_BAND_DEFAULTS,
    STAGE_LABEL_ZH,
    STAGE_ORDER,
    derive_stage_from_intimacy,
)

# 里程碑：相识时长（天）→ 人话标签。达到即「解锁」一个温暖的纪念点。
_DAY_MILESTONES: List[tuple] = [
    (7, "relationship_week", "相识一周"),
    (30, "relationship_month", "相识满月"),
    (100, "relationship_100d", "相识百日"),
    (365, "relationship_year", "相识一周年"),
]

# 里程碑：累计对方主动消息数 → 标签（衡量「交心」深度，而非我方刷量）。
_TURN_MILESTONES: List[tuple] = [
    (50, "talked_50", "聊了 50 句心里话"),
    (200, "talked_200", "聊了 200 句"),
    (1000, "talked_1000", "聊了 1000 句"),
]


def _band_thresholds(bands: Optional[Dict[str, float]] = None) -> List[float]:
    """返回升序的阶段下界列表 ``[0, to_warming, to_intimate, to_steady]``。

    与 ``STAGE_ORDER`` 一一对应：initial=[0,..) / warming=[to_warming,..) / …
    """
    b = dict(INTIMACY_BAND_DEFAULTS)
    if isinstance(bands, dict):
        for k in ("to_warming", "to_intimate", "to_steady"):
            if k in bands:
                try:
                    b[k] = float(bands[k])
                except (TypeError, ValueError):
                    pass
    return [0.0, b["to_warming"], b["to_intimate"], b["to_steady"]]


def compute_bond_level(
    intimacy_score: Optional[float],
    *,
    bands: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """把 intimacy_score 映射成「关系等级 + 升级进度」（确定性纯函数）。

    返回 ``{level, stage, name, score, progress, score_to_next, next_stage,
    next_name, is_max}``：
      - ``level``：1..len(STAGE_ORDER)，``0`` 表示无信号（score=None/非法）。
      - ``stage`` / ``name``：规范阶段 code 与中文标签（来自 companion_relationship）。
      - ``progress``：在**当前段内**朝下一级的完成度 0..1（满级=1.0）。
      - ``score_to_next``：还差多少分升级（满级=0.0）。
    """
    empty = {
        "level": 0, "stage": "", "name": "", "score": None,
        "progress": 0.0, "score_to_next": None, "next_stage": "",
        "next_name": "", "is_max": False,
    }
    stage = derive_stage_from_intimacy(intimacy_score, bands)
    if stage is None or stage not in STAGE_ORDER:
        return empty
    try:
        s = max(0.0, min(100.0, float(intimacy_score)))
    except (TypeError, ValueError):
        return empty

    idx = STAGE_ORDER.index(stage)
    level = idx + 1
    thresholds = _band_thresholds(bands)
    lower = thresholds[idx]
    is_max = idx >= len(STAGE_ORDER) - 1

    if is_max:
        # 满级：进度按 [to_steady, 100] 计；无下一级
        span = max(1e-9, 100.0 - lower)
        progress = max(0.0, min(1.0, (s - lower) / span))
        return {
            "level": level, "stage": stage, "name": STAGE_LABEL_ZH.get(stage, stage),
            "score": round(s, 1), "progress": round(progress, 3),
            "score_to_next": 0.0, "next_stage": "", "next_name": "",
            "is_max": True,
        }

    upper = thresholds[idx + 1]
    span = max(1e-9, upper - lower)
    progress = max(0.0, min(1.0, (s - lower) / span))
    next_stage = STAGE_ORDER[idx + 1]
    return {
        "level": level, "stage": stage, "name": STAGE_LABEL_ZH.get(stage, stage),
        "score": round(s, 1), "progress": round(progress, 3),
        "score_to_next": round(max(0.0, upper - s), 1),
        "next_stage": next_stage, "next_name": STAGE_LABEL_ZH.get(next_stage, next_stage),
        "is_max": False,
    }


def bond_milestones(
    *,
    intimacy_score: Optional[float] = None,
    days_known: Optional[float] = None,
    turn_count_in: Optional[int] = None,
    bands: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """列出**已达成**的关系里程碑（确定性，供后台/端展示「成长足迹」）。

    含三类：相识时长（天）、累计交心句数、升级到某阶段。无对应输入则跳过该类。
    返回按重要度（时长 > 句数 > 升级）稳定排序的 ``[{code, label, kind}]``。
    """
    out: List[Dict[str, Any]] = []

    if days_known is not None:
        try:
            d = float(days_known)
            for thr, code, label in _DAY_MILESTONES:
                if d >= thr:
                    out.append({"code": code, "label": label, "kind": "tenure"})
        except (TypeError, ValueError):
            pass

    if turn_count_in is not None:
        try:
            t = int(turn_count_in)
            for thr, code, label in _TURN_MILESTONES:
                if t >= thr:
                    out.append({"code": code, "label": label, "kind": "talk"})
        except (TypeError, ValueError):
            pass

    if intimacy_score is not None:
        lvl = compute_bond_level(intimacy_score, bands=bands)
        # 升级里程碑：达到 warming 及以上每一阶段各记一个
        for i in range(1, lvl.get("level", 0)):  # 跳过 initial（level 1，非「升级」）
            st = STAGE_ORDER[i]
            out.append({
                "code": f"reached_{st}",
                "label": f"关系升至「{STAGE_LABEL_ZH.get(st, st)}」",
                "kind": "levelup",
            })

    return out


def level_unlocks(
    level: int,
    unlock_map: Optional[Dict[Any, Any]] = None,
) -> List[str]:
    """按关系等级返回**累计解锁**的条目 id 列表（配置驱动的「预览」）。

    ``unlock_map`` 支持两种键：等级数字（1..N）或阶段 code（initial/warming/…）。
    返回所有 ``key <= 当前等级`` 的条目并集（去重保序）。默认 None/空 → 空列表
    （不解锁任何东西）。本函数**不做付费判定**，只回答「关系深度配解锁什么」。
    """
    if not unlock_map or not isinstance(unlock_map, dict):
        return []
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return []
    if lvl <= 0:
        return []

    out: List[str] = []
    seen = set()
    for key, items in unlock_map.items():
        key_level = _key_to_level(key)
        if key_level is None or key_level > lvl:
            continue
        for it in (items or []):
            sid = str(it).strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def _key_to_level(key: Any) -> Optional[int]:
    """unlock_map 的键 → 等级数字（支持阶段 code 或 1..N 数字 / 数字字符串）。"""
    if isinstance(key, bool):  # 防 True/False 被当 1/0
        return None
    if isinstance(key, int):
        return key
    s = str(key).strip().lower()
    if s in STAGE_ORDER:
        return STAGE_ORDER.index(s) + 1
    if s.isdigit():
        return int(s)
    return None


def build_bond_level_block(
    intimacy_score: Optional[float],
    *,
    days_known: Optional[float] = None,
    fresh_milestone: Optional[str] = None,
    bands: Optional[Dict[str, float]] = None,
) -> str:
    """组装【关系进展】prompt 块——克制地让 AI 感知关系深度/纪念点（默认无则返回 ""）。

    与 ``stage_directive``（语气校准）互补：本块给「关系的厚度与纪念感」上下文，
    供 AI 自然带出（"我们都认识两个月啦"），**绝不**让 AI 像游戏 NPC 一样宣告
    "你已升到 Lv3"。触发收敛到真正有意义的时刻：
      - 关系已到 warming 及以上（对陌生人不谈深度，免越界）；且
      - 传入了 ``fresh_milestone``（本轮刚达成的里程碑 code）——无则不打扰。
    无 fresh_milestone 时仅在 intimate/steady 阶段给一句轻量「关系厚度」背景。
    """
    lvl = compute_bond_level(intimacy_score, bands=bands)
    if lvl.get("level", 0) < 2:
        return ""  # initial：不谈关系深度

    name = lvl.get("name") or ""
    if fresh_milestone:
        label = _milestone_label(fresh_milestone)
        if label:
            return (
                f"【关系进展】你们的关系刚迎来一个小小的纪念点：{label}。"
                f"可以自然、真诚地流露一点开心或感慨，别刻意宣布或当成任务播报。"
            )

    # 无新里程碑：仅深度关系给一句厚度背景，浅关系保持沉默（免油腻）
    if lvl.get("stage") in ("intimate", "steady"):
        days_hint = ""
        if days_known is not None:
            try:
                dd = int(float(days_known))
                if dd >= 14:
                    days_hint = f"（已相识约 {dd} 天）"
            except (TypeError, ValueError):
                days_hint = ""
        return (
            f"【关系进展】你们已是「{name}」的关系{days_hint}；"
            f"语气可更自然亲近，像彼此惦记的老朋友，但仍跟随对方节奏，不刻意秀亲密。"
        )
    return ""


def _milestone_label(code: str) -> str:
    c = str(code or "").strip()
    for _, mc, label in _DAY_MILESTONES:
        if mc == c:
            return label
    for _, mc, label in _TURN_MILESTONES:
        if mc == c:
            return label
    if c.startswith("reached_"):
        st = c[len("reached_"):]
        if st in STAGE_ORDER:
            return f"关系升至「{STAGE_LABEL_ZH.get(st, st)}」"
    # Phase ④续：剧情完成纪念点——``story:<人话标签>``，标签随码透传（避免本模块耦合剧情表）
    if c.startswith("story:"):
        return c[len("story:"):].strip()
    return ""


__all__ = [
    "compute_bond_level",
    "bond_milestones",
    "level_unlocks",
    "build_bond_level_block",
]
