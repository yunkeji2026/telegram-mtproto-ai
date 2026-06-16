"""D2 运维可靠性聚合（错误率 / 处置量趋势 / 后台部件健康度）。

定位：D1 是「此刻各组件红绿灯」、D3 是「异常主动告警」，D2 补「一段时间的可靠性」——
后台 worker 的发送成功率/错误率/熔断、草稿处置量与拦截率趋势、近期健康告警次数。

数据来源全部既有：
- worker ``status_snapshot()``（autosend/webhook 的 sent/errors）—— 实时快照；
- ``InboxStore.get_reliability_timeline`` —— 持久时间序（重启不丢）；
- EventBus 最近 ``health_alert`` 事件 —— 近期告警。

:func:`build_reliability` 为**纯函数**，便于单测。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def worker_reliability(snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把各 worker 的 status_snapshot 归一成可靠性视图（含 error_rate / 状态）。"""
    out: List[Dict[str, Any]] = []
    for s in snapshots or []:
        if not s:
            continue
        sent = int(s.get("total_sent", 0) or 0)
        errors = int(s.get("total_errors", 0) or 0)
        attempts = sent + errors
        running = bool(s.get("running"))
        circuit = bool(s.get("circuit_open"))
        err_rate = _rate(errors, attempts)
        if not running:
            status = "fail"
        elif circuit or err_rate >= 0.2:
            status = "warn"
        else:
            status = "ok"
        out.append({
            "id": s.get("id") or s.get("name") or "worker",
            "name": s.get("name") or s.get("id") or "worker",
            "running": running,
            "circuit_open": circuit,
            "total_sent": sent,
            "total_errors": errors,
            "error_rate": err_rate,
            "status": status,
        })
    return out


def build_reliability(
    *,
    worker_snapshots: Optional[List[Dict[str, Any]]] = None,
    timeline: Optional[List[Dict[str, Any]]] = None,
    recent_alerts: Optional[List[Dict[str, Any]]] = None,
    window_hours: int = 24,
) -> Dict[str, Any]:
    """聚合运维可靠性（纯函数）。返回 {ok, score, light, workers, trend, totals, alerts}。"""
    workers = worker_reliability(worker_snapshots or [])

    # 趋势 + 区间累计
    tl = timeline or []
    total = sum(int(b.get("total", 0) or 0) for b in tl)
    blocked = sum(int(b.get("blocked", 0) or 0) for b in tl)
    rejected = sum(int(b.get("rejected", 0) or 0) for b in tl)
    autosend = sum(int(b.get("autosend", 0) or 0) for b in tl)
    trend = [
        {"ts": int(b.get("bucket_ts", 0) or 0),
         "total": int(b.get("total", 0) or 0),
         "blocked": int(b.get("blocked", 0) or 0),
         "rejected": int(b.get("rejected", 0) or 0)}
        for b in tl
    ]

    alerts = recent_alerts or []
    alert_count = len(alerts)

    # 可靠性评分：100 起扣——worker fail -30 / warn -10；近期告警每条 -5（封顶 -25）
    score = 100
    for w in workers:
        score -= {"fail": 30, "warn": 10}.get(w["status"], 0)
    score -= min(25, alert_count * 5)
    score = max(0, min(100, score))
    light = "red" if score < 60 else ("yellow" if score < 85 else "green")

    return {
        "ok": True,
        "window_hours": int(window_hours),
        "score": score,
        "light": light,
        "workers": workers,
        "trend": trend,
        "totals": {
            "dispositions": total,
            "autosend": autosend,
            "blocked": blocked,
            "rejected": rejected,
            "block_rate": _rate(blocked, total),
            "reject_rate": _rate(rejected, total),
        },
        "alerts": alerts,
        "alert_count": alert_count,
        "ts": time.time(),
    }
