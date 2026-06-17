"""G 线运营智能化：把「看板数据」转成「可执行洞察」。

三块纯函数（不碰 IO，便于单测）：
- :func:`incident_advice` —— 按事件问题项给出「可能根因 + 处置建议」。
- :func:`detect_trend_anomaly` —— 趋势序列末点相对基线的显著异动检测。
- :func:`build_ops_report` —— 7 日运营周报装配（事件/自动化/业务/可靠性/计费）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# 问题 id → (可能根因, 处置建议)。worker_* 走前缀匹配。
_ADVICE = {
    "db": ("持久层不可达（磁盘满/文件锁/进程异常）", "检查 SQLite 文件与磁盘空间，必要时重启进程"),
    "ai": ("AI provider 或 api_key 缺失/占位", "在「设置」填入有效 ai.api_key 并确认 provider"),
    "license": ("授权过期或失效", "续期或重新导入授权文件，避免降级只读"),
    "channels": ("无消息渠道就绪/登录", "到「渠道管理」完成登录或配置至少一个渠道"),
    "queue": ("草稿队列积压，处理跟不上产出", "增派坐席处理，或排查 autosend worker 是否卡顿"),
    "over_seats": ("活跃坐席数超过授权席位", "升级席位额度，或减少同时在线坐席"),
    "message_overage": ("本期消息量超出套餐额度", "升级套餐，或对外发量做节流/收口"),
}
_WORKER_ADVICE = ("后台 worker 未运行或处于熔断", "查看日志定位错误并重启对应 worker")


def incident_advice(problems: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """为事件的每个问题项给出根因+建议。返回 [{id, name, cause, action}]。"""
    out: List[Dict[str, Any]] = []
    for p in problems or []:
        pid = str((p or {}).get("id") or "")
        name = str((p or {}).get("name") or pid)
        if pid.startswith("worker_"):
            cause, action = _WORKER_ADVICE
        else:
            cause, action = _ADVICE.get(pid, ("未归类异常", "查看事件详情与日志进一步定位"))
        out.append({"id": pid, "name": name, "cause": cause, "action": action})
    return out


def detect_trend_anomaly(
    values: Optional[List[Any]],
    *,
    threshold_pct: float = 50.0,
    min_points: int = 4,
    drop_last: bool = False,
) -> Optional[Dict[str, Any]]:
    """检测序列「末点」相对前序基线的显著异动。

    基线 = 除末点外的均值。基线为 0 时：末点 >0 视为上升异动。
    |delta%| >= threshold_pct 才算异动。点数不足 min_points 返回 None。
    返回 {direction: up|down, last, baseline, delta_pct} 或 None。

    drop_last：当序列末桶为「当前未走完」的时段（今天/当前小时）时置 True，
    丢弃该半截桶，改以「最后一个已完结桶」为候选点，避免半桶 vs 满桶的误报。
    """
    vals = [float(v or 0) for v in (values or [])]
    if drop_last and vals:
        vals = vals[:-1]
    if len(vals) < int(min_points):
        return None
    last = vals[-1]
    prior = vals[:-1]
    baseline = sum(prior) / len(prior) if prior else 0.0
    if baseline == 0:
        if last > 0:
            return {"direction": "up", "last": last, "baseline": 0.0, "delta_pct": None}
        return None
    delta_pct = (last - baseline) / baseline * 100.0
    if abs(delta_pct) < float(threshold_pct):
        return None
    return {
        "direction": "up" if delta_pct > 0 else "down",
        "last": last,
        "baseline": round(baseline, 2),
        "delta_pct": round(delta_pct, 1),
    }


def build_ops_report(
    *,
    days: int = 7,
    incident_stats: Optional[Dict[str, Any]] = None,
    roi: Optional[Dict[str, Any]] = None,
    reliability: Optional[Dict[str, Any]] = None,
    billing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """装配运营周报（纯函数）。各子数据由路由层算好传入。"""
    inc = incident_stats or {}
    roi = roi or {}
    reliability = reliability or {}
    billing = billing or {}
    biz = roi.get("business") or {}
    auto = roi.get("automation") or {}
    charges = billing.get("charges") or {}

    mttr_sec = inc.get("mttr_sec")
    mttr_hours = round(mttr_sec / 3600.0, 1) if mttr_sec else None

    incidents = {
        "total": int(inc.get("total") or 0),
        "resolved": int(inc.get("resolved") or 0),
        "open": int(inc.get("open") or 0),
        "by_kind": inc.get("by_kind") or {},
        "mttr_hours": mttr_hours,
    }
    automation = {
        "ai_share_pct": auto.get("ai_share_pct"),
        "saved_hours": auto.get("saved_hours"),
        "saved_money": auto.get("saved_money"),
    }
    business = {
        "leads": biz.get("leads"),
        "conversions": biz.get("conversions"),
        "conversion_rate": biz.get("conversion_rate"),
    }
    rel = {"score": reliability.get("score"), "light": reliability.get("light")}

    # 文字摘要（便于直接贴进周报/IM）。
    headline: List[str] = []
    headline.append(f"近 {days} 天运维事件 {incidents['total']} 起（已解决 {incidents['resolved']}）"
                    + (f"，平均解决 {mttr_hours}h" if mttr_hours is not None else ""))
    if automation["saved_hours"] is not None:
        headline.append(f"AI 自动化节省约 {automation['saved_hours']} 小时"
                        + (f"、{automation['saved_money']} 成本" if automation.get('saved_money') else ""))
    if business["conversions"] is not None:
        headline.append(f"转化 {business['conversions']} 单（转化率 {business.get('conversion_rate')}）")
    if rel["score"] is not None:
        headline.append(f"可靠性评分 {rel['score']}（{rel['light']}）")

    return {
        "ok": True,
        "days": days,
        "incidents": incidents,
        "automation": automation,
        "business": business,
        "reliability": rel,
        "billing": {"total": charges.get("total"), "currency": charges.get("currency"),
                    "anomalies": billing.get("reconcile", {}).get("over_seats", 0)},
        "headline": headline,
        "ts": time.time(),
    }
