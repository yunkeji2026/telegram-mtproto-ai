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
        billing_interval_sec: float = 3600.0,
        incident_retention_days: float = 30.0,
        weekly_report_enabled: bool = False,
        weekly_interval_sec: float = 604800.0,
    ) -> None:
        self._app = app
        self._config_manager = config_manager
        self._interval = max(30.0, float(interval_sec))
        self._pending_threshold = int(pending_threshold)
        # 计费巡检比健康巡检稀疏（默认 1h）：对账单是月窗聚合，无需每个健康周期都算。
        self._billing_interval = max(self._interval, float(billing_interval_sec))
        self._last_billing_check_ts = 0.0
        # 已关闭事件保留期（天）；<=0 关闭清理。每日节流跑一次 DELETE，防表无限膨胀。
        self._retention_days = float(incident_retention_days)
        self._purge_interval = 86400.0
        self._last_purge_ts = 0.0
        # H1：运营周报自动外发（默认关，遵循「新子系统默认 enabled:false」）。
        # _last_weekly_ts 初始化为「现在」→ 首份周报在启动一个周期后才发，避免每次重启刷屏。
        self._weekly_enabled = bool(weekly_report_enabled)
        self._weekly_interval = max(3600.0, float(weekly_interval_sec))
        self._last_weekly_ts = time.time()
        self.total_weekly_reports: int = 0
        # 默认只对 fail（red）告警；warn 噪音大，可显式开
        self._alert_on_warn = bool(alert_on_warn)
        self._stop_evt = asyncio.Event()
        self._running = False
        self._last_sig: Optional[str] = None
        self._last_light: str = "green"
        self._last_billing_sig: Optional[str] = None
        # 草稿质量告警去抖（记忆命中率/p95 延迟/风险分类回检）
        self._last_draft_quality_sig: Optional[str] = None
        # AI 回复质量退化告警去抖（采纳/弃用率 + 高危量环比，基于 ai_safety_summary）
        self._last_ai_quality_sig: Optional[str] = None
        # 实时语音通话退化告警去抖（主机健康/接通率/不可达，基于 RealtimeVoiceStats）
        self._last_realtime_voice_sig: Optional[str] = None
        # 记忆 key 漂移巡检（裸 key 复发）——结构性数据，独立稀疏节流 + 去抖
        self._last_drift_check_ts: float = 0.0
        self._last_drift_sig: Optional[str] = None
        self.total_alerts: int = 0
        self.total_recoveries: int = 0
        self.total_billing_alerts: int = 0
        self.total_draft_quality_alerts: int = 0
        self.total_ai_quality_alerts: int = 0
        self.total_realtime_voice_alerts: int = 0
        self.total_memory_key_drift_alerts: int = 0
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

    def _evaluate_health(self) -> Dict[str, Any]:
        """采集健康并按签名变化 emit 告警/恢复（_tick 与 recheck 共用）。返回 health。"""
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
        return health

    def recheck(self) -> Dict[str, Any]:
        """按需立即重巡健康（H2 一键动作）：复用 _evaluate_health，会即时开/关事件。

        只跑健康部分（不触发计费/清理/周报），让主管修复后点一下即可看到事件自动恢复。
        """
        return self._evaluate_health()

    def _tick(self) -> None:
        self._evaluate_health()

        # E3：计费异常巡检（超席位/超额），独立去抖，经 D3 通道外发。
        try:
            self._check_billing()
        except Exception:
            logger.debug("计费巡检异常（已忽略）", exc_info=True)

        # 统一草稿引擎质量巡检（记忆命中率/p95/风险分类回检），独立去抖。
        try:
            self._check_draft_quality()
        except Exception:
            logger.debug("草稿质量巡检异常（已忽略）", exc_info=True)

        # AI 回复质量退化巡检（采纳/弃用率 + 高危量环比），默认关、独立去抖。
        try:
            self._check_ai_quality()
        except Exception:
            logger.debug("AI 质量巡检异常（已忽略）", exc_info=True)

        # 实时语音通话退化巡检（主机健康/接通率/不可达），默认关、独立去抖。
        try:
            self._check_realtime_voice()
        except Exception:
            logger.debug("实时语音告警巡检异常（已忽略）", exc_info=True)

        # 实时语音趋势落库兜底 sync（旁路漏记时补写当日增量）。
        try:
            self._sync_realtime_voice_trend()
        except Exception:
            logger.debug("实时语音趋势 sync 异常（已忽略）", exc_info=True)

        # 记忆 key 漂移巡检（裸 key 复发 → 记忆对引擎不可见），稀疏节流 + 独立去抖。
        try:
            self._check_memory_key_drift()
        except Exception:
            logger.debug("记忆 key 漂移巡检异常（已忽略）", exc_info=True)

        # 运维卫生：按保留期清理已关闭事件（每日节流一次）。
        try:
            self._maybe_purge_incidents()
        except Exception:
            logger.debug("事件清理异常（已忽略）", exc_info=True)

        # H1：运营周报自动外发（每周节流一次，默认关）。
        try:
            self._maybe_weekly_report()
        except Exception:
            logger.debug("运营周报生成异常（已忽略）", exc_info=True)

    def _license_quota(self) -> Dict[str, Any]:
        try:
            from src.licensing import get_license_manager
            st = get_license_manager().status()
            return {
                "plan": st.plan, "state": st.state,
                "customer": getattr(st, "customer", ""),
                "seats": st.seats, "channels": list(st.channels),
            }
        except Exception:
            return {"plan": "community", "state": "unavailable", "customer": "",
                    "seats": 0, "channels": []}

    def _check_billing(self, *, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        # 节流：距上次计费巡检不足 billing_interval 则跳过（首次 last=0 必跑）。
        if self._last_billing_check_ts and (ts - self._last_billing_check_ts) < self._billing_interval:
            return
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_usage_stats"):
            return
        self._last_billing_check_ts = ts
        from src.utils.ops_overview import billing_anomalies

        statement = self._compute_statement()
        if statement is None:
            return
        anomalies = billing_anomalies(statement)
        sig = "|".join(sorted(a.get("code", "") for a in anomalies))

        if anomalies:
            if sig != self._last_billing_sig:
                self._emit_billing_alert(anomalies)
                self.total_billing_alerts += 1
            self._last_billing_sig = sig
        else:
            if self._last_billing_sig:
                # 本进程内 alert→green 的正常恢复：resolve + 外发恢复通知
                self._emit_billing_recovery()
            else:
                # 进程刚起且当前无异常：静默 reconcile 掉上一进程遗留的 open 计费事件。
                # （修复某计费异常后重启时，in-memory 签名为空，否则旧 red 事件会一直挂着，
                #  既不在本进程内 emit 恢复，也无人关闭。）静默关闭，不外发恢复通知。
                inbox = self._inbox()
                if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                    try:
                        n = inbox.resolve_open_incidents(kind="billing") or 0
                        if n:
                            logger.info(
                                "HealthWatchdog 启动 reconcile：关闭遗留计费事件 %d 条", n)
                    except Exception:
                        logger.debug("计费事件 reconcile 失败（已忽略）", exc_info=True)
            self._last_billing_sig = None

    def _compute_statement(self) -> Optional[Dict[str, Any]]:
        """算当月对账单（_check_billing 与周报共用）。失败/无 store 返回 None。"""
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_usage_stats"):
            return None
        from src.utils.billing import compute_statement
        config = getattr(self._config_manager, "config", None) or {}
        lt = time.localtime()
        try:
            return compute_statement(
                inbox, lt.tm_year, lt.tm_mon,
                license_status=self._license_quota(), pricing=config.get("pricing"),
            )
        except Exception:
            logger.debug("对账单计算失败（已忽略）", exc_info=True)
            return None

    def _emit_billing_alert(self, anomalies: List[Dict[str, Any]]) -> None:
        # E3↔E2：计费异常也进 ops_incidents（kind=billing），与健康事件统一可 ack/指派/恢复。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                has_fail = any(a.get("severity") == "fail" for a in anomalies)
                problems = [
                    {"id": a.get("code"), "name": "计费", "status": a.get("severity"),
                     "detail": a.get("message")}
                    for a in anomalies
                ]
                inbox.open_or_update_incident(
                    kind="billing",
                    signature="|".join(sorted(a.get("code", "") for a in anomalies)),
                    light="red" if has_fail else "yellow",
                    summary={"anomalies": len(anomalies)},
                    problems=problems,
                )
        except Exception:
            logger.debug("计费事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("billing_alert", {
                "anomalies": anomalies, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出计费异常告警：%d 项", len(anomalies))
        except Exception:
            logger.debug("billing_alert 发布失败（已忽略）", exc_info=True)

    def _emit_billing_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="billing")
        except Exception:
            logger.debug("计费事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("billing_alert", {"anomalies": [], "recovered": True})
            logger.info("HealthWatchdog 发出计费恢复通知")
        except Exception:
            logger.debug("billing recovery 发布失败（已忽略）", exc_info=True)

    def _check_draft_quality(self) -> None:
        """统一草稿引擎质量巡检：基于**窗口速率**评估（可触发、可恢复）。

        三项规则（阈值见 ``inbox.auto_draft.quality_alert``）：
          - 记忆命中率过低 → 自动回复可能「记不住」客户信息
          - p95 生成延迟过高 → 延迟预算被突破
          - 低风险快路占比过高 → 风险分类可能过宽（敏感消息或未走全栈/审核）

        用**窗口**速率而非累计：累计率一旦退化无法回弹，无法表达「已恢复」；窗口率
        随近 1h 流量实时升降，触发与恢复都灵敏。窗口样本不足时静默（不改状态）。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        qa = (((cfg.get("inbox") or {}).get("auto_draft") or {}).get("quality_alert")
              or {}) if isinstance(cfg, dict) else {}
        if not qa.get("enabled", True):
            return
        try:
            from src.monitoring.metrics_store import get_metrics_store
            snap = get_metrics_store().get_inbox_draft_metrics()
        except Exception:
            return

        window = snap.get("window") or {}
        win_gen = int(window.get("generated") or 0)
        min_samples = int(qa.get("min_samples", 30))
        if win_gen < max(1, min_samples):
            return  # 样本不足，静默（不改变既有告警/恢复态）

        def _rate(name: str) -> float:
            return (int(window.get(name) or 0) / win_gen) if win_gen else 0.0

        problems: List[Dict[str, Any]] = []

        # 分级：严重退化升 red（fail），轻微越界为 yellow（warn）。
        mem_min = float(qa.get("memory_hit_min", 0.30))
        mem_severe = float(qa.get("memory_hit_severe", 0.15))
        mem_r = _rate("memory_hit")
        if mem_r < mem_min:
            problems.append({
                "id": "memory_hit_low", "name": "草稿记忆命中率",
                "status": "fail" if mem_r < mem_severe else "warn",
                "detail": (f"近窗口记忆命中率 {mem_r:.0%} < 阈值 {mem_min:.0%}"
                           f"{'（严重失忆）' if mem_r < mem_severe else '（自动回复可能记不住客户信息）'}"),
            })

        latency = snap.get("latency") or {}
        p95_max = int(qa.get("p95_ms_max", 8000))
        p95_severe = int(qa.get("p95_ms_severe", p95_max * 2))
        p95 = int(latency.get("p95_ms") or 0)
        if latency.get("count") and p95 > p95_max:
            problems.append({
                "id": "latency_high", "name": "草稿生成 p95 延迟",
                "status": "fail" if p95 > p95_severe else "warn",
                "detail": f"p95 {p95}ms > 阈值 {p95_max}ms（n={latency.get('count')}）",
            })

        fp_max = float(qa.get("fast_path_ratio_max", 0.98))
        fp_ratio = _rate("fast_path")
        if fp_ratio > fp_max:
            problems.append({
                "id": "risk_classify_loose", "name": "风险分类可能过宽",
                "status": "warn",  # 配置质量信号，非故障 → 恒 yellow
                "detail": (f"近窗口低风险快路占比 {fp_ratio:.0%} > 阈值 {fp_max:.0%}"
                           "（几乎全判低风险，敏感消息可能未走全栈/人工审核）"),
            })

        # 签名带上 status：轻微→严重的升级会改变签名 → 重新发一条（值班能感知升级）。
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
        if problems:
            if sig != self._last_draft_quality_sig:
                self._emit_draft_quality_alert(problems, light)
                self.total_draft_quality_alerts += 1
            self._last_draft_quality_sig = sig
        else:
            if self._last_draft_quality_sig:
                self._emit_draft_quality_recovery()
            self._last_draft_quality_sig = None

    def _emit_draft_quality_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="draft_quality",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("草稿质量事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("draft_quality_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出草稿质量告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("draft_quality_alert 发布失败（已忽略）", exc_info=True)

    def _emit_draft_quality_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="draft_quality")
        except Exception:
            logger.debug("草稿质量事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("draft_quality_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出草稿质量恢复通知")
        except Exception:
            logger.debug("draft_quality recovery 发布失败（已忽略）", exc_info=True)

    def _check_ai_quality(self, *, now: Optional[float] = None) -> None:
        """AI 回复质量退化巡检（F1）：基于 ``ai_safety_summary`` 处置结果口径评估采纳/弃用率
        与高危量环比，退化即落 ``ops_incidents(kind=ai_quality)`` 供值班 ack/指派，恢复自动
        resolve。**默认关**（阈值须按真实分布校准后再开）；样本不足静默；去抖同其余巡检。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        aq = ((((cfg.get("inbox") or {}).get("ai_quality_alert")) or {})
              if isinstance(cfg, dict) else {})
        if not aq.get("enabled", False):
            return
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "ai_safety_summary"):
            return
        now = float(now if now is not None else time.time())
        window = max(1, int(aq.get("window_days", 7) or 7)) * 86400
        try:
            cur = inbox.ai_safety_summary(since_ts=now - window)
            prev = inbox.ai_safety_summary(since_ts=now - 2 * window, until_ts=now - window)
        except Exception:
            return  # 读失败静默，不改变既有告警/恢复态
        from src.utils.ai_quality_alert import evaluate_ai_quality
        res = evaluate_ai_quality(cur, prev, aq)
        problems = res.get("problems") or []
        light = res.get("light") or "green"
        # 签名带 status：warn→fail 升级会改签名 → 重发一条（值班能感知升级）。
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        if problems:
            if sig != self._last_ai_quality_sig:
                self._emit_ai_quality_alert(problems, light)
                self.total_ai_quality_alerts += 1
            self._last_ai_quality_sig = sig
        else:
            if self._last_ai_quality_sig:
                self._emit_ai_quality_recovery()
            self._last_ai_quality_sig = None

    def _emit_ai_quality_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="ai_quality",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("AI 质量事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_quality_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出 AI 质量告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("ai_quality_alert 发布失败（已忽略）", exc_info=True)

    def _emit_ai_quality_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="ai_quality")
        except Exception:
            logger.debug("AI 质量事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ai_quality_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出 AI 质量恢复通知")
        except Exception:
            logger.debug("ai_quality recovery 发布失败（已忽略）", exc_info=True)

    def _check_realtime_voice(self) -> None:
        """实时语音通话退化巡检（B 线）：基于 ``RealtimeVoiceStats`` 评估主机健康/接通率/
        主机不可达，退化即落 ``ops_incidents(kind=realtime_voice)``。**默认关**；功能未启用
        或样本不足静默；去抖同其余巡检。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False):
            return
        alert_cfg = (rtv.get("alert") or {}) if isinstance(rtv, dict) else {}
        if not alert_cfg.get("enabled", False):
            return
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            from src.utils.realtime_voice_alert import evaluate_realtime_voice_alert
            stats = get_realtime_voice_stats().dump()
            res = evaluate_realtime_voice_alert(stats, alert_cfg)
        except Exception:
            return
        problems = res.get("problems") or []
        light = res.get("light") or "green"
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        if problems:
            if sig != self._last_realtime_voice_sig:
                self._emit_realtime_voice_alert(problems, light)
                self.total_realtime_voice_alerts += 1
            self._last_realtime_voice_sig = sig
        else:
            if self._last_realtime_voice_sig:
                self._emit_realtime_voice_recovery()
            else:
                # 进程刚起且当前无异常：静默 reconcile 遗留 open 事件（重启后 stats 归零，
                # 内存签名空，否则旧 red 事件会一直挂着且不会 emit 恢复）。
                inbox = self._inbox()
                if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                    try:
                        n = inbox.resolve_open_incidents(kind="realtime_voice") or 0
                        if n:
                            logger.info(
                                "HealthWatchdog 启动 reconcile：关闭遗留实时语音事件 %d 条", n)
                    except Exception:
                        logger.debug("实时语音事件 reconcile 失败（已忽略）", exc_info=True)
            self._last_realtime_voice_sig = None

    def _sync_realtime_voice_trend(self) -> None:
        """E 线兜底：watchdog tick 把进程 stats 与上次同步快照 diff 写入趋势库（旁路漏记时补）。"""
        cfg = getattr(self._config_manager, "config", None) or {}
        rtv = (cfg.get("realtime_voice") or {}) if isinstance(cfg, dict) else {}
        if not rtv.get("enabled", False) or not rtv.get("trend_log", False):
            return
        try:
            from src.ai.realtime_voice_stats import get_realtime_voice_stats
            from src.ai.realtime_voice_trend_store import sync_realtime_voice_trend_from_stats
            sync_realtime_voice_trend_from_stats(get_realtime_voice_stats().dump())
        except Exception:
            logger.debug("实时语音趋势 sync 失败（已忽略）", exc_info=True)

    def _emit_realtime_voice_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="realtime_voice",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("实时语音事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("realtime_voice_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出实时语音告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("realtime_voice_alert 发布失败（已忽略）", exc_info=True)

    def _emit_realtime_voice_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="realtime_voice")
        except Exception:
            logger.debug("实时语音事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("realtime_voice_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出实时语音恢复通知")
        except Exception:
            logger.debug("realtime_voice recovery 发布失败（已忽略）", exc_info=True)

    def _skill_manager(self):
        return getattr(getattr(self._app, "state", self._app), "skill_manager", None)

    def _check_memory_key_drift(self, *, now: Optional[float] = None) -> None:
        """记忆 key 漂移巡检：裸 key（无 ``platform:`` 前缀）复发即告警。

        一次性迁移（:mod:`src.utils.episodic_key_migration`）清存量后，若某入口又漏传
        platform，记忆会重新落到裸 key、对收件箱引擎不可见 → 静默拉低命中率。本巡检
        让漂移**自我守护**：``bare_keys`` 超阈即发 ``memory_key_drift`` 事件（可恢复）。

        结构性数据（key 集合慢变），故独立稀疏节流（默认 1h）；阈值见
        ``inbox.auto_draft.key_drift_alert``。
        """
        cfg = getattr(self._config_manager, "config", None) or {}
        kd = (((cfg.get("inbox") or {}).get("auto_draft") or {}).get("key_drift_alert")
              or {}) if isinstance(cfg, dict) else {}
        if not kd.get("enabled", True):
            return
        ts = float(now if now is not None else time.time())
        interval = max(self._interval, float(kd.get("interval_sec", 3600)))
        if self._last_drift_check_ts and (ts - self._last_drift_check_ts) < interval:
            return
        sm = self._skill_manager()
        if sm is None or not hasattr(sm, "episodic_key_health"):
            return
        try:
            health = sm.episodic_key_health(sample=5)
        except Exception:
            return
        if not health.get("enabled"):
            return
        self._last_drift_check_ts = ts

        bare = int(health.get("bare_keys") or 0)
        bare_max = int(kd.get("bare_keys_max", 0))
        bare_severe = int(kd.get("bare_keys_severe", 50))
        problems: List[Dict[str, Any]] = []
        if bare > bare_max:
            samples = ", ".join(
                str(s.get("key")) for s in (health.get("bare_samples") or [])[:5]
            )
            problems.append({
                "id": "memory_key_drift", "name": "记忆 key 漂移",
                "status": "fail" if bare >= bare_severe else "warn",
                "detail": (
                    f"检测到 {bare} 个裸 key（无 platform 前缀，含 "
                    f"{int(health.get('bare_facts') or 0)} 条事实）对收件箱引擎不可见 → "
                    f"拉低命中率；样例: {samples}。"
                    "可运行 src.utils.episodic_key_migration 并入 canonical key"
                ),
            })

        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in problems))
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
        if problems:
            if sig != self._last_drift_sig:
                self._emit_memory_key_drift_alert(problems, light)
                self.total_memory_key_drift_alerts += 1
            self._last_drift_sig = sig
        else:
            if self._last_drift_sig:
                self._emit_memory_key_drift_recovery()
            self._last_drift_sig = None

    def _emit_memory_key_drift_alert(
        self, problems: List[Dict[str, Any]], light: str = "yellow",
    ) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="memory_key_drift",
                    signature="|".join(sorted(p["id"] for p in problems)),
                    light=light,
                    summary={"problems": len(problems)},
                    problems=problems,
                )
        except Exception:
            logger.debug("记忆 key 漂移事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("memory_key_drift_alert", {
                "light": light, "problems": problems, "recovered": False,
            })
            logger.warning("HealthWatchdog 发出记忆 key 漂移告警：light=%s %d 项",
                           light, len(problems))
        except Exception:
            logger.debug("memory_key_drift_alert 发布失败（已忽略）", exc_info=True)

    def _emit_memory_key_drift_recovery(self) -> None:
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="memory_key_drift")
        except Exception:
            logger.debug("记忆 key 漂移事件 resolve 失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("memory_key_drift_alert", {
                "problems": [], "recovered": True,
            })
            logger.info("HealthWatchdog 发出记忆 key 漂移恢复通知")
        except Exception:
            logger.debug("memory_key_drift recovery 发布失败（已忽略）", exc_info=True)

    def _maybe_purge_incidents(self, *, now: Optional[float] = None) -> int:
        if self._retention_days <= 0:
            return 0
        ts = float(now if now is not None else time.time())
        if self._last_purge_ts and (ts - self._last_purge_ts) < self._purge_interval:
            return 0
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "purge_resolved_incidents"):
            return 0
        self._last_purge_ts = ts
        cutoff = ts - self._retention_days * 86400.0
        n = inbox.purge_resolved_incidents(cutoff)
        if n:
            logger.info("HealthWatchdog 清理已关闭运维事件 %d 条（保留 %.0f 天）",
                        n, self._retention_days)
        return n

    def _build_weekly_report(self, *, days: int = 7, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """无 request 装配运营周报：事件统计 + 自动化价值 + 计费 + 环比上周。

        ROI 的「经营/首响」段需 request（依赖 _daily_report_rows），watchdog 取不到，
        故周报以「运维 + 自动化 + 计费」为主，business 段从缺（build_ops_report 优雅降级）。
        """
        inbox = self._inbox()
        if inbox is None or not hasattr(inbox, "get_incident_stats"):
            return None
        from src.utils.ops_intel import automation_value, build_ops_report, weekly_compare

        ts = float(now if now is not None else time.time())
        span_sec = days * 86400.0
        since = ts - span_sec
        prev_since = since - span_sec

        config = getattr(self._config_manager, "config", None) or {}
        roi_cfg = ((config.get("workspace") or {}).get("roi") or {})
        sec_per_reply = int(roi_cfg.get("sec_per_reply") or 180)
        cost_per_hour = float(roi_cfg.get("cost_per_hour") or 0)

        def _roi_for(since_ts: float, until_ts: Optional[float]) -> Dict[str, Any]:
            auto_stats = {}
            if hasattr(inbox, "get_automation_roi_stats"):
                try:
                    auto_stats = (inbox.get_automation_roi_stats(since_ts, until_ts=until_ts)
                                  if until_ts is not None
                                  else inbox.get_automation_roi_stats(since_ts))
                except TypeError:
                    auto_stats = inbox.get_automation_roi_stats(since_ts)
                except Exception:
                    logger.debug("自动化统计失败（已忽略）", exc_info=True)
            return {"automation": automation_value(
                auto_stats, sec_per_reply=sec_per_reply, cost_per_hour=cost_per_hour)}

        cur_inc = inbox.get_incident_stats(since)
        prev_inc = inbox.get_incident_stats(prev_since, until_ts=since)
        cur_roi = _roi_for(since, None)
        prev_roi = _roi_for(prev_since, since)
        billing = self._compute_statement()

        # weekly_compare 只读 incidents.total 与 automation 几个键，故用轻量 view 即可，
        # 避免为算环比额外整套 build_ops_report（构建从 3 次降到 1 次）。
        compare = weekly_compare(
            {"incidents": {"total": cur_inc.get("total")}, "automation": cur_roi["automation"]},
            {"incidents": {"total": prev_inc.get("total")}, "automation": prev_roi["automation"]},
        )
        return build_ops_report(days=days, incident_stats=cur_inc, roi=cur_roi,
                                billing=billing, compare=compare)

    def _maybe_weekly_report(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self._weekly_enabled:
            return None
        ts = float(now if now is not None else time.time())
        if self._last_weekly_ts and (ts - self._last_weekly_ts) < self._weekly_interval:
            return None
        report = self._build_weekly_report(now=ts)
        if report is None:
            return None
        self._last_weekly_ts = ts
        self.total_weekly_reports += 1
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("ops_report", report)
            logger.info("HealthWatchdog 发出运营周报（事件 %d 起）",
                        (report.get("incidents") or {}).get("total", 0))
        except Exception:
            logger.debug("ops_report 发布失败（已忽略）", exc_info=True)
        return report

    def _inbox(self):
        return getattr(getattr(self._app, "state", self._app), "inbox_store", None)

    def _emit_alert(self, health: Dict[str, Any]) -> None:
        problems = problems_of(health)
        # E2：先落表为运维事件（按健康签名去重 open/update），可追踪到处理人。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "open_or_update_incident"):
                inbox.open_or_update_incident(
                    kind="health",
                    signature=health_signature(health),
                    light=str(health.get("light") or ""),
                    summary=health.get("summary") or {},
                    problems=problems,
                )
        except Exception:
            logger.debug("运维事件落表失败（已忽略）", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("health_alert", {
                "light": health.get("light"),
                "problems": problems,
                "summary": health.get("summary"),
                "recovered": False,
            })
            logger.warning("HealthWatchdog 发出健康告警：light=%s 异常 %d 项",
                           health.get("light"), len(problems))
        except Exception:
            logger.debug("health_alert 发布失败（已忽略）", exc_info=True)

    def _emit_recovery(self, health: Dict[str, Any]) -> None:
        # E2：健康恢复时把未关闭的「健康」事件标 resolved（不动计费事件）。
        try:
            inbox = self._inbox()
            if inbox is not None and hasattr(inbox, "resolve_open_incidents"):
                inbox.resolve_open_incidents(kind="health")
        except Exception:
            logger.debug("运维事件 resolve 失败（已忽略）", exc_info=True)
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
            "total_billing_alerts": self.total_billing_alerts,
            "total_draft_quality_alerts": self.total_draft_quality_alerts,
            "total_ai_quality_alerts": self.total_ai_quality_alerts,
            "total_realtime_voice_alerts": self.total_realtime_voice_alerts,
            "total_memory_key_drift_alerts": self.total_memory_key_drift_alerts,
            "total_weekly_reports": self.total_weekly_reports,
            "last_check_ts": self.last_check_ts,
            "last_light": self.last_light,
        }
