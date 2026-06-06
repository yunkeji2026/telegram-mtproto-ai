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
        self._alert_rules: list = list(cfg.get("alert_rules") or [])  # O2
        self._stop_evt = asyncio.Event()
        self._running = False
        self.total_sent: int = 0
        self.total_errors: int = 0
        self.total_alerts: int = 0  # O2 预警触发次数
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
        """生成简报、广播到 EventBus，并触发 O2 预警规则评估。"""
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

            # O2: 简报数据复用，直接评估预警规则（零额外 DB 查询）
            await self._evaluate_alert_rules(report_data)

            # S2: 异常检测（在 O2 规则之外，用统计基线自动发现偏差）
            await self._run_anomaly_detection(report_data)
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
            "total_alerts": self.total_alerts,  # O2
            "alert_rules": len(self._alert_rules),
        }

    # ── O2：智能预警规则评估 ─────────────────────────────────────────────

    async def _evaluate_alert_rules(self, report_data: Dict[str, Any]) -> None:
        """O2：评估配置的预警规则，符合条件时发布 csat_alert 事件。

        rules 配置示例（config.yaml）：
          report:
            alert_rules:
              - condition: avg_csat_below
                threshold: 3.5
                message: "团队 CSAT 均分低于 {threshold}"
              - condition: l3l4_rate_above
                threshold: 0.3
                message: "L3/L4 高风险草稿占比超过 {pct}%"
        """
        rules = self._alert_rules
        if not rules:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            csat_data = report_data.get("csat") or {}
            sla_data = report_data.get("sla_stats") or {}

            for rule in rules:
                condition = str(rule.get("condition") or "")
                threshold = float(rule.get("threshold") or 0)
                msg_tpl = str(rule.get("message") or condition)
                triggered = False
                payload: Dict[str, Any] = {}

                if condition == "avg_csat_below":
                    avg = csat_data.get("avg")
                    if avg is not None and avg < threshold:
                        triggered = True
                        payload = {"avg_csat": avg, "threshold": threshold}
                        msg_tpl = msg_tpl.format(threshold=threshold, avg=avg)

                elif condition == "l3l4_rate_above":
                    compliance = float(sla_data.get("compliance_rate") or 100)
                    high_risk_rate = round((100 - compliance) / 100, 3)
                    if high_risk_rate > threshold:
                        triggered = True
                        payload = {"rate": high_risk_rate, "threshold": threshold}
                        pct = round(high_risk_rate * 100, 1)
                        msg_tpl = msg_tpl.format(threshold=threshold, pct=pct)

                elif condition == "force_override_above":
                    forced = int(sla_data.get("force_overrides") or 0)
                    if forced > int(threshold):
                        triggered = True
                        payload = {"force_overrides": forced, "threshold": int(threshold)}
                        msg_tpl = msg_tpl.format(threshold=int(threshold), count=forced)

                if triggered:
                    get_event_bus().publish("csat_alert", {
                        "condition": condition,
                        "message": msg_tpl,
                        "threshold": threshold,
                        **payload,
                    })
                    self.total_alerts += 1
                    logger.warning(
                        "O2 预警触发: %s — %s", condition, msg_tpl,
                    )
        except Exception:
            logger.debug("O2 预警规则评估失败（已忽略）", exc_info=True)

    # ── S2：统计异常检测 ─────────────────────────────────────────────────

    async def _run_anomaly_detection(self, report_data: Dict[str, Any]) -> None:
        """S2：运行统计异常检测，将异常结果发布为 anomaly_alert 事件。"""
        try:
            from src.inbox.anomaly import AnomalyDetector, build_anomaly_alert_payload
            from src.integrations.shared.event_bus import get_event_bus
            detector = AnomalyDetector(self._store, self._cfg)
            if not detector.is_enabled():
                return
            # 从报告数据中提取当前指标值（复用已计算结果，零额外 DB 查询）
            current_metrics: Dict[str, float] = {}
            if report_data.get("avg_csat") is not None:
                current_metrics["csat_avg"] = float(report_data["avg_csat"])
            if report_data.get("l3l4_rate") is not None:
                current_metrics["l3l4_rate"] = float(report_data["l3l4_rate"]) * 100
            if report_data.get("autosend_rate") is not None:
                current_metrics["autosend_rate"] = float(report_data["autosend_rate"]) * 100
            results = detector.run_full_check(current_metrics)
            payload = build_anomaly_alert_payload(
                results,
                detector_cfg=detector._anomaly_cfg(),
            )
            if payload:
                get_event_bus().publish("anomaly_alert", payload)
                self.total_alerts += 1
                logger.warning(
                    "S2 AnomalyDetector: %d 异常已发布", payload["anomaly_count"]
                )
        except Exception:
            logger.debug("S2 异常检测失败（已忽略）", exc_info=True)

    # ── 手动触发（用于测试 / API 按钮） ─────────────────────────────────

    async def trigger(self, period: str = "daily") -> None:
        """立即生成并推送指定类型简报（不受防重触发限制）。"""
        await self._send(period)
