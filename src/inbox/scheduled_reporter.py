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
        # P32：SLA 坐席告警防重状态（conversation_id → last_alerted_ts）
        self._sla_agent_alerted: Dict[str, float] = {}
        # P32：SLA 阈值（秒）从 config 读取，回落合理默认值
        _sla_cfg = cfg.get("sla_agent_alert") or {}
        self._sla_warn_sec: int = int(_sla_cfg.get("warn_sec") or 300)    # 5 分钟
        self._sla_crit_sec: int = int(_sla_cfg.get("crit_sec") or 900)    # 15 分钟
        self._sla_renotify_sec: int = int(_sla_cfg.get("renotify_sec") or 900)  # 15 分钟内不重复
        # P36：自动归档定时器（每 N tick 触发一次，默认 60 tick = 1小时）
        _aa_cfg = cfg.get("auto_archive") or {}
        self._auto_archive_idle_hours: int = int(_aa_cfg.get("idle_hours") or 24)
        self._auto_archive_interval_ticks: int = int(_aa_cfg.get("check_interval_ticks") or 60)
        self._auto_archive_tick_count: int = 0  # 当前 tick 计数器

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
        # P32：每 tick 检查 SLA 坐席告警
        await self._run_sla_agent_alerts()

        # P36：定期（每 hour）自动归档+质检评分
        self._auto_archive_tick_count += 1
        if self._auto_archive_tick_count >= self._auto_archive_interval_ticks:
            self._auto_archive_tick_count = 0
            await self._run_auto_archive_and_qa()

        # P44：工作链自动执行（每 tick）
        await self._run_workflow_chains()

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

    # ── P32：SLA 坐席告警（每 tick 运行，精准推送给负责坐席）────────────

    async def _run_sla_agent_alerts(self) -> None:
        """P32：检查未回复会话是否超时，向负责坐席发布定向 queue_alert 事件。

        告警策略：
          - warn_sec 以上：向 claimed_by 坐席发 warn 级告警
          - crit_sec 以上：发 crit 级告警（颜色更深，toast 更醒目）
          - 同一会话 renotify_sec 内不重复告警（防轰炸）
          - 未认领会话（claimed_by 为空）发布 broadcast 告警（所有在线坐席可收）
        """
        if self._store is None:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            now = time.time()

            # 清理过期防重记录
            self._sla_agent_alerted = {
                k: v for k, v in self._sla_agent_alerted.items()
                if now - v < self._sla_renotify_sec * 2
            }

            # 仅拉取近期活跃（last_ts 在 SLA crit 窗口内）且未归档的会话
            # 优化：crit_sec * 4 作为扫描窗口，排除长期静默会话，减少全表扫描开销
            convs = self._store.list_conversations(limit=500)
            if not convs:
                return
            # 预过滤：只关注最近有活动的会话（last_ts 在 crit_sec * 6 以内）
            window_cutoff = now - self._sla_crit_sec * 6
            convs = [c for c in convs if float(c.get("last_ts") or 0) >= window_cutoff]
            if not convs:
                return
            cids = [str(c.get("conversation_id") or "") for c in convs if c.get("conversation_id")]
            last_dirs = self._store.last_message_dirs(cids)

            bus = get_event_bus()
            alerted_count = 0

            for c in convs:
                cid = str(c.get("conversation_id") or "")
                if not cid:
                    continue

                # 只有末条为入站（客户消息未被坐席回复）才计入等待
                info = last_dirs.get(cid, {})
                if not info or info.get("direction") != "in":
                    continue

                wait_sec = max(0, int(now - (info.get("ts") or now)))
                if wait_sec < self._sla_warn_sec:
                    continue  # 未到警告阈值

                # 防重：该会话最近是否已告警
                last_alerted = self._sla_agent_alerted.get(cid, 0.0)
                if now - last_alerted < self._sla_renotify_sec:
                    continue

                # 获取会话基础信息
                display_name = str(c.get("display_name") or c.get("chat_key") or "未知客户")
                platform = str(c.get("platform") or "")
                claimed_by = str(c.get("claimed_by") or "").strip()

                # 获取 claimed_by — 优先从 conv_meta 读（持久化）
                try:
                    meta = self._store.get_conv_meta(cid) or {}
                    claimed_by = str(meta.get("claimed_by") or claimed_by).strip()
                except Exception:
                    pass

                sla_level = "crit" if wait_sec >= self._sla_crit_sec else "warn"
                wait_min = round(wait_sec / 60, 1)

                event_data = {
                    "conversation_id": cid,
                    "display_name": display_name,
                    "platform": platform,
                    "wait_sec": wait_sec,
                    "wait_min": wait_min,
                    "sla_level": sla_level,
                    "to_agent_id": claimed_by or None,  # None = 广播给所有在线坐席
                    "ts": now,
                }

                bus.publish("queue_alert", event_data)

                self._sla_agent_alerted[cid] = now
                alerted_count += 1
                logger.info(
                    "P32 SLA 坐席告警: %s 等待 %.1f分钟 [%s] → %s",
                    display_name, wait_min, sla_level, claimed_by or "全体"
                )

            if alerted_count > 0:
                self.total_alerts += alerted_count

        except Exception:
            logger.debug("P32 SLA 坐席告警检查失败（已忽略）", exc_info=True)

    # ── P36：自动归档 + QA 质检评分（每小时运行）────────────────────────

    async def _run_auto_archive_and_qa(self) -> None:
        """P36：自动归档长时间无活动会话，同时计算 QA 评分并持久化。

        处理流程：
          1. 从 InboxStore 拉取满足条件的候选（idle >= idle_hours + 未归档 + 未 auto_archived）
          2. 对每条会话：计算 QA 评分 → 生成规则摘要 → 标记归档 → 发布 conv_archived 事件
          3. 上限 50 条/次，避免批量阻塞
        """
        if self._store is None:
            return
        try:
            from src.integrations.shared.event_bus import get_event_bus
            candidates = self._store._auto_archive_candidates(self._auto_archive_idle_hours)
            if not candidates:
                return

            bus = get_event_bus()
            archived_count = 0
            now = time.time()

            for c in candidates[:50]:
                cid = str(c.get("conversation_id") or "")
                if not cid:
                    continue
                try:
                    # 步骤 1：计算 QA 评分（写入 conversation_meta.qa_score）
                    qa = self._store.compute_and_store_qa_score(cid)

                    # 步骤 2：生成规则摘要（从最后 N 条消息构造）
                    summary = self._build_auto_summary(cid, qa)

                    # 步骤 3：归档 + 更新 auto_archived_at + 写入摘要
                    self._store.update_conv_meta(cid, {
                        "archived": 1,
                        "auto_archived_at": now,
                        "summary": summary,
                    })

                    # 步骤 4：发布归档事件（Webhook / SSE 透传）
                    bus.publish("conv_archived", {
                        "conversation_id": cid,
                        "display_name": c.get("display_name", ""),
                        "platform": c.get("platform", ""),
                        "reason": "auto_idle",
                        "idle_hours": self._auto_archive_idle_hours,
                        "qa_score": qa.get("score"),
                        "summary": summary[:200] if summary else "",
                        "ts": now,
                    })
                    archived_count += 1
                    logger.info(
                        "P36 自动归档: %s [QA=%s] 已归档（idle≥%dh）",
                        c.get("display_name", cid), qa.get("score", "?"), self._auto_archive_idle_hours
                    )
                except Exception:
                    logger.debug("P36 自动归档单条失败（已跳过）", exc_info=True)

            if archived_count:
                self.total_sent += archived_count
                logger.info("P36 本轮自动归档 %d 条会话", archived_count)

        except Exception:
            logger.debug("P36 自动归档任务失败（已忽略）", exc_info=True)

    def _build_auto_summary(self, conversation_id: str, qa: Dict[str, Any]) -> str:
        """P36：基于消息记录生成规则摘要（LLM 不可用时的可靠兜底）。"""
        try:
            with self._store._lock:
                rows = self._store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ?
                       ORDER BY ts ASC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
            if not rows:
                return ""
            msgs = [dict(r) for r in rows]
            total = len(msgs)
            in_msgs = [m for m in msgs if m["direction"] in ("in", "inbound")]
            out_msgs = [m for m in msgs if m["direction"] in ("out", "outbound")]
            last_in = next((m["text"] for m in reversed(msgs)
                           if m["direction"] in ("in", "inbound") and m["text"]), "")
            score = qa.get("score", -1)
            grade = qa.get("grade", "N/A")
            duration_h = round((time.time() - float(rows[0]["ts"] or 0)) / 3600, 1)
            parts = [
                f"会话共 {total} 条消息（客户 {len(in_msgs)} 条，坐席 {out_msgs.__len__()} 条）",
                f"持续约 {duration_h} 小时",
            ]
            if last_in:
                parts.append(f"客户最后表达：「{last_in[:50]}{'…' if len(last_in)>50 else ''}」")
            if score >= 0:
                parts.append(f"质检评分 {score}/100（{grade} 级）")
            return "；".join(parts) + "。"
        except Exception:
            return ""

    # ── P44：工作链自动执行 ───────────────────────────────────────────────

    async def _run_workflow_chains(self) -> None:
        """P44：处理到期工作链步骤 + 按条件自动启动新链。"""
        if self._store is None:
            return
        try:
            from src.inbox.workflow_runner import WorkflowRunner
            contacts = None
            if self._app_state is not None:
                cs = getattr(getattr(self._app_state, "contacts", None), "store", None)
                contacts = cs
            runner = WorkflowRunner(self._store, contacts_store=contacts)
            n = runner.process_due_executions()
            # 自动启动：每 60 tick（约 1h）扫描一次，避免每 tick 全表扫
            if not hasattr(self, "_wf_auto_tick"):
                self._wf_auto_tick = 0
            self._wf_auto_tick += 1
            if self._wf_auto_tick >= 60:
                self._wf_auto_tick = 0
                started = runner.auto_start_chains()
                if started:
                    logger.info("P44 WorkflowRunner 自动启动 %d 条工作链", started)
            if n:
                logger.info("P44 WorkflowRunner 执行 %d 条到期步骤", n)
        except Exception:
            logger.debug("P44 工作链执行失败（已忽略）", exc_info=True)

    # ── 手动触发（用于测试 / API 按钮） ─────────────────────────────────

    async def trigger(self, period: str = "daily") -> None:
        """立即生成并推送指定类型简报（不受防重触发限制）。"""
        await self._send(period)
