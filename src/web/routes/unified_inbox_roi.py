"""统一收件箱——ROI/转化门面看板（P0-3）。

给**老板/采购视角**的只读概览：把已有的经营/SLA/翻译/自动化数据蒸馏成几个
「能证明价值」的 KPI——AI 自动应答占比、人工接管数、首响达标率、引流成功数、
节省人力估算——并叠加一张「配置健康度」卡（复用 P0-1 ``check_config``）。

设计原则：**门面 = 重新呈现，不新造事实源**。
- 经营/首响指标复用 ``unified_inbox_reports._daily_report_rows``（与日报/仪表盘同源）；
- AI vs 人工拆分用 ``InboxStore.get_automation_roi_stats``（draft_audit_log 真实口径）；
- 配置健康度用 ``config_check.check_config``（与 ``--check`` 同一校验器）。

``register_roi_routes(app, *, api_auth, config_manager)`` 挂 ``GET /api/workspace/roi``
（主管专属）。聚合主体 :func:`build_roi_summary` 为纯函数，便于单测。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request

from src.web.routes.unified_inbox_auth import _require_supervisor
from src.web.routes.unified_inbox_reports import _daily_report_rows
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)

# 节省人力估算：每条 AI 自动应答约等于坐席省下的处理时长（秒）。
# 取保守默认 180s（含读取+思考+打字+等待），可由 config.workspace.roi.sec_per_reply 覆盖。
_DEFAULT_SEC_PER_REPLY = 180


def _roi_cfg(config_manager) -> Dict[str, Any]:
    try:
        full = getattr(config_manager, "config", None) or {}
        return dict(((full.get("workspace") or {}).get("roi") or {}))
    except Exception:
        return {}


def _pct_delta(cur: float, prev: float) -> Optional[float]:
    """环比百分比（cur 相对 prev）。prev=0 时无意义返回 None（前端显示「—」）。"""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def build_roi_summary(
    request: Request, config_manager=None, span: int = 7,
) -> Dict[str, Any]:
    """聚合老板视角 ROI 概览（纯函数；store 缺失时各段优雅降级为 0）。"""
    span = 30 if int(span or 7) >= 30 else 7
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    since = midnight - (span - 1) * 86400

    out: Dict[str, Any] = {"ok": True, "days": span}

    # ── 经营/首响：复用日报逐日表（与 daily-report.csv / dashboard 同源）──
    new_contacts = leads = conversions = 0
    frt_count = frt_responded = frt_attain = 0
    try:
        for r in _daily_report_rows(request, span):
            new_contacts += r["new_contacts"]
            leads += r["leads"]
            conversions += r["conversions"]
            frt_count += r["frt_count"]
            frt_responded += r["frt_responded"]
            frt_attain += round(r["frt_attain_rate"] / 100 * r["frt_responded"])
    except Exception:
        logger.debug("ROI 经营指标聚合失败（已忽略）", exc_info=True)
    out["business"] = {
        "new_contacts": new_contacts,
        "leads": leads,
        "conversions": conversions,
        "lead_rate": round(leads / new_contacts * 100, 1) if new_contacts else 0.0,
        "conversion_rate": round(conversions / leads * 100, 1) if leads else 0.0,
        "frt_attain_rate": round(frt_attain / frt_responded * 100, 1) if frt_responded else 0.0,
        "frt_responded": frt_responded,
    }

    # ── AI vs 人工 + 节省人力/金额 + 环比 ──────────────────────────────
    ai_sent = human_sent = 0
    automation: Dict[str, Any] = {}
    prev_ai = prev_human = 0
    prev_since = since - span * 86400
    try:
        inbox = _inbox_store(request)
        if inbox is not None and hasattr(inbox, "get_automation_roi_stats"):
            automation = inbox.get_automation_roi_stats(since)
            ai_sent = int(automation.get("ai_sent", 0))
            human_sent = int(automation.get("human_sent", 0))
            try:
                prev = inbox.get_automation_roi_stats(prev_since, until_ts=since)
                prev_ai = int(prev.get("ai_sent", 0))
                prev_human = int(prev.get("human_sent", 0))
            except TypeError:
                # 旧 store 不支持 until_ts：放弃环比（不阻断主体）
                logger.debug("store 不支持 until_ts，跳过环比", exc_info=True)
    except Exception:
        logger.debug("ROI 自动化指标聚合失败（已忽略）", exc_info=True)
    cfg = _roi_cfg(config_manager)
    sec_per_reply = int(cfg.get("sec_per_reply") or _DEFAULT_SEC_PER_REPLY)
    cost_per_hour = float(cfg.get("cost_per_hour") or 0)
    saved_sec = ai_sent * max(0, sec_per_reply)
    saved_hours = round(saved_sec / 3600, 1)
    total_sent = ai_sent + human_sent
    prev_total = prev_ai + prev_human
    cur_share = ai_sent / total_sent * 100 if total_sent else 0.0
    prev_share = prev_ai / prev_total * 100 if prev_total else 0.0
    out["automation"] = {
        "ai_sent": ai_sent,
        "human_sent": human_sent,
        "total_sent": total_sent,
        "ai_share_pct": round(cur_share, 1),
        "suppressed": int(automation.get("suppressed", 0)),
        "saved_hours": saved_hours,
        "sec_per_reply": sec_per_reply,
        # 金额化（cost_per_hour=0 时前端隐藏该卡）
        "cost_per_hour": cost_per_hour,
        "saved_money": round(saved_hours * cost_per_hour, 2) if cost_per_hour else 0.0,
        "trend": automation.get("trend", []),
        # 环比（上一等长窗口）；prev 基数为 0 时各项为 None → 前端显示「—」
        "compare": {
            "ai_sent": prev_ai,
            "total_sent": prev_total,
            "ai_sent_delta_pct": _pct_delta(ai_sent, prev_ai),
            "ai_share_delta_pp": round(cur_share - prev_share, 1) if prev_total else None,
            "saved_hours_delta_pct": _pct_delta(ai_sent, prev_ai),
        },
    }

    # ── 配置健康度卡（复用 P0-1 校验器）────────────────────────────────
    out["config_health"] = _config_health(config_manager)
    return out


def _config_health(config_manager) -> Dict[str, Any]:
    """跑 check_config，返回 error/warn 计数 + 前几条供 UI 展示。"""
    try:
        from src.utils.config_check import check_config
    except Exception:
        return {"available": False}
    try:
        config = getattr(config_manager, "config", None)
        path = getattr(config_manager, "config_path", None)
        if not isinstance(config, dict):
            return {"available": False}
        issues = check_config(config, config_path=path)
    except Exception:
        logger.debug("ROI 配置健康度检查失败（已忽略）", exc_info=True)
        return {"available": False}
    errors = [i for i in issues if i.severity == "error"]
    warns = [i for i in issues if i.severity == "warn"]
    top = [
        {"severity": i.severity, "path": i.path, "message": i.message}
        for i in (errors + warns)[:5]
    ]
    status = "error" if errors else ("warn" if warns else "ok")
    return {
        "available": True,
        "status": status,
        "errors": len(errors),
        "warnings": len(warns),
        "top_issues": top,
    }


def register_roi_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载 ROI 门面看板端点（GET /api/workspace/roi，主管专属）。"""

    @app.get("/api/workspace/roi")
    async def api_workspace_roi(request: Request, days: int = 7):
        """老板视角 ROI 概览：AI 占比/节省人力/引流转化/首响达标 + 配置健康度。"""
        api_auth(request)
        _require_supervisor(request)
        return build_roi_summary(request, config_manager, days)
