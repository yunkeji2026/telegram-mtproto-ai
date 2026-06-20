"""共享「发送前反封号闸门」（N 线 核心3）。

A 线 (``TelegramClient._send_reply``) 与 B 线 (协议 autoreply ``_send``) 此前各管各的
限速，预热爬坡 / 健康红绿灯（M7 ``account_health``）没接到任一真实发送路径。本模块把
M7 纯函数**编排成一道两线共用的发送前决策**，不重复造代理/评分/爬坡逻辑：

    signals → account_health(M7) → 决策 allowed / reason / health

默认**关闭**（``companion_send_gate.enabled`` 缺省 false）→ 行为零变更；开启后两条线
共用同一门控（超预热上限 / 红灯 → 拒发或转人工）。机群概览复用 M7 ``fleet_health``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.skills.account_health import account_health, fleet_health


def gate_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """读 ``companion_send_gate.enabled``（默认 False → 零破坏）。"""
    try:
        return bool(((config or {}).get("companion_send_gate") or {}).get("enabled", False))
    except Exception:
        return False


def _gate_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return dict((config or {}).get("companion_send_gate") or {})
    except Exception:
        return {}


def gate_decision(
    signals: Dict[str, Any],
    *,
    target_cap: int = 15,
    warmup_start_cap: int = 2,
    warmup_ramp_days: int = 14,
    block_on_red: bool = True,
) -> Dict[str, Any]:
    """单账号发送前决策（纯函数，复用 M7 ``account_health``）。

    返回 ``{allowed, reason, light, score, recommended_cap, health}``：
    - ``banned`` 或（``block_on_red`` 且红灯）→ allowed=False, reason=``health_red``/``banned``
    - ``sends_today >= recommended_cap``（预热/配额）→ allowed=False, reason=``warmup_cap``
    - 否则 allowed=True, reason=``ok``
    """
    health = account_health(
        signals,
        target_cap=target_cap,
        warmup_start_cap=warmup_start_cap,
        warmup_ramp_days=warmup_ramp_days,
    )
    light = health["light"]
    sends_today = int(signals.get("sends_today") or 0)
    rec_cap = int(health["recommended_cap"])

    if bool(signals.get("banned", False)):
        reason, allowed = "banned", False
    elif block_on_red and light == "red":
        reason, allowed = "health_red", False
    elif sends_today >= rec_cap:
        reason, allowed = "warmup_cap", False
    else:
        reason, allowed = "ok", True

    return {
        "allowed": allowed,
        "reason": reason,
        "light": light,
        "score": health["score"],
        "recommended_cap": rec_cap,
        "health": health,
    }


def evaluate(
    signals: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """从 config 取阈值后做 ``gate_decision``（供两线发送路径调用的便捷入口）。

    闸门关闭时恒 ``allowed=True``（reason=``disabled``），保证零破坏。
    """
    if not gate_enabled(config):
        return {"allowed": True, "reason": "disabled", "light": "green",
                "score": 100, "recommended_cap": 0, "health": {}}
    gc = _gate_cfg(config)
    return gate_decision(
        signals,
        target_cap=int(gc.get("target_cap", 15) or 15),
        warmup_start_cap=int(gc.get("warmup_start_cap", 2) or 2),
        warmup_ramp_days=int(gc.get("warmup_ramp_days", 14) or 14),
        block_on_red=bool(gc.get("block_on_red", True)),
    )


def aggregate_fleet(
    accounts: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """两条线账号信号汇成机群健康概览（复用 M7 ``fleet_health``）。"""
    gc = _gate_cfg(config)
    return fleet_health(
        accounts,
        target_cap=int(gc.get("target_cap", 15) or 15),
        warmup_start_cap=int(gc.get("warmup_start_cap", 2) or 2),
        warmup_ramp_days=int(gc.get("warmup_ramp_days", 14) or 14),
    )


__all__ = ["gate_enabled", "gate_decision", "evaluate", "aggregate_fleet"]
