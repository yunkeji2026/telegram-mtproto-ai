"""D3 健康看门狗 —— 周期巡检 D1 运行时健康，异常时主动告警（EventBus → Webhook）。

设计要点
========
- **复用 D1 检测**：直接调用 :func:`collect_health`（与 ``/api/admin/health`` 同口径），
  不重复造检测逻辑。
- **复用既有投递**：异常时 ``EventBus.publish("health_alert", ...)``，由 ``WebhookNotifier``
  按订阅推送（Telegram/WhatsApp/Messenger/JSON），无需新投递通道。
- **去抖**：仅在「健康签名变化」时告警（如新组件转 fail / 恢复），避免每个巡检周期刷屏；
  WebhookNotifier 自身的 1/小时速率限制是第二层兜底。
- **恢复通知**：从异常恢复到全绿时补发一条「已恢复」，闭环值班体验。

:func:`collect_health` 为采集器（route 与 watchdog 共用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _collect_workers(state) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    specs = [
        ("autosend", "L2 自动发送 Worker", "autosend_worker"),
        ("autoclaim", "自动认领 Worker", "auto_claim_worker"),
    ]
    for wid, name, attr in specs:
        w = getattr(state, attr, None)
        if w is None:
            out.append({"id": wid, "name": name, "present": False})
            continue
        snap: Dict[str, Any] = {}
        try:
            snap = w.status_snapshot()
        except Exception:
            logger.debug("worker %s 快照失败（已忽略）", attr, exc_info=True)
        out.append({
            "id": wid, "name": name, "present": True,
            "running": bool(snap.get("running")),
            "circuit_open": bool(snap.get("circuit_open")),
            "last_error": snap.get("last_error", ""),
        })
    return out


def _pending_drafts(state) -> Optional[int]:
    svc = getattr(state, "draft_service", None)
    if svc is None or not hasattr(svc, "list_drafts"):
        return None
    try:
        rows = svc.list_drafts(status="pending", limit=1000)
        return len(rows or [])
    except Exception:
        logger.debug("草稿队列统计失败（已忽略）", exc_info=True)
        return None


def collect_health(app, config_manager=None, *, pending_threshold: int = 200) -> Dict[str, Any]:
    """采集运行时健康（route 与 watchdog 共用）。返回 build_health 的结果。"""
    from src.utils.health import build_health, is_placeholder

    state = getattr(app, "state", app)
    config = getattr(config_manager, "config", None) or {}

    inbox = getattr(state, "inbox_store", None)
    db_ok = bool(inbox.ping()) if (inbox is not None and hasattr(inbox, "ping")) else False

    ai = config.get("ai") or {}
    ai_provider = str(ai.get("provider") or "").strip()
    ai_key_ok = not is_placeholder(ai.get("api_key"))

    lic_state = lic_plan = ""
    lic_ro = False
    try:
        from src.licensing import get_license_manager
        st = get_license_manager().status()
        lic_state, lic_plan, lic_ro = st.state, st.plan, bool(st.read_only)
    except Exception:
        logger.debug("授权状态读取失败（已忽略）", exc_info=True)

    ready = configured = total = 0
    try:
        from src.utils.channel_setup import channel_status
        chs = channel_status(config)
        total = len(chs)
        ready = sum(1 for c in chs if c.get("ready"))
        configured = sum(1 for c in chs if c.get("configured"))
    except Exception:
        logger.debug("渠道状态读取失败（已忽略）", exc_info=True)

    return build_health(
        db_ok=db_ok,
        ai_provider=ai_provider, ai_key_ok=ai_key_ok,
        license_state=lic_state, license_read_only=lic_ro, license_plan=lic_plan,
        channels_ready=ready, channels_configured=configured, channels_total=total,
        workers=_collect_workers(state),
        pending_drafts=_pending_drafts(state),
        pending_threshold=pending_threshold,
    )


def health_signature(health: Dict[str, Any]) -> str:
    """把「异常组件集合」压成签名，用于去抖（只在变化时告警）。"""
    bad = sorted(
        f"{c.get('id')}:{c.get('status')}"
        for c in (health.get("components") or [])
        if c.get("status") in ("fail", "warn")
    )
    return "|".join(bad)


def problems_of(health: Dict[str, Any]) -> List[Dict[str, Any]]:
    """提取需要告警的异常组件（fail + warn）。"""
    return [
        {"id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
         "detail": c.get("detail")}
        for c in (health.get("components") or [])
        if c.get("status") in ("fail", "warn")
    ]


class HealthWatchdog:
    """周期巡检运行时健康，状态变化时经 EventBus 发 ``health_alert``。

    Usage::

        wd = HealthWatchdog(app=web_app, config_manager=cm, interval_sec=300)
        asyncio.create_task(wd.run())
        wd.stop()
    """

    def __init__(
        self,
        *,
        app,
        config_manager=None,
        interval_sec: float = 300.0,
        pending_threshold: int = 200,
        alert_on_warn: bool = False,
    ) -> None:
        self._app = app
        self._config_manager = config_manager
        self._interval = max(30.0, float(interval_sec))
        self._pending_threshold = int(pending_threshold)
        # 默认只对 fail（red）告警；warn 噪音大，可显式开
        self._alert_on_warn = bool(alert_on_warn)
        self._stop_evt = asyncio.Event()
        self._running = False
        self._last_sig: Optional[str] = None
        self._last_light: str = "green"
        self.total_alerts: int = 0
        self.total_recoveries: int = 0
        self.last_check_ts: float = 0.0
        self.last_light: str = "green"

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info("HealthWatchdog 已启动（interval=%.0fs alert_on_warn=%s）",
                    self._interval, self._alert_on_warn)
        # 启动后稍等，避开冷启动期的瞬时 fail（worker 尚未 running）
        try:
            await asyncio.wait_for(self._stop_evt.wait(), timeout=min(60.0, self._interval))
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop_evt.is_set():
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._tick)
            except Exception:
                logger.debug("HealthWatchdog tick 异常（已忽略）", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                break
            except asyncio.TimeoutError:
                pass
        self._running = False
        logger.info("HealthWatchdog 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    def _tick(self) -> None:
        health = collect_health(self._app, self._config_manager,
                                pending_threshold=self._pending_threshold)
        self.last_check_ts = time.time()
        light = str(health.get("light") or "green")
        self.last_light = light

        # 决定「是否处于告警态」：red 必告；yellow 仅在开关打开时告
        alerting = (light == "red") or (light == "yellow" and self._alert_on_warn)
        sig = health_signature(health) if alerting else ""

        if alerting:
            if sig != self._last_sig:
                self._emit_alert(health)
                self.total_alerts += 1
            self._last_sig = sig
            self._last_light = light
        else:
            # 从异常恢复 → 补发恢复通知
            if self._last_light in ("red", "yellow") and self._last_sig:
                self._emit_recovery(health)
                self.total_recoveries += 1
            self._last_sig = None
            self._last_light = light

    def _emit_alert(self, health: Dict[str, Any]) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("health_alert", {
                "light": health.get("light"),
                "problems": problems_of(health),
                "summary": health.get("summary"),
                "recovered": False,
            })
            logger.warning("HealthWatchdog 发出健康告警：light=%s 异常 %d 项",
                           health.get("light"),
                           len(problems_of(health)))
        except Exception:
            logger.debug("health_alert 发布失败（已忽略）", exc_info=True)

    def _emit_recovery(self, health: Dict[str, Any]) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("health_alert", {
                "light": "green", "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出恢复通知")
        except Exception:
            logger.debug("health recovery 发布失败（已忽略）", exc_info=True)

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "interval_sec": self._interval,
            "alert_on_warn": self._alert_on_warn,
            "total_alerts": self.total_alerts,
            "total_recoveries": self.total_recoveries,
            "last_check_ts": self.last_check_ts,
            "last_light": self.last_light,
        }
