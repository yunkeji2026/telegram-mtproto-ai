"""每日仪式感主动问候：晨安 / 晚安（按用户活跃时段择时）——确定性纯函数。

陪伴型 AI 对标竞品（Replika / 星野）的留存核心：除了"沉默 N 小时才主动"
（见 ``companion_proactive``），还要有**每天固定的仪式感问候**——清晨一句早安、睡前
一句晚安，像真的有人每天惦记着 TA。这是日活（DAU）留存的关键钩子，本产品此前缺失。

与 ``companion_proactive`` 互补：
- 那条是**沉默驱动**（久未联系才回访某条记忆）；本模块是**时段驱动**（每天到点问候）。
- 共用同一发送回路 / 情绪护栏 / care 去重；但**每日每档去重**（一天最多一句早安、一句晚安）。

设计（与 ``plan_proactive_sends`` 同范式）：
- ``plan_daily_rituals`` 是**确定性纯函数**：给定会话快照 + 已发表 + 时钟 + 注入式 opener，
  决定本 tick 该给谁道早/晚安。零 IO、可单测。
- **个性化择时**：注入 ``active_hours_provider`` 时，按该用户历史消息的活跃时段推断 TA 习惯的
  晨/晚点，只在那个点问候（早起的人 7 点收到、夜猫子 23 点收到）；无历史 → 退回配置窗口起点。
- **默认关**：上层 ``companion.proactive_topic.daily_ritual.enabled`` 控。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MORNING = "morning"
NIGHT = "night"

# 推断活跃时段时的「晨 / 晚」带（小时，闭区间集合）——比问候窗口宽，用于读懂用户作息。
_MORNING_BAND = set(range(5, 12))      # 5..11
_NIGHT_BAND = set(range(18, 24))       # 18..23


def window_hours(window: Any, *, default_start: int, default_end: int) -> List[int]:
    """把 (start, end) 问候窗口（闭开区间 [start, end)，支持跨午夜）展开成小时列表。

    例：morning (7,10)→[7,8,9]；night (21,24)→[21,22,23]；跨午夜 (23,2)→[23,0,1]。
    非法/缺省 → 用 default。
    """
    try:
        start = int(window[0]) % 24
        end = int(window[1]) % 24
    except (TypeError, ValueError, IndexError):
        start, end = int(default_start) % 24, int(default_end) % 24
    if start == end:
        return [start]
    hrs: List[int] = []
    h = start
    # 最多绕一圈，防御性封顶 24 步
    for _ in range(24):
        if h == end:
            break
        hrs.append(h)
        h = (h + 1) % 24
    return hrs or [start]


def current_slot(
    hour: int,
    *,
    morning_window: Any = (7, 10),
    night_window: Any = (21, 24),
) -> Optional[str]:
    """当前小时落在哪个仪式档（morning / night），都不在 → None。morning 优先。"""
    h = int(hour) % 24
    if h in window_hours(morning_window, default_start=7, default_end=10):
        return MORNING
    if h in window_hours(night_window, default_start=21, default_end=24):
        return NIGHT
    return None


def infer_active_hour(hour_samples: Any, slot: str) -> Optional[int]:
    """从历史消息小时直方图推断该用户在某档的习惯活跃点。无样本落在带内 → None。

    morning：取晨带 [5,12) 内出现最多的小时（并列取最早，照顾早起者先收到）；
    night：取晚带 [18,24) 内最多的小时（并列取最晚，夜猫子晚点收到）。
    """
    s = str(slot or "").strip().lower()
    band = _MORNING_BAND if s == MORNING else _NIGHT_BAND if s == NIGHT else None
    if band is None:
        return None
    counts: Dict[int, int] = {}
    for raw in hour_samples or []:
        try:
            h = int(raw) % 24
        except (TypeError, ValueError):
            continue
        if h in band:
            counts[h] = counts.get(h, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    cands = [h for h, c in counts.items() if c == best]
    return min(cands) if s == MORNING else max(cands)


def _target_hour(
    slot: str,
    window: Any,
    *,
    default_start: int,
    default_end: int,
    samples: Optional[List[int]],
) -> int:
    """该用户本档应被问候的**唯一小时**：个性化活跃点（落在窗口内才采纳），否则窗口起点。"""
    hrs = window_hours(window, default_start=default_start, default_end=default_end)
    if samples is not None:
        pref = infer_active_hour(samples, slot)
        if pref is not None and pref in hrs:
            return pref
    return hrs[0]


def plan_daily_rituals(
    conversations: List[Dict[str, Any]],
    *,
    ritual_sent: Dict[str, float],
    opener_fn: Callable[..., Dict[str, Any]],
    now: Optional[float] = None,
    morning_window: Any = (7, 10),
    night_window: Any = (21, 24),
    min_intimacy: float = 20.0,
    min_quiet_gap_hours: float = 3.0,
    max_per_tick: int = 5,
    has_pending_care: Optional[Callable[[str], bool]] = None,
    active_hours_provider: Optional[Callable[[str], List[int]]] = None,
) -> List[Dict[str, Any]]:
    """决定本 tick 该给谁道早 / 晚安（确定性纯函数）。非问候时段 → 空。

    Args:
        conversations: 会话快照（同 ``plan_proactive_sends``：conversation_id/platform/
            account_id/chat_key/last_ts/last_direction/archived/memory_key/stage/intimacy/
            last_emotion）。
        ritual_sent: ``{ritual_key: ts}``，``ritual_key=f"{cid}:{daykey}:{slot}"``——每日每档去重。
        opener_fn: ``opener_fn(slot=, memory_key=, stage=, intimacy=, last_emotion=,
            contact_key=) -> {mode, directive, fact, ...}``（即 build_ritual_opener；
            含情绪护栏：危机→blocked、低落→克制问候）。
        active_hours_provider: 可选 ``(cid) -> [hour,...]``（该用户历史消息小时）；提供则个性化择时。
        min_intimacy: 低于此亲密度不问候（不对刚认识的人道"早安亲爱的"）。
        min_quiet_gap_hours: 距上次互动不足此小时 → 不问候（人还在场，道早晚安多余）。

    Returns:
        计划列表（同 plan_proactive_sends 形状 + ``slot/ritual_key``），按亲密度降序截断。
    """
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    hour = lt.tm_hour
    slot = current_slot(hour, morning_window=morning_window, night_window=night_window)
    if slot is None:
        return []  # 非晨 / 晚问候时段
    day_key = time.strftime("%Y%m%d", lt)

    plans: List[Dict[str, Any]] = []
    for c in conversations or []:
        if not isinstance(c, dict) or c.get("archived"):
            continue
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        try:
            intimacy = float(c.get("intimacy") or 0.0)
        except (TypeError, ValueError):
            intimacy = 0.0
        if intimacy < float(min_intimacy):
            continue  # 关系太浅，不做仪式问候
        ritual_key = f"{cid}:{day_key}:{slot}"
        if ritual_key in (ritual_sent or {}):
            continue  # 今天这一档已问候过
        # 与 proactive_care 去重：已排关怀的会话让路（care 优先、更具体）
        if has_pending_care is not None:
            try:
                if has_pending_care(cid):
                    continue
            except Exception:
                logger.debug("[ritual] has_pending_care 失败 cid=%s", cid, exc_info=True)
        # 人还在场（刚聊过）→ 不必道早 / 晚安
        try:
            last_ts = float(c.get("last_ts") or 0)
        except (TypeError, ValueError):
            last_ts = 0.0
        if last_ts > 0 and (now - last_ts) / 3600.0 < float(min_quiet_gap_hours):
            continue
        # 个性化择时：只在该用户本档的目标小时问候（个性化活跃点或窗口起点）
        samples = None
        if active_hours_provider is not None:
            try:
                samples = list(active_hours_provider(cid) or [])
            except Exception:
                samples = None
        target = _target_hour(
            slot, morning_window if slot == MORNING else night_window,
            default_start=7 if slot == MORNING else 21,
            default_end=10 if slot == MORNING else 24,
            samples=samples,
        )
        if hour != target:
            continue
        try:
            opener = opener_fn(
                slot=slot,
                memory_key=str(c.get("memory_key") or ""),
                stage=str(c.get("stage") or ""),
                intimacy=intimacy,
                last_emotion=str(c.get("last_emotion") or ""),
                contact_key=cid,
            ) or {}
        except Exception:
            logger.debug("[ritual] opener_fn 失败 cid=%s", cid, exc_info=True)
            continue
        mode = str(opener.get("mode") or "")
        directive = str(opener.get("directive") or "")
        if not mode or not directive:
            continue  # 被情绪护栏拦下（危机）或无文案 → 不问候
        plans.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or ""),
            "chat_key": str(c.get("chat_key") or ""),
            "mode": mode,
            "directive": directive,
            "fact": str(opener.get("fact") or ""),
            "context_facts": [
                str(f).strip() for f in (opener.get("context_facts") or [])
                if str(f).strip()
            ],
            "scenario_id": "",
            "feature": "",
            "slot": slot,
            "ritual_key": ritual_key,
            "intimacy": round(intimacy, 1),
        })

    plans.sort(key=lambda p: p["intimacy"], reverse=True)
    return plans[: max(0, int(max_per_tick))]


__all__ = [
    "MORNING",
    "NIGHT",
    "window_hours",
    "current_slot",
    "infer_active_hour",
    "plan_daily_rituals",
]
