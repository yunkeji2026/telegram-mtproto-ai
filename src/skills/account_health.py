"""M7 反封号 v1 · 账号预热爬坡 + 健康评分（纯函数，零副作用，便于单测）。

为什么需要
==========
出海个人号/RPA 账号最大的命门是**封号**。平台风控盯三件事：
1. **新号暴发**：刚注册就大量外发 → 立刻判定营销号。对策：**预热爬坡**——
   新号每日上限从很低（如 2）随账号天龄线性升到目标值（如 14 天到满）。
2. **账号孤立信号**：无独立代理、频繁 FLOOD_WAIT / 发送失败 → 风险升高。
   对策：**健康评分红绿灯**，把这些信号汇成 0–100 分 + green/amber/red。
3. **超配额**：已由 ``AccountLimiter`` 日配额覆盖；本模块给「应当用多少配额」的建议值。

本模块只算**建议**与**评分**，不做强制（强制由 AccountLimiter 决定）。纯函数无 IO。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 健康灯阈值（分数 → 灯色）
_GREEN_MIN = 70
_AMBER_MIN = 40


def warmup_cap(
    age_days: float,
    target_cap: int,
    *,
    start_cap: int = 2,
    ramp_days: int = 14,
) -> int:
    """账号预热期建议每日上限：从 ``start_cap`` 在 ``ramp_days`` 天内线性升到 ``target_cap``。

    - ``age_days <= 0``（新号当天）→ start_cap。
    - ``age_days >= ramp_days`` → target_cap（预热完成）。
    - 中间线性插值，向下取整，且不低于 start_cap、不高于 target_cap。
    - 若 ``target_cap <= start_cap``（目标本就很低）→ 直接返回 target_cap，不爬坡。
    """
    target = int(target_cap)
    start = max(0, int(start_cap))
    ramp = max(1, int(ramp_days))
    if target <= start:
        return max(0, target)
    age = float(age_days or 0.0)
    if age <= 0:
        return start
    if age >= ramp:
        return target
    span = target - start
    capped = start + int(span * (age / ramp))
    return max(start, min(target, capped))


def account_health(
    signals: Dict[str, Any],
    *,
    target_cap: int = 15,
    warmup_start_cap: int = 2,
    warmup_ramp_days: int = 14,
) -> Dict[str, Any]:
    """把单账号风控信号汇成健康评分 + 红绿灯 + 建议每日上限（纯函数）。

    ``signals`` 字段（全部可选，缺省视为良性）::

        age_days        账号天龄（影响预热建议；过新且高发会扣分）
        sends_today     今日已外发次数
        flood_waits_24h 近 24h FLOOD_WAIT/限频次数（Telegram 关键风控信号）
        errors_24h      近 24h 发送失败/异常次数
        proxy_bound     是否绑定独立代理（bool）
        banned          是否已被封/受限（bool）→ 直接红灯 0 分

    返回 ``{score, light, recommended_cap, over_cap, reasons[]}``。
    """
    age_days = float(signals.get("age_days") or 0.0)
    sends_today = int(signals.get("sends_today") or 0)
    floods = int(signals.get("flood_waits_24h") or 0)
    errors = int(signals.get("errors_24h") or 0)
    proxy_bound = bool(signals.get("proxy_bound", True))
    banned = bool(signals.get("banned", False))

    rec_cap = warmup_cap(
        age_days, target_cap,
        start_cap=warmup_start_cap, ramp_days=warmup_ramp_days,
    )
    reasons: List[str] = []

    if banned:
        return {
            "score": 0,
            "light": "red",
            "recommended_cap": rec_cap,
            "over_cap": sends_today > rec_cap,
            "reasons": ["账号已被封禁/受限，立即停发并人工核查"],
        }

    score = 100
    if not proxy_bound:
        score -= 35
        reasons.append("未绑定独立代理（多号共出口 IP 极易被关联封号）")
    if floods > 0:
        score -= min(40, 12 * floods)
        reasons.append(f"近 24h 触发 {floods} 次限频（FLOOD_WAIT），需放缓节奏")
    if errors > 0:
        score -= min(20, 4 * errors)
        reasons.append(f"近 24h {errors} 次发送失败/异常")
    over_cap = sends_today > rec_cap
    if over_cap:
        score -= 20
        reasons.append(
            f"今日已发 {sends_today} 次，超出预热建议上限 {rec_cap} 次"
        )
    if age_days < warmup_ramp_days:
        reasons.append(
            f"账号处于预热期（{age_days:.0f}/{warmup_ramp_days} 天），建议每日 ≤ {rec_cap} 次"
        )

    score = max(0, min(100, score))
    light = "green" if score >= _GREEN_MIN else ("amber" if score >= _AMBER_MIN else "red")
    if not reasons:
        reasons.append("账号健康，无风控信号")
    return {
        "score": score,
        "light": light,
        "recommended_cap": rec_cap,
        "over_cap": over_cap,
        "reasons": reasons,
    }


def fleet_health(accounts: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
    """对一组账号信号批量评分，汇成机群健康概览（纯函数）。

    每个元素需含 ``account_id`` + account_health 所需信号字段。
    返回总体灯色（取最差账号灯）+ 红绿黄计数 + 平均分 + 每账号明细。
    """
    items: List[Dict[str, Any]] = []
    counts = {"green": 0, "amber": 0, "red": 0}
    total_score = 0
    for acc in accounts or []:
        h = account_health(acc, **kwargs)
        counts[h["light"]] += 1
        total_score += h["score"]
        items.append({
            "account_id": str(acc.get("account_id") or ""),
            "score": h["score"],
            "light": h["light"],
            "recommended_cap": h["recommended_cap"],
            "over_cap": h["over_cap"],
            "reasons": h["reasons"],
        })
    n = len(items)
    if counts["red"]:
        fleet_light = "red"
    elif counts["amber"]:
        fleet_light = "amber"
    elif n:
        fleet_light = "green"
    else:
        fleet_light = "unknown"
    return {
        "fleet_light": fleet_light,
        "total": n,
        "counts": counts,
        "avg_score": round(total_score / n, 1) if n else 0.0,
        "accounts": sorted(items, key=lambda x: x["score"]),  # 最差在前
    }
