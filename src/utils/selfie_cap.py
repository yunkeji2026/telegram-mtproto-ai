"""自拍/形象照「全局每日出图预算」跟踪器——进程级单例。

Stage F 给 `companion.selfie.daily_global_cap` 建了护栏：跨所有端用户的当日出图总次数硬上限，
护住出图 API（OpenAI images 等，按张计费）账单。Stage J 把它从「SkillManager 实例属性」提升为
**进程级单例**——这样：
  1. 真·全局：多账号（多 SkillManager 实例）共享同一份预算，与「单一全局 config」语义一致；
  2. 可观测：Web 路由（`/api/monetize/selfie-cap`）能 peek 同一份，把已用/剩余/归零时刻上看板。

底层复用 `DailyCapTracker`（线程安全、按 tz 0 点自动归零、运行时 set_cap 热调）。
与 `companion_funnel_store` 的单例同型（get / peek / reset）。
"""

from __future__ import annotations

from typing import Optional

from src.integrations.rpa_base.daily_cap import DailyCapTracker

_TRACKER: Optional[DailyCapTracker] = None


def get_selfie_cap_tracker(cap: int) -> DailyCapTracker:
    """取/建全局出图预算跟踪器单例。每次按传入 cap `set_cap`（跟随 config 热调）。"""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = DailyCapTracker(daily_cap=int(cap))
    else:
        _TRACKER.set_cap(int(cap))
    return _TRACKER


def peek_selfie_cap_tracker() -> Optional[DailyCapTracker]:
    """只读取单例（不创建）。供 Web 看板取快照——未出过图则返回 None。"""
    return _TRACKER


def reset_selfie_cap_tracker() -> None:
    """重置单例（测试隔离用）。"""
    global _TRACKER
    _TRACKER = None


__all__ = [
    "get_selfie_cap_tracker",
    "peek_selfie_cap_tracker",
    "reset_selfie_cap_tracker",
]
