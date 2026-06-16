"""统一收件箱——用量计量看板（C0-2）。

商业化地基的第二块：把可计费用量（消息量 / AI 调用数 / 活跃坐席数）按天聚合，
并与 C0-1 授权额度（席位 seats）对照，接近上限时提示。数据全部来自既有
``InboxStore.get_usage_stats``（单一数据源，与 ROI/质量看板同口径）。

:func:`build_usage_summary` 为纯函数，便于单测；端点 ``GET /api/workspace/usage``
（主管/老板专属）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request

from src.web.routes.unified_inbox_auth import _require_supervisor
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _pct_delta(cur: float, prev: Optional[float]) -> Optional[float]:
    """环比百分比变化；prev 为 0/None 时返回 None（避免除零/无意义）。"""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _license_quota() -> Dict[str, Any]:
    """读取当前授权额度（席位/套餐/渠道）；不可用时返回社区默认。"""
    try:
        from src.licensing import get_license_manager

        st = get_license_manager().status()
        return {
            "plan": st.plan,
            "state": st.state,
            "customer": getattr(st, "customer", ""),
            "seats": st.seats,
            "channels": list(st.channels),
        }
    except Exception:
        logger.debug("授权额度读取失败（已忽略）", exc_info=True)
        return {"plan": "community", "state": "unavailable", "customer": "",
                "seats": 0, "channels": []}


def build_usage_summary(request: Request, span: int = 30) -> Dict[str, Any]:
    """聚合用量概览（纯逻辑；store 缺失时优雅降级为空）。"""
    span = 7 if int(span or 30) <= 7 else 30
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    since = midnight - (span - 1) * 86400
    prev_since = since - span * 86400

    out: Dict[str, Any] = {"ok": True, "days": span, "available": False}
    out["license"] = _license_quota()

    inbox = _inbox_store(request)
    if inbox is None or not hasattr(inbox, "get_usage_stats"):
        return out

    try:
        cur = inbox.get_usage_stats(since)
    except Exception:
        logger.debug("用量统计失败（已忽略）", exc_info=True)
        return out

    # 环比：上一等长窗口 [prev_since, since)；旧 store 无 until_ts 时跳过
    prev: Dict[str, Any] = {}
    try:
        prev = inbox.get_usage_stats(prev_since, until_ts=since)
    except TypeError:
        logger.debug("store 不支持 until_ts，跳过用量环比", exc_info=True)
    except Exception:
        logger.debug("用量环比统计失败（已忽略）", exc_info=True)

    out["available"] = True
    out["usage"] = {
        "messages_total": cur.get("messages_total", 0),
        "messages_in": cur.get("messages_in", 0),
        "messages_out": cur.get("messages_out", 0),
        "ai_calls": cur.get("ai_calls", 0),
        "ai_sent": cur.get("ai_sent", 0),
        "active_agents": cur.get("active_agents", 0),
    }
    out["compare"] = {
        "messages_delta_pct": _pct_delta(
            cur.get("messages_total", 0), prev.get("messages_total")) if prev else None,
        "ai_calls_delta_pct": _pct_delta(
            cur.get("ai_calls", 0), prev.get("ai_calls")) if prev else None,
    }
    out["trend"] = cur.get("trend", [])
    out["quota"] = _quota_status(out["usage"], out["license"])
    return out


def _quota_status(usage: Dict[str, Any], lic: Dict[str, Any]) -> Dict[str, Any]:
    """席位额度对照：用量内活跃坐席 vs 授权席位。seats=0 视为不限。"""
    seats = int(lic.get("seats") or 0)
    active = int(usage.get("active_agents") or 0)
    if seats <= 0:
        return {"seats": 0, "active_agents": active, "ratio": None,
                "level": "ok", "text": "席位不限"}
    ratio = round(active / seats, 2)
    if active > seats:
        level, text = "over", f"活跃坐席 {active} 已超授权席位 {seats}，请升级套餐"
    elif ratio >= 0.8:
        level, text = "warn", f"活跃坐席 {active}/{seats}，接近席位上限"
    else:
        level, text = "ok", f"活跃坐席 {active}/{seats}"
    return {"seats": seats, "active_agents": active, "ratio": ratio,
            "level": level, "text": text}


def _pricing(request: Request) -> Optional[Dict[str, Any]]:
    """从 config 读取价目表覆盖；缺失时返回 None（billing 用内置默认）。"""
    try:
        cm = getattr(request.app.state, "config_manager", None)
        if cm is not None and getattr(cm, "config", None):
            pr = cm.config.get("pricing")
            if isinstance(pr, dict) and pr:
                return pr
    except Exception:
        logger.debug("价目表读取失败（用默认）", exc_info=True)
    return None


def build_billing_statement(request: Request, period: str = "") -> Dict[str, Any]:
    """C2-1/C2-2：按账期(YYYY-MM)生成对账单 + 应收金额（纯逻辑，复用账单模块）。"""
    from src.utils.billing import compute_statement, parse_period

    year, month = parse_period(period)
    return compute_statement(_inbox_store(request), year, month,
                             license_status=_license_quota(),
                             pricing=_pricing(request))


def register_usage_routes(app, *, api_auth) -> None:
    """挂载用量计量看板端点（GET /api/workspace/usage，主管/老板专属）。"""

    @app.get("/api/workspace/usage")
    async def api_workspace_usage(request: Request, days: int = 30):
        """用量：消息量 / AI 调用数 / 活跃坐席 + 环比 + 趋势 + 授权额度对照。"""
        api_auth(request)
        _require_supervisor(request)
        return build_usage_summary(request, days)

    @app.get("/api/workspace/billing")
    async def api_workspace_billing(request: Request, month: str = "", format: str = "json"):
        """C2-1 对账单：按账期(YYYY-MM)聚合用量 vs 授权席位。

        ``format=csv`` 直接下载对账单 CSV，便于财务存档/导入。
        """
        api_auth(request)
        _require_supervisor(request)
        stmt = build_billing_statement(request, month)
        if str(format or "").lower() == "csv":
            from fastapi.responses import Response

            from src.utils.billing import statement_to_csv

            csv_text = statement_to_csv(stmt)
            fname = f"billing_{stmt.get('period','')}.csv"
            return Response(
                content=csv_text, media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'})
        return stmt
