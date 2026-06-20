"""共享账号信号源 + 机群概览（N 线：N3 信号接线 + N6 云端多开运维）。

把「发送前反封号闸门」与「机群健康看板」所需的账号风控信号，**统一**从
- ``account_registry``：天龄(created_at) / 独立代理(proxy_id) / 状态(status) / meta.banned
- ``AutoReplyLimiter``：今日已发(day_used) / 熔断(circuit_open)
装配出来，A/B 两线与 ops 看板**共用同一份事实**，避免各算各的（不重复造轮子）。

纯函数 + 依赖注入（registry/limiter/now 可传假对象）→ 不依赖真号即可单测。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple, Union

from src.skills.companion_send_gate import aggregate_fleet

_DAY = 86400.0

# 生命周期阶段（云端多开运维可视化）
STAGE_PENDING = "pending"      # 待登录（扫码/配置已建档，未上线）
STAGE_WARMING = "warming"      # 预热期（天龄 < ramp_days）
STAGE_ACTIVE = "active"        # 活跃在线
STAGE_RESTRICTED = "restricted"  # 受限（熔断中）
STAGE_BANNED = "banned"        # 封禁/下线
STAGE_OFFLINE = "offline"      # 离线


def build_account_signals(
    platform: str,
    account_id: str,
    *,
    registry: Any = None,
    limiter: Any = None,
    now: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """装配单账号风控信号（best-effort，缺数据视为良性）。

    返回字段对齐 ``account_health`` / ``companion_send_gate``：
    ``account_id, age_days?, proxy_bound, banned, sends_today?, _circuit_open?``。
    """
    now = float(now if now is not None else time.time())
    sig: Dict[str, Any] = {"account_id": str(account_id or "")}

    acc: Dict[str, Any] = {}
    if registry is not None:
        try:
            acc = registry.get(platform, account_id) or {}
        except Exception:
            acc = {}

    try:
        created = float(acc.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if created > 0:
        sig["age_days"] = max(0.0, (now - created) / _DAY)

    sig["proxy_bound"] = bool(acc.get("proxy_id"))
    status = str(acc.get("status") or "")
    meta = acc.get("meta") or {}
    sig["banned"] = bool(meta.get("banned")) or status == "removed"

    if limiter is not None:
        try:
            snap = limiter.snapshot(f"{str(platform or '').lower()}:{account_id}", now)
            sig["sends_today"] = int(snap.get("day_used") or 0)
            if snap.get("circuit_open"):
                sig["_circuit_open"] = True
        except Exception:
            pass

    if extra:
        for k, v in extra.items():
            if v is not None:
                sig[k] = v
    return sig


def lifecycle_stage(
    signals: Dict[str, Any], status: str = "", *, warmup_ramp_days: int = 14
) -> str:
    """从信号 + 注册表状态推断账号生命周期阶段（看板可视化用）。"""
    status = str(status or "").lower()
    if signals.get("banned") or status == "removed":
        return STAGE_BANNED
    if signals.get("_circuit_open"):
        return STAGE_RESTRICTED
    if status == "pending":
        return STAGE_PENDING
    age = float(signals.get("age_days") or 0.0)
    in_warmup = 0 < age < float(warmup_ramp_days or 14)
    online = status in ("online", "active") or bool(signals.get("sends_today"))
    if in_warmup:
        return STAGE_WARMING
    if online:
        return STAGE_ACTIVE
    if status == "offline":
        return STAGE_OFFLINE
    return STAGE_ACTIVE if online else STAGE_OFFLINE


def fleet_overview(
    accounts: List[Union[Dict[str, Any], Tuple]],
    *,
    registry: Any = None,
    limiter: Any = None,
    config: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """对一组账号装配信号 → 机群健康（M7 fleet_health）+ 生命周期分布。

    ``accounts`` 元素可为 dict（含 platform/account_id/status）或
    ``(platform, account_id[, status])`` 元组。
    """
    now = float(now if now is not None else time.time())
    gc = (config or {}).get("companion_send_gate") or {}
    ramp = int(gc.get("warmup_ramp_days", 14) or 14)

    sigs: List[Dict[str, Any]] = []
    lifecycle: Dict[str, int] = {}
    detail: List[Dict[str, Any]] = []

    for item in accounts or []:
        if isinstance(item, dict):
            platform = item.get("platform")
            account_id = item.get("account_id")
            status = item.get("status", "")
        else:
            platform = item[0]
            account_id = item[1]
            status = item[2] if len(item) > 2 else ""
        sig = build_account_signals(
            platform, account_id, registry=registry, limiter=limiter, now=now
        )
        sigs.append(sig)
        stage = lifecycle_stage(sig, status, warmup_ramp_days=ramp)
        lifecycle[stage] = lifecycle.get(stage, 0) + 1
        detail.append({
            "platform": str(platform or "").lower(),
            "account_id": str(account_id or ""),
            "stage": stage,
        })

    fleet = aggregate_fleet(sigs, config)
    return {
        "fleet": fleet,
        "lifecycle": lifecycle,
        "accounts": detail,
        "total": len(detail),
    }


__all__ = [
    "build_account_signals",
    "lifecycle_stage",
    "fleet_overview",
    "STAGE_PENDING", "STAGE_WARMING", "STAGE_ACTIVE",
    "STAGE_RESTRICTED", "STAGE_BANNED", "STAGE_OFFLINE",
]
