"""N2 — 定时简报推送（ScheduledReporter）。

每分钟轮询本地时间，在配置的时刻自动生成日报/周报并广播到 EventBus，
由 WebhookNotifier（L2）透传至企业 IM（DingTalk/Feishu/WeCom）。

设计决策：
  - 基于时钟轮询（每 60 秒一次），精度 ±1 分钟，对简报场景足够
  - 不依赖 APScheduler / Celery，零额外依赖
  - last_sent 存内存（重启后补发一次可接受），无需 DB
  - 时区通过 tz_offset_hours 配置，默认 UTC+8

config.yaml 示例：
  report:
    enabled: true
    daily_time: "09:00"       # HH:MM 本地时间，每天发日报
    weekly_day: "monday"      # monday/tuesday/…/sunday，留空=不发周报
    tz_offset_hours: 8        # 时区偏移
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class ScheduledReporter:
    """N2：定时简报推送后台任务。

    启动：
        reporter = ScheduledReporter(inbox_store, cfg)
        asyncio.create_task(reporter.run())

    停止：
        reporter.stop()
    """

    def __init__(
        self,
        inbox_store: Any,
        draft_service: Any = None,
        app_state: Any = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        self._store = inbox_store
        self._svc = draft_service
        self._app_state = app_state
        self._daily_time: str = str(cfg.get("daily_time") or "09:00").strip()
        self._weekly_day: str = str(cfg.get("weekly_day") or "").lower().strip()
        self._tz_offset: int = int(cfg.get("tz_offset_hours") or 8)
        self._stop_evt = asyncio.Event()
        self._running = False
        self.total_sent: int = 0
        self.total_errors: int = 0
        self._last_daily: Optional[str] = None   # "YYYY-MM-DD"
        self._last_weekly: Optional[str] = None  # "YYYY-WNN"
        self._tick_secs: int = int(cfg.get("tick_secs") or 60)

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info(
            "ScheduledReporter 已启动（daily=%s weekly=%s tz=UTC+%d）",
            self._daily_time, self._weekly_day or "禁用", self._tz_offset,
        )
        try:
            while not self._stop_evt.is_set():
                try:
                    await self._check()
                except Exception:
                    logger.debug("ScheduledReporter tick 异常（已忽略）", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=float(self._tick_secs))
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            logger.info("ScheduledReporter 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 调度逻辑 ─────────────────────────────────────────────────────────

    def _now_local(self) -> float:
        """返回本地时间戳（UTC + tz_offset），测试时可 monkeypatch。"""
        return time.time() + self._tz_offset * 3600

    async def _check(self) -> None:
        """检查当前时刻是否需要发送简报（每 tick 调用一次）。"""
        now_ts = self._now_local()
        dt = datetime.datetime.utcfromtimestamp(now_ts)
        today_str = dt.strftime("%Y-%m-%d")
        week_str = dt.strftime("%Y-W%W")
        hour_min = dt.strftime("%H:%M")

        # 解析目标时间
        try:
            h, m = self._daily_time.split(":", 1)
            target_hm = f"{int(h):02d}:{int(m):02d}"
        except Exception:
            target_hm = "09:00"

        # 日报：每天在目标时刻发送一次
        if hour_min == target_hm and self._last_daily != today_str:
            self._last_daily = today_str
            logger.info("ScheduledReporter → 触发日报推送 (%s)", today_str)
            await self._send("daily")

        # 周报：每周指定星期在目标时刻发送一次
        if self._weekly_day and hour_min == target_hm:
            target_wd = _WEEKDAY_MAP.get(self._weekly_day, -1)
            if target_wd >= 0 and dt.weekday() == target_wd and self._last_weekly != week_str:
                self._last_weekly = week_str
                logger.info("ScheduledReporter → 触发周报推送 (%s)", week_str)
                await self._send("weekly")

    async def _send(self, period: str) -> None:
        """生成简报并广播到 EventBus。"""
        try:
            from src.inbox.report_generator import ReportGenerator
            from src.integrations.shared.event_bus import get_event_bus
            gen = ReportGenerator(
                inbox_store=self._store,
                draft_service=self._svc,
                app_state=self._app_state,
            )
            report_data = gen.generate(period=period)
            text = gen.format_text(report_data)
            get_event_bus().publish("report", {
                "period": period,
                "period_label": report_data.get("period_label", ""),
                "text": text,
                "scheduled": True,
            })
            self.total_sent += 1
            logger.info("ScheduledReporter %s 简报已发布到 EventBus（total_sent=%d）", period, self.total_sent)
        except Exception:
            self.total_errors += 1
            logger.warning("ScheduledReporter %s 简报推送失败", period, exc_info=True)

    # ── 运行状态快照 ──────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "daily_time": self._daily_time,
            "weekly_day": self._weekly_day or None,
            "tz_offset": self._tz_offset,
            "last_daily": self._last_daily,
            "last_weekly": self._last_weekly,
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
        }

    # ── 手动触发（用于测试 / API 按钮） ─────────────────────────────────

    async def trigger(self, period: str = "daily") -> None:
        """立即生成并推送指定类型简报（不受防重触发限制）。"""
        await self._send(period)
