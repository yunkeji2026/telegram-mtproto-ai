"""实时语音通话退化告警评估（B 线，纯函数）。

基于 :class:`~src.ai.realtime_voice_stats.RealtimeVoiceStats` 的 ``dump()`` 判定 GPU 主机健康 /
接通率 / 主机不可达是否退化到需值班介入。纯函数、无 I/O：喂 stats + 阈值，产出
``{light, problems}``，由 :class:`~src.inbox.health_watchdog.HealthWatchdog` 落
``ops_incidents``（kind=``realtime_voice``）——与 health/billing/ai_quality 同一条运维事件闭环。

阈值默认值与 :mod:`src.companion.readiness_signals` 的 ``rtv_*`` 对齐；告警侧额外提供
warn/fail 双档（就绪信号仅单档），样本不足静默返回 green。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.companion.readiness_signals import DEFAULTS as _READINESS_DEFAULTS

# 默认阈值（保守）。生产经 config ``realtime_voice.alert.*`` 覆盖并 opt-in 开启。
DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS: Dict[str, Any] = {
    "min_attempts": int(_READINESS_DEFAULTS["rtv_min_attempts"]),
    "min_health_probes": int(_READINESS_DEFAULTS["rtv_min_health_probes"]),
    "health_ok_rate_warn": float(_READINESS_DEFAULTS["rtv_health_target"]),
    "health_ok_rate_fail": 0.50,
    "connect_rate_warn": float(_READINESS_DEFAULTS["rtv_connect_target"]),
    "connect_rate_fail": 0.30,
}

_THRESHOLD_CASTERS: Dict[str, Any] = {
    "min_attempts": int,
    "min_health_probes": int,
    "health_ok_rate_warn": float,
    "health_ok_rate_fail": float,
    "connect_rate_warn": float,
    "connect_rate_fail": float,
}


def sanitize_realtime_voice_alert_thresholds(raw: Any) -> Dict[str, Any]:
    """白名单 + 类型收敛，供未来校准 UI 写回 config.local.yaml。"""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, caster in _THRESHOLD_CASTERS.items():
        if key not in raw or raw[key] is None:
            continue
        try:
            val = caster(raw[key])
        except (TypeError, ValueError):
            continue
        if caster is float and not (0.0 <= val <= 1.0):
            continue
        if caster is int and val < 0:
            continue
        out[key] = val
    return out


def evaluate_realtime_voice_alert(
    stats: Optional[Dict[str, Any]],
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """评估实时语音基础设施是否退化，返回 ``{"light": str, "problems": [...]}``。

    ``stats`` 为 ``RealtimeVoiceStats.dump()``。样本不足（探测/通话次数低于下限）静默 green。
    """
    t = dict(DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS)
    if thresholds:
        t.update({k: thresholds[k] for k in DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS
                  if k in thresholds and thresholds[k] is not None})
    stats = stats or {}
    problems: List[Dict[str, Any]] = []

    attempts = int(stats.get("attempts") or 0)
    connect_rate = float(stats.get("connect_rate") or 0.0)
    h_ok = int(stats.get("health_ok") or 0)
    h_fail = int(stats.get("health_fail") or 0)
    h_total = h_ok + h_fail
    health_rate = float(stats.get("health_ok_rate") or 0.0)
    by_reason = stats.get("by_end_reason") or {}
    host_unreachable = int(by_reason.get("host_unreachable") or 0)

    min_att = int(t["min_attempts"])
    min_hp = int(t["min_health_probes"])

    if h_total >= min_hp and health_rate < float(t["health_ok_rate_warn"]):
        problems.append({
            "id": "health_ok_rate_low",
            "name": "语音主机健康率",
            "status": ("fail" if health_rate < float(t["health_ok_rate_fail"]) else "warn"),
            "detail": (
                f"主机健康率 {health_rate:.0%} < 阈值 {float(t['health_ok_rate_warn']):.0%}"
                f"（探测 {h_total} 次，ok={h_ok} fail={h_fail}）"
            ),
        })

    if attempts >= min_att:
        if host_unreachable > 0 and host_unreachable >= max(1, attempts // 2):
            problems.append({
                "id": "host_unreachable_spike",
                "name": "主机不可达",
                "status": "fail",
                "detail": (
                    f"主机不可达 {host_unreachable}/{attempts} 次"
                    "（≥半数尝试），先查 GPU 主机/网络"
                ),
            })
        if connect_rate < float(t["connect_rate_warn"]):
            problems.append({
                "id": "connect_rate_low",
                "name": "实时语音接通率",
                "status": ("fail" if connect_rate < float(t["connect_rate_fail"]) else "warn"),
                "detail": (
                    f"接通率 {connect_rate:.0%} < 阈值 {float(t['connect_rate_warn']):.0%}"
                    f"（尝试 {attempts}，接通 {int(stats.get('connected') or 0)}）"
                ),
            })

    light = "green"
    if problems:
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
    return {"light": light, "problems": problems}


def _rtv_metrics_from_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    attempts = int(stats.get("attempts") or 0)
    h_ok = int(stats.get("health_ok") or 0)
    h_fail = int(stats.get("health_fail") or 0)
    by_reason = stats.get("by_end_reason") or {}
    host_unreachable = int(by_reason.get("host_unreachable") or 0)
    return {
        "attempts": attempts,
        "connected": int(stats.get("connected") or 0),
        "connect_rate": float(stats.get("connect_rate") or 0.0),
        "health_ok": h_ok,
        "health_fail": h_fail,
        "health_ok_rate": float(stats.get("health_ok_rate") or 0.0),
        "host_unreachable": host_unreachable,
        "host_unreachable_ratio": round(host_unreachable / attempts, 4) if attempts else 0.0,
    }


def _rtv_margins(stats: Dict[str, Any], t: Dict[str, Any]) -> Dict[str, Any]:
    m = _rtv_metrics_from_stats(stats)
    min_att = int(t["min_attempts"])
    min_hp = int(t["min_health_probes"])
    hr = m["health_ok_rate"]
    cr = m["connect_rate"]
    return {
        "health_ok_rate": {
            "value": hr,
            "warn_margin": round(hr - float(t["health_ok_rate_warn"]), 4),
            "fail_margin": round(hr - float(t["health_ok_rate_fail"]), 4),
            "samples": m["health_ok"] + m["health_fail"],
            "min_samples": min_hp,
        },
        "connect_rate": {
            "value": cr,
            "warn_margin": round(cr - float(t["connect_rate_warn"]), 4),
            "fail_margin": round(cr - float(t["connect_rate_fail"]), 4),
            "samples": m["attempts"],
            "min_samples": min_att,
        },
        "host_unreachable": {
            "count": m["host_unreachable"],
            "attempts": m["attempts"],
            "ratio": m["host_unreachable_ratio"],
            "fail_at_count": max(1, m["attempts"] // 2) if m["attempts"] >= min_att else 0,
        },
    }


def calibrate_realtime_voice_alert(
    stats: Optional[Dict[str, Any]],
    thresholds: Optional[Dict[str, Any]] = None,
    *,
    daily: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """据 stats 快照（+ 可选按日历史 ``daily``，E 线预留）复刻 watchdog 告警评估。

    无 ``daily`` 时用当前进程累计 stats 做单点回放；返回 margins / by_signal / points，
    供 ops 卡 what-if 定阈值后再开 ``realtime_voice.alert.enabled``。
    """
    t = dict(DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS)
    if thresholds:
        t.update({k: thresholds[k] for k in DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS
                  if k in thresholds and thresholds[k] is not None})
    series = [dict(s) for s in (daily or []) if isinstance(s, dict)]
    if not series:
        series = [dict(stats or {})]

    order = {"green": 0, "yellow": 1, "red": 2}
    alerts = 0
    days_in_alert = 0
    by_signal: Dict[str, int] = {}
    points: List[Dict[str, Any]] = []
    worst = "green"
    last_sig: Optional[str] = None

    for snap in series:
        res = evaluate_realtime_voice_alert(snap, t)
        probs = res["problems"]
        light = res["light"]
        ids = sorted(p["id"] for p in probs)
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in probs))
        m = _rtv_metrics_from_stats(snap)
        if probs:
            days_in_alert += 1
            if sig != last_sig:
                alerts += 1
            for pid in ids:
                by_signal[pid] = by_signal.get(pid, 0) + 1
            if order[light] > order[worst]:
                worst = light
            last_sig = sig
        else:
            last_sig = None
        points.append({
            "day": str(snap.get("day") or snap.get("last_end_ts") or ""),
            "light": light,
            "problem_ids": ids,
            "connect_rate": m["connect_rate"],
            "health_ok_rate": m["health_ok_rate"],
            "host_unreachable": m["host_unreachable"],
            "attempts": m["attempts"],
        })

    cur = dict(stats or series[-1] if series else {})
    cur_eval = evaluate_realtime_voice_alert(cur, t)
    mcur = _rtv_metrics_from_stats(cur)
    min_att = int(t["min_attempts"])
    min_hp = int(t["min_health_probes"])
    insufficient = (
        (mcur["attempts"] < min_att)
        and (mcur["health_ok"] + mcur["health_fail"] < min_hp)
    )

    return {
        "evaluated_windows": len(points),
        "alerts": alerts,
        "days_in_alert": days_in_alert,
        "worst_light": worst,
        "by_signal": by_signal,
        "metrics": mcur,
        "margins": _rtv_margins(cur, t),
        "insufficient_samples": insufficient,
        "current_light": cur_eval["light"],
        "current_problems": cur_eval["problems"],
        "points": points,
    }


__all__ = [
    "DEFAULT_REALTIME_VOICE_ALERT_THRESHOLDS",
    "sanitize_realtime_voice_alert_thresholds",
    "evaluate_realtime_voice_alert",
    "calibrate_realtime_voice_alert",
]
