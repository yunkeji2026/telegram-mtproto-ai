"""C2-1 计费对账：把 C0-2 用量按「账期（自然月）」聚合成对账单 + 导出 CSV。

设计要点
========
- **复用单一数据源**：账单数字全部来自 ``InboxStore.get_usage_stats(since, until)``，
  与 ROI / 质量 / 用量看板**同口径**，不引入第二套统计逻辑。
- **账期 = 自然月**：``month_window`` 按本地时区算月首/次月首半开区间 ``[since, until)``，
  与 ``get_usage_stats`` 的 until_ts 语义对齐。
- **纯函数**：``compute_statement`` / ``statement_to_csv`` 无副作用，便于单测与复用
  （API 与 CLI 都能调）。
- **对账维度**：消息量(in/out/total)、AI 调用、AI 自动发、活跃坐席 vs 授权席位，
  并标出超额（over_seats）供对账时人工核对。
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any, Dict, Optional, Tuple

# 默认价目表（USD）。生产可在 config.pricing 覆盖；included_*=0 表示不限/不计超额。
DEFAULT_PRICING: Dict[str, Any] = {
    "currency": "USD",
    "plans": {
        "community": {"monthly": 0, "included_messages": 0, "included_seats": 0},
        "basic": {"monthly": 49, "included_messages": 5000, "included_seats": 2},
        "pro": {"monthly": 149, "included_messages": 20000, "included_seats": 5},
        "flagship": {"monthly": 499, "included_messages": 0, "included_seats": 0},
    },
    "overage": {"per_message": 0.0, "per_seat": 0.0},
}


def month_window(year: int, month: int) -> Tuple[float, float]:
    """返回自然月 ``[since, until)`` 的本地时间戳（秒）。

    until 为次月 1 号 00:00，半开区间避免跨月重复计数。
    """
    year = int(year)
    month = int(month)
    since = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    if month == 12:
        nxt = (year + 1, 1, 1, 0, 0, 0, 0, 0, -1)
    else:
        nxt = (year, month + 1, 1, 0, 0, 0, 0, 0, -1)
    until = time.mktime(nxt)
    return float(since), float(until)


def parse_period(period: str) -> Tuple[int, int]:
    """解析 ``YYYY-MM`` → (year, month)；非法时回退到当前自然月。"""
    try:
        y, m = str(period or "").split("-", 1)
        yi, mi = int(y), int(m)
        if 1 <= mi <= 12 and 2000 <= yi <= 9999:
            return yi, mi
    except Exception:
        pass
    lt = time.localtime()
    return lt.tm_year, lt.tm_mon


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def plan_included_messages(plan: str, pricing: Optional[Dict[str, Any]] = None) -> int:
    """某套餐的月含消息量；0 = 不限。缺套餐回退 community。"""
    pr = pricing or DEFAULT_PRICING
    plans = pr.get("plans") or DEFAULT_PRICING["plans"]
    cfg = plans.get(str(plan or "community")) or plans.get("community") or {}
    return int(_f(cfg.get("included_messages")))


def message_quota_status(used: int, included: int) -> Dict[str, Any]:
    """月度消息配额软状态（纯函数，K 阶段）。``included=0`` → 不限。

    level：ok / warn（≥80%）/ over（超含量，将按超额计费）。**只产状态、不阻断发送**——
    硬限交由独立 enforce 开关（避免误切付费客户）。
    """
    used = int(used or 0)
    included = int(included or 0)
    if included <= 0:
        return {"used": used, "included": 0, "ratio": None,
                "level": "ok", "text": "消息量不限"}
    ratio = round(used / included, 2)
    if used > included:
        level, text = "over", f"本月消息 {used} 已超套餐含量 {included}，超出部分按量计费"
    elif ratio >= 0.8:
        level, text = "warn", f"本月消息 {used}/{included}，接近套餐含量"
    else:
        level, text = "ok", f"本月消息 {used}/{included}"
    return {"used": used, "included": included, "ratio": ratio,
            "level": level, "text": text}


def compute_charges(statement: Dict[str, Any], pricing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按价目表把对账单用量换算成应收金额（纯函数）。

    模型：``base`` 套餐月费 + 消息超额 + 席位超额。``included_*=0`` 视为不限（不计超额）。
    席位含额优先用价目表 ``included_seats``，缺省回退授权 ``seats``。
    """
    pr = pricing or DEFAULT_PRICING
    plan = str(statement.get("plan") or "community")
    plans = pr.get("plans") or DEFAULT_PRICING["plans"]
    plan_cfg = plans.get(plan) or plans.get("community") or {}
    overage = pr.get("overage") or {}
    currency = str(pr.get("currency") or DEFAULT_PRICING["currency"])

    usage = statement.get("usage") or {}
    messages = int(usage.get("messages_total", 0) or 0)
    active = int(usage.get("active_agents", 0) or 0)

    base = _f(plan_cfg.get("monthly"))
    inc_msg = int(_f(plan_cfg.get("included_messages")))
    inc_seat = int(_f(plan_cfg.get("included_seats"))) or int(statement.get("seats") or 0)

    per_msg = _f(overage.get("per_message"))
    per_seat = _f(overage.get("per_seat"))

    msg_over_qty = max(0, messages - inc_msg) if inc_msg > 0 else 0
    seat_over_qty = max(0, active - inc_seat) if inc_seat > 0 else 0
    msg_over_amt = round(msg_over_qty * per_msg, 2)
    seat_over_amt = round(seat_over_qty * per_seat, 2)
    total = round(base + msg_over_amt + seat_over_amt, 2)

    return {
        "currency": currency,
        "plan": plan,
        "base_fee": round(base, 2),
        "included_messages": inc_msg,
        "included_seats": inc_seat,
        "message_overage_qty": msg_over_qty,
        "message_overage_amount": msg_over_amt,
        "seat_overage_qty": seat_over_qty,
        "seat_overage_amount": seat_over_amt,
        "total": total,
        "lines": [
            {"label": f"{plan} 套餐月费", "amount": round(base, 2)},
            {"label": f"消息超额 {msg_over_qty} 条 @ {per_msg}", "amount": msg_over_amt},
            {"label": f"席位超额 {seat_over_qty} 席 @ {per_seat}", "amount": seat_over_amt},
        ],
    }


def compute_statement(
    inbox,
    year: int,
    month: int,
    *,
    license_status: Optional[Dict[str, Any]] = None,
    pricing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成某账期（自然月）的对账单。store 缺失时优雅降级为 available=False。"""
    since, until = month_window(year, month)
    period = f"{int(year):04d}-{int(month):02d}"
    lic = license_status or {}
    seats = int(lic.get("seats") or 0)
    out: Dict[str, Any] = {
        "ok": True,
        "period": period,
        "since_ts": since,
        "until_ts": until,
        "available": False,
        "plan": lic.get("plan", "community"),
        "license_state": lic.get("state", "unavailable"),
        "customer": lic.get("customer", ""),
        "seats": seats,
    }
    if inbox is None or not hasattr(inbox, "get_usage_stats"):
        out["charges"] = compute_charges(out, pricing)
        return out
    try:
        u = inbox.get_usage_stats(since, until_ts=until)
    except TypeError:
        u = inbox.get_usage_stats(since)
    except Exception:
        return out

    active = int(u.get("active_agents", 0) or 0)
    out["available"] = True
    out["usage"] = {
        "messages_in": int(u.get("messages_in", 0) or 0),
        "messages_out": int(u.get("messages_out", 0) or 0),
        "messages_total": int(u.get("messages_total", 0) or 0),
        "ai_calls": int(u.get("ai_calls", 0) or 0),
        "ai_sent": int(u.get("ai_sent", 0) or 0),
        "active_agents": active,
    }
    out["reconcile"] = {
        "seats": seats,
        "active_agents": active,
        "over_seats": max(0, active - seats) if seats > 0 else 0,
        "within_quota": (seats <= 0) or (active <= seats),
    }
    # 对账行项：稳定顺序，便于 CSV / 人工核对
    out["line_items"] = [
        {"metric": "messages_total", "label": "消息总量", "qty": out["usage"]["messages_total"]},
        {"metric": "messages_in", "label": "入站消息", "qty": out["usage"]["messages_in"]},
        {"metric": "messages_out", "label": "出站消息", "qty": out["usage"]["messages_out"]},
        {"metric": "ai_calls", "label": "AI 调用", "qty": out["usage"]["ai_calls"]},
        {"metric": "ai_sent", "label": "AI 自动发送", "qty": out["usage"]["ai_sent"]},
        {"metric": "active_agents", "label": "活跃坐席", "qty": active},
    ]
    out["charges"] = compute_charges(out, pricing)
    return out


def statement_to_csv(statement: Dict[str, Any]) -> str:
    """对账单 → CSV 文本（含账期/套餐元信息 + 行项明细）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["billing_statement"])
    w.writerow(["period", statement.get("period", "")])
    w.writerow(["customer", statement.get("customer", "")])
    w.writerow(["plan", statement.get("plan", "")])
    w.writerow(["license_state", statement.get("license_state", "")])
    w.writerow(["seats", statement.get("seats", 0)])
    rec = statement.get("reconcile", {}) or {}
    w.writerow(["active_agents", rec.get("active_agents", 0)])
    w.writerow(["over_seats", rec.get("over_seats", 0)])
    w.writerow(["within_quota", "yes" if rec.get("within_quota", True) else "no"])
    w.writerow([])
    w.writerow(["metric", "label", "quantity"])
    for li in statement.get("line_items", []) or []:
        w.writerow([li.get("metric", ""), li.get("label", ""), li.get("qty", 0)])
    ch = statement.get("charges") or {}
    if ch:
        cur = ch.get("currency", "")
        w.writerow([])
        w.writerow(["charges", cur])
        for ln in ch.get("lines", []) or []:
            w.writerow(["charge", ln.get("label", ""), ln.get("amount", 0)])
        w.writerow(["total", "", ch.get("total", 0)])
    return buf.getvalue()
