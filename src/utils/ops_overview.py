"""E 线运营总览：把 ROI / 计费 / 运行时健康 / 运维可靠性 聚合成「老板单页」。

设计要点
========
- **纯函数**：本模块只做数据装配，不触碰 request / DB / 网络；各子 payload 由
  路由层用已有 builder（build_roi_summary / build_billing_statement /
  collect_health / build_reliability）算好后传入。便于单测、零耦合。
- **总览灯（overall_light）**：取健康灯与可靠性灯中「更差」的一个，红 > 黄 > 绿。
- **计费异常（E3）**：从对账单的 reconcile / charges 派生「超席位 / 超额计费」信号，
  既在总览展示，也可被 watchdog 取出后经 D3（EventBus → webhook/SSE）外发。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# 灯的严重度排序（数值越大越糟）。空串视为「未知/不参与」。
_LIGHT_RANK = {"green": 1, "yellow": 2, "red": 3}


def worst_light(*lights: str) -> str:
    """返回若干灯中最严重的一个；全为空时返回 ""。"""
    worst = ""
    worst_rank = 0
    for lt in lights:
        rank = _LIGHT_RANK.get((lt or "").lower(), 0)
        if rank > worst_rank:
            worst_rank = rank
            worst = (lt or "").lower()
    return worst


def billing_anomalies(billing: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从对账单派生计费异常信号（E3）。

    返回 [{code, severity, message, detail}]，severity ∈ warn|fail。
    - over_seats：活跃坐席超过授权席位（fail）。
    - message_overage：本期消息超出套餐额度产生超额费（warn）。
    """
    billing = billing or {}
    out: List[Dict[str, Any]] = []
    if not billing.get("available"):
        return out

    reconcile = billing.get("reconcile") or {}
    over_seats = int(reconcile.get("over_seats") or 0)
    if over_seats > 0:
        out.append({
            "code": "over_seats",
            "severity": "fail",
            "message": f"活跃坐席超授权席位 {over_seats} 个",
            "detail": {
                "seats": reconcile.get("seats"),
                "active_agents": reconcile.get("active_agents"),
                "over_seats": over_seats,
            },
        })

    charges = billing.get("charges") or {}
    msg_over_qty = int(charges.get("message_overage_qty") or 0)
    if msg_over_qty > 0:
        out.append({
            "code": "message_overage",
            "severity": "warn",
            "message": f"本期消息超额 {msg_over_qty} 条，产生超额费",
            "detail": {
                "message_overage_qty": msg_over_qty,
                "message_overage_amount": charges.get("message_overage_amount"),
                "currency": charges.get("currency"),
            },
        })
    return out


def companion_config_light(companion: Optional[Dict[str, Any]]) -> str:
    """陪伴能力配置健康灯：体检 error→红、warn→黄、自洽→绿；无数据→""（不参与）。"""
    if not companion:
        return ""
    summary = companion.get("summary") or {}
    if int(summary.get("errors") or 0) > 0:
        return "red"
    if int(summary.get("warnings") or 0) > 0:
        return "yellow"
    return "green"


def assemble_ops_overview(
    *,
    roi: Optional[Dict[str, Any]] = None,
    billing: Optional[Dict[str, Any]] = None,
    health: Optional[Dict[str, Any]] = None,
    reliability: Optional[Dict[str, Any]] = None,
    auto_claim: Optional[Dict[str, Any]] = None,
    open_incidents: Optional[int] = None,
    companion: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把四路 payload 装配成总览。

    Args:
        roi:        build_roi_summary 输出
        billing:    build_billing_statement 输出
        health:     collect_health / build_health 输出
        reliability:build_reliability 输出
        auto_claim: 自动认领统计（可选）
        open_incidents: 未关闭运维事件数（E2，可选）
    """
    roi = roi or {}
    billing = billing or {}
    health = health or {}
    reliability = reliability or {}

    biz = roi.get("business") or {}
    auto = roi.get("automation") or {}
    charges = billing.get("charges") or {}
    reconcile = billing.get("reconcile") or {}

    health_light = (health.get("light") or "")
    rel_light = (reliability.get("light") or "")
    overall = worst_light(health_light, rel_light)

    anomalies = billing_anomalies(billing)
    # 有 fail 级计费异常时，总览灯也至少抬到红/黄。
    if any(a["severity"] == "fail" for a in anomalies):
        overall = worst_light(overall, "red")
    elif anomalies:
        overall = worst_light(overall, "yellow")

    # 陪伴能力配置错配（如真发裸奔/真发开但 worker 关）也作为健康信号抬总览灯。
    comp_light = companion_config_light(companion)
    overall = worst_light(overall, comp_light)
    comp_summary = (companion or {}).get("summary") or {}

    kpis = {
        "overall_light": overall,
        "health_light": health_light,
        "reliability_light": rel_light,
        "reliability_score": reliability.get("score"),
        # 业务
        "leads": biz.get("leads"),
        "conversions": biz.get("conversions"),
        "conversion_rate": biz.get("conversion_rate"),
        # 自动化价值
        "ai_share_pct": auto.get("ai_share_pct"),
        "saved_hours": auto.get("saved_hours"),
        "saved_money": auto.get("saved_money"),
        # 计费
        "plan": billing.get("plan"),
        "billing_total": charges.get("total"),
        "billing_currency": charges.get("currency"),
        "over_seats": int(reconcile.get("over_seats") or 0),
        # 告警/事件
        "open_alerts": int(reliability.get("alert_count") or 0),
        "open_incidents": int(open_incidents or 0),
        "billing_anomaly_count": len(anomalies),
        # 陪伴能力配置健康
        "companion_config_light": comp_light,
        "companion_config_errors": int(comp_summary.get("errors") or 0),
        "companion_config_warnings": int(comp_summary.get("warnings") or 0),
    }

    return {
        "ok": True,
        "overall_light": overall,
        "kpis": kpis,
        "billing_anomalies": anomalies,
        "sections": {
            "health": health,
            "reliability": reliability,
            "roi": roi,
            "billing": billing,
            "auto_claim": auto_claim or {},
            "companion": companion or {},
        },
        "ts": time.time(),
    }
