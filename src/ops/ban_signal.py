"""G2 封号信号自动急停（反封号护栏三件套之二）。

发送命中平台风控错误时，按类型**分级处置**，不再硬怼到死：

| kind     | 触发（pyrogram/通用异常）                                   | 处置 |
|----------|------------------------------------------------------------|------|
| backoff  | FloodWait / SlowmodeWait（限速，非封号）                   | 仅退避冷却，**不**停号 |
| pause    | PeerFlood（被判垃圾邀约，可恢复）                          | 账号级 Kill-Switch + TTL 自动恢复 |
| ban      | UserDeactivated(Ban) / Unauthorized / AuthKeyUnregistered  | 账号级 Kill-Switch 永久 + 注册表 meta.banned |
| none     | 其它（含我们自己的 send_gate/kill_switch RuntimeError）     | 不处置（交既有熔断） |

**关键设计（复用 G1，不另造拦截路径）**：
- pause/ban 的「停发」直接用 ``kill_switch.set(account:<p>:<id>, ttl=...)`` 落地——
  G1 已把 is_blocked 接到 A/B（Phase C 起含 RPA）全发送路径、持久化、TTL 自动恢复、
  API 可见。故 G2 只需「分类 → set 账号闸」，无需教 gate 认 paused、也无视 gate 是否开。
- ``classify`` 是**纯函数**（按异常类名+属性，零 pyrogram 硬依赖），可注入假异常单测。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

# 异常类名 → 处置类别（按 pyrogram.errors 类名；用类名字符串避免硬依赖）
_BAN_NAMES = frozenset({
    "UserDeactivated", "UserDeactivatedBan", "UserBannedInChannel",
    "Unauthorized", "AuthKeyUnregistered", "AuthKeyDuplicated",
    "SessionRevoked", "SessionExpired", "UserBlocked",
})
_PAUSE_NAMES = frozenset({"PeerFlood", "PeerIdInvalid", "ChatWriteForbidden"})
_BACKOFF_NAMES = frozenset({"FloodWait", "SlowmodeWait", "FloodPremiumWait"})

# 自己抛的控制流异常（不是平台风控）——明确归 none，避免误停
_OWN_PREFIXES = ("send_gate_blocked", "kill_switch_blocked")

DEFAULT_PAUSE_MINUTES = 60.0


def _exc_seconds(exc: Any) -> float:
    """从 FloodWait 类异常取需等待秒数（pyrogram 常见属性 value/x/seconds）。"""
    for attr in ("value", "x", "seconds"):
        try:
            v = getattr(exc, attr, None)
            if v is not None:
                return max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    return 0.0


def classify(exc: Any) -> Dict[str, Any]:
    """纯函数：异常 → ``{kind, cooldown_sec, reason}``。

    kind ∈ {backoff, pause, ban, none}。未知/自家控制流异常 → none。
    """
    name = type(exc).__name__
    msg = str(exc or "")
    if any(msg.startswith(p) for p in _OWN_PREFIXES):
        return {"kind": "none", "cooldown_sec": 0.0, "reason": "own_control_flow"}
    if name in _BACKOFF_NAMES:
        return {"kind": "backoff", "cooldown_sec": _exc_seconds(exc),
                "reason": f"{name}:{_exc_seconds(exc):.0f}s"}
    if name in _PAUSE_NAMES:
        return {"kind": "pause", "cooldown_sec": 0.0, "reason": name}
    if name in _BAN_NAMES:
        return {"kind": "ban", "cooldown_sec": 0.0, "reason": name}
    # 名字未精确命中时，按消息关键词兜底（不同 pyrogram 版本命名差异）
    low = (name + " " + msg).upper()
    if "FLOOD" in low and "WAIT" in low:
        return {"kind": "backoff", "cooldown_sec": _exc_seconds(exc), "reason": name}
    if "PEER_FLOOD" in low or "PEERFLOOD" in low:
        return {"kind": "pause", "cooldown_sec": 0.0, "reason": name}
    if "DEACTIVAT" in low or "UNAUTHORIZED" in low or "AUTH_KEY" in low or "BANNED" in low:
        return {"kind": "ban", "cooldown_sec": 0.0, "reason": name}
    return {"kind": "none", "cooldown_sec": 0.0, "reason": name}


def apply_action(
    platform: str,
    account_id: str,
    action: Dict[str, Any],
    *,
    kill_switch: Any,
    registry: Any = None,
    alert: Optional[Callable[..., Any]] = None,
    pause_minutes: float = DEFAULT_PAUSE_MINUTES,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """按分类结果落地处置（pause/ban 写账号级 Kill-Switch；ban 另标注册表）。

    返回 ``{applied, scope?}``。``kind=none/backoff`` 不动账号（backoff 交既有限速/熔断）。
    """
    kind = str((action or {}).get("kind") or "none")
    reason = str((action or {}).get("reason") or kind)
    scope = f"account:{str(platform or '').lower()}:{account_id}"
    out: Dict[str, Any] = {"applied": kind, "scope": scope}

    if kind in ("none", "backoff"):
        out["applied"] = kind
        return out

    if kind == "pause":
        ttl = float((action or {}).get("cooldown_sec") or 0) or pause_minutes * 60.0
        kill_switch.set(scope, reason=f"auto_pause:{reason}", actor="ban_signal",
                        ttl_sec=ttl, now=now)
        if alert:
            try:
                alert("account_paused",
                      {"platform": platform, "account_id": account_id},
                      f"自动暂停（{reason}），{ttl/60:.0f} 分钟后自动恢复")
            except Exception:
                pass
        out["ttl_sec"] = ttl
        return out

    # kind == "ban"：永久停 + 注册表标记（机群看板可见，待人工核查）
    kill_switch.set(scope, reason=f"auto_ban:{reason}", actor="ban_signal", ttl_sec=0,
                    now=now)
    if registry is not None:
        try:
            row = registry.get(platform, account_id) or {}
            meta = dict(row.get("meta") or {})
            meta["banned"] = True
            meta["ban_reason"] = reason
            registry.upsert(platform, account_id, meta=meta)
        except Exception:
            pass
    if alert:
        try:
            alert("account_banned",
                  {"platform": platform, "account_id": account_id},
                  f"检测到封禁信号（{reason}），已永久冻结，待人工核查")
        except Exception:
            pass
    return out


def handle_send_exception(
    platform: str,
    account_id: str,
    exc: Any,
    *,
    kill_switch: Any = None,
    registry: Any = None,
    alert: Optional[Callable[..., Any]] = None,
    pause_minutes: float = DEFAULT_PAUSE_MINUTES,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """发送异常的便捷处置入口：classify → apply。

    ``kill_switch`` 缺省时取进程单例。绝不抛异常（处置失败不应掩盖原始发送错误）。
    """
    try:
        action = classify(exc)
        if action["kind"] in ("none", "backoff"):
            return {"applied": action["kind"], "kind": action["kind"]}
        ks = kill_switch
        if ks is None:
            from src.ops.kill_switch import get_kill_switch
            ks = get_kill_switch()
        res = apply_action(platform, account_id, action, kill_switch=ks,
                           registry=registry, alert=alert,
                           pause_minutes=pause_minutes, now=now)
        res["kind"] = action["kind"]
        res["reason"] = action["reason"]
        return res
    except Exception:
        return {"applied": "error", "kind": "none"}


__all__ = ["classify", "apply_action", "handle_send_exception", "DEFAULT_PAUSE_MINUTES"]
