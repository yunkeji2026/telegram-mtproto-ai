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


def orchestrator_worker_problems(
    orchestrator: Optional[Dict[str, Any]], *, min_restarts_fail: int = 3,
) -> List[Dict[str, Any]]:
    """从编排器 ``status()`` 的 accounts 提取 ``error`` 态 worker 作为「问题项」。

    供总览灯升级（``orchestrator_worker_light``）与 HealthWatchdog 主动外发（P6）**共用同一口径**，
    避免两处各判、口径漂移。分级：
    - ``restarts >= min_restarts_fail``（编排器退避重试多次仍未恢复＝**真实掉线**）→ ``fail``；
    - 否则 ``warn``（可能瞬时抖动/正在重连）。

    返回 ``[{id, name, status(warn|fail), platform, account_id, restarts, detail}]``；
    无 accounts / 无 error 态 → 空列表。纯函数。
    """
    if not orchestrator:
        return []
    threshold = max(1, int(min_restarts_fail))
    out: List[Dict[str, Any]] = []
    for a in orchestrator.get("accounts") or []:
        if str(a.get("state") or "") != "error":
            continue
        platform = str(a.get("platform") or "?")
        account_id = str(a.get("account_id") or "?")
        restarts = int(a.get("restarts") or 0)
        severity = "fail" if restarts >= threshold else "warn"
        detail = str(a.get("last_error") or "").strip()[:200]
        out.append({
            "id": f"{platform}:{account_id}",
            "name": f"{platform}/{account_id}",
            "status": severity,
            "platform": platform,
            "account_id": account_id,
            "restarts": restarts,
            "detail": detail or "worker 启动失败",
        })
    return out


def orchestrator_worker_light(
    orchestrator: Optional[Dict[str, Any]], *, min_restarts_fail: int = 3,
) -> str:
    """编排器受管 worker 健康灯（出站路由的「真信号」）。

    为何不用回落率直接上灯：``send_routes`` 的回落率单看有歧义——RPA-only 部署里
    LINE/WhatsApp/Messenger 账号本就不归编排器管、100% 走适配器是**正常**的，按回落率
    上灯会对这类部署误报。真正的异常是「某个已登记账号的编排器 worker 崩了」——即受管
    worker 处于 ``error`` 态（``_start_account`` 抛错后进退避重试），此时该账号的协议/网页
    收发降级或中断，且当前**别处无任何健康信号覆盖**（collect_health 只看 autosend/autoclaim）。

    分级（与 ``orchestrator_worker_problems`` / P6 告警同口径）：
    - 无 status / 无受管账号 → ""（不参与；RPA-only 或编排器未启用天然不误报）。
    - 有 worker 持续崩溃（``restarts >= min_restarts_fail``）→ "red"（真实掉线）。
    - 有 worker ``error`` 态但未达阈值 → "yellow"（降级：回落适配器或退避重试中）。
    - 其余（全 running/starting/stopped）→ "green"。

    注：``accounts`` 缺失但 ``by_state.error>0``（概要口径，如单测/概览快照）时保守回落 "yellow"。
    """
    if not orchestrator:
        return ""
    by_state = orchestrator.get("by_state") or {}
    total = int(orchestrator.get("total") or 0)
    if total <= 0 and not by_state:
        return ""
    problems = orchestrator_worker_problems(
        orchestrator, min_restarts_fail=min_restarts_fail)
    if any(p["status"] == "fail" for p in problems):
        return "red"
    if problems or int(by_state.get("error") or 0) > 0:
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
    orchestrator: Optional[Dict[str, Any]] = None,
    send_routes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把四路 payload 装配成总览。

    Args:
        roi:        build_roi_summary 输出
        billing:    build_billing_statement 输出
        health:     collect_health / build_health 输出
        reliability:build_reliability 输出
        auto_claim: 自动认领统计（可选）
        open_incidents: 未关闭运维事件数（E2，可选）
        companion:  陪伴能力配置体检（gather_companion_advice，可选）
        orchestrator: 账号编排器 status()（受管 worker 健康，可选）
        send_routes: SendRouteStats.dump()（出站路由回落率，信息量，可选）
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

    # 编排器受管 worker 崩溃（error 态）→ 出站路由降级，抬总览灯（当前别处无此信号）。
    orch_light = orchestrator_worker_light(orchestrator)
    overall = worst_light(overall, orch_light)
    orch_by_state = (orchestrator or {}).get("by_state") or {}
    send_routes = send_routes or {}

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
        # 出站路由：编排器 worker 健康（真信号）+ 回落率（信息量）
        "orchestrator_worker_light": orch_light,
        "orchestrator_workers_error": int(orch_by_state.get("error") or 0),
        "orchestrator_workers_running": int(orch_by_state.get("running") or 0),
        "send_fallback_rate": float(send_routes.get("fallback_rate") or 0.0),
        "send_total": int(send_routes.get("total") or 0),
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
            "orchestrator": orchestrator or {},
            "send_routes": send_routes,
        },
        "ts": time.time(),
    }
