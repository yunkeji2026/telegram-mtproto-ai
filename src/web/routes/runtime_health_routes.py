"""D1 运行时健康检查聚合端点（AI 客服栈）。

``GET /api/admin/health`` —— 把 DB / AI / 授权 / 渠道 / 后台 worker / 草稿队列的运行时
信号聚成一张红绿灯。复用 :func:`src.inbox.health_watchdog.collect_health`（与 D3 看门狗
同口径），不重复造采集逻辑。

与 ``health_routes.py``（/api/health-check、配置/策略域巡检）正交：本模块聚焦 AI 客服
运行时栈的存活/积压/熔断。
"""

from __future__ import annotations

import logging
import time

from fastapi import Request

logger = logging.getLogger(__name__)


def _worker_snapshots(request: Request):
    """收集各后台 worker 的 status_snapshot（带 id/name），用于可靠性聚合。"""
    out = []
    specs = [
        ("autosend", "L2 自动发送", "autosend_worker"),
        ("webhook", "告警推送", "webhook_notifier"),
        ("autoclaim", "自动认领", "auto_claim_worker"),
        ("watchdog", "健康看门狗", "health_watchdog"),
    ]
    for wid, name, attr in specs:
        w = getattr(request.app.state, attr, None)
        if w is None or not hasattr(w, "status_snapshot"):
            continue
        try:
            snap = w.status_snapshot()
        except Exception:
            logger.debug("worker %s 快照失败（已忽略）", attr, exc_info=True)
            continue
        snap = dict(snap or {})
        snap.setdefault("id", wid)
        snap.setdefault("name", name)
        out.append(snap)
    return out


def _recent_health_alerts(limit: int = 20):
    """从 EventBus 历史取最近的 health_alert 事件（近期告警）。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        evts = get_event_bus().recent_events(50)
        alerts = [
            {"ts": e.get("ts"), "light": (e.get("data") or {}).get("light"),
             "recovered": bool((e.get("data") or {}).get("recovered")),
             "problems": (e.get("data") or {}).get("problems") or []}
            for e in evts if e.get("type") == "health_alert"
        ]
        return alerts[-limit:]
    except Exception:
        logger.debug("读取近期 health_alert 失败（已忽略）", exc_info=True)
        return []


def register_runtime_health_routes(app, *, api_auth, config_manager=None) -> None:
    @app.get("/api/admin/health")
    async def api_admin_health(request: Request):
        """运行时健康红绿灯：DB / AI / 授权 / 渠道 / worker / 队列。"""
        api_auth(request)
        from src.inbox.health_watchdog import collect_health

        return collect_health(request.app, config_manager)

    @app.get("/api/admin/reliability")
    async def api_admin_reliability(request: Request, hours: int = 24):
        """D2 运维可靠性：worker 错误率 + 处置量/拦截率趋势 + 近期告警。"""
        api_auth(request)
        from src.utils.reliability import build_reliability

        hours = 24 if int(hours or 24) <= 24 else 168
        inbox = getattr(request.app.state, "inbox_store", None)
        timeline = []
        if inbox is not None and hasattr(inbox, "get_reliability_timeline"):
            since = time.time() - hours * 3600
            bucket = 3600 if hours <= 24 else 86400
            try:
                timeline = inbox.get_reliability_timeline(since, bucket_sec=bucket)
            except Exception:
                logger.debug("可靠性时间线读取失败（已忽略）", exc_info=True)
        return build_reliability(
            worker_snapshots=_worker_snapshots(request),
            timeline=timeline,
            recent_alerts=_recent_health_alerts(),
            window_hours=hours,
        )
