"""SLAWatcher — K1 草稿 SLA 红线预警推送 + K2 无人应答自动再分配。

K1 — 草稿 SLA 预警（SSE push）：
  - 每 60s 检查一次 L3/L4 pending 草稿，对超过 SLA 阈值的草稿发布 `draft_sla_breach` 事件
  - 首次越线立即告警；此后按**指数退避**再告警（默认 1h→2h→4h…，封顶 24h），
    而非旧版「去重集每小时清空 → 全部积压草稿每小时重发一遍」造成的铃铛风暴
  - 陈旧封顶（stale_hours>0，默认关）：草稿老到超过该阈值后，首告警一次即静默——
    已知积压不再反复打扰（仍留在草稿队列与审计中，铃铛只是提醒器而非事实源）
  - 超时解除后（草稿被处置）从告警态移除，允许后续重新越线时再次告警
  - 积压汇总（backlog_summary=true，默认关）：每 tick 统计当前越线的 L3/L4 总数，
    在数量变化或超过 backlog_summary_interval_sec 时发布**一条** `draft_backlog_summary`
    事件——前端按「type 单键」合并成一条滚动摘要（「积压 N 条」），使一波并发越线
    不再在铃铛里堆成 N 条并列。逐草稿 `draft_sla_breach` 仍照常发（新鲜越线仍需点名）。

K2 — 无人应答自动再分配：
  - 检查 agent_presence：坐席断线（last_seen_at > absent_sec）且仍有 L3+ pending 草稿的
  - 自动将该草稿的认领（conversation_claim）转移给负载最低的在线主管
  - 写 draft_audit_log（action=auto_reassigned）
  - 发布 `draft_reassigned` SSE 事件

优化亮点（相比"在 SSE heartbeat 内内联计算"方案）：
  - 单一后台 tick，无论多少 SSE 客户端连接，检查只运行一次
  - 事件经 EventBus fan-out → 所有客户端同时收到，延迟低至毫秒级
  - 与 AutosendWorker 同构（stop/run/status_snapshot），便于监控

配置（config.yaml::inbox.sla_watcher）：
  enabled: true
  sla_hours: 4              # L3/L4 草稿超过此时长未处置 → 触发预警
  tick_sec: 60              # 后台检查间隔（秒）
  absent_sec: 300           # 坐席心跳消失超过此时长 → 视为断线，触发再分配
  realert_base_sec: 3600    # 首次再告警间隔（秒）；设 realert_backoff=1 即退回旧的定频
  realert_backoff: 2.0      # 再告警间隔的指数倍率（每告警一次翻倍，直到封顶）
  realert_max_sec: 86400    # 再告警间隔封顶（默认每天最多一次）
  stale_hours: 0            # >0 时：草稿超此年龄首告警后静默（默认 0=关，不静默）
  auto_expire_hours: 0      # >0 时：把搁置超此年龄的 pending 草稿自动作废（默认 0=关）
  auto_expire_levels: []    # 自动作废仅限这些等级（如 ['L3','L4']）；空=全部等级
  backlog_summary: false    # true 时：发布聚合的 draft_backlog_summary 事件（默认关=旧行为）
  backlog_summary_min: 1    # 越线草稿数达此值才发汇总（默认 1）
  backlog_summary_interval_sec: 3600  # 数量不变时的最小重发间隔（默认每小时一次）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DEFAULT_SLA_HOURS: float = 4.0
_DEFAULT_TICK_SEC: float = 60.0
_DEFAULT_ABSENT_SEC: float = 300.0  # 5 分钟
_DEDUP_CLEAR_SEC: float = 3600.0    # 再分配去重集每小时清空（仅 K2 用，防内存泄漏）
_DEFAULT_REALERT_BASE_SEC: float = 3600.0   # 首次再告警间隔
_DEFAULT_REALERT_BACKOFF: float = 2.0       # 指数退避倍率
_DEFAULT_REALERT_MAX_SEC: float = 86400.0   # 再告警间隔封顶（每日一次）


class SLAWatcher:
    """K1+K2 后台监控任务。

    Usage::

        watcher = SLAWatcher(draft_service=svc, inbox_store=store, config=cfg)
        asyncio.ensure_future(watcher.run())
        # 关闭：
        watcher.stop()
    """

    def __init__(
        self,
        *,
        draft_service: Any,
        inbox_store: Any,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        self._svc = draft_service
        self._store = inbox_store
        self._sla_hours: float = float(cfg.get("sla_hours", _DEFAULT_SLA_HOURS))
        self._tick_sec: float = float(cfg.get("tick_sec", _DEFAULT_TICK_SEC))
        self._absent_sec: float = float(cfg.get("absent_sec", _DEFAULT_ABSENT_SEC))

        # K1 再告警节流（指数退避 + 陈旧封顶）
        self._realert_base_sec: float = float(
            cfg.get("realert_base_sec", _DEFAULT_REALERT_BASE_SEC))
        self._realert_backoff: float = max(
            1.0, float(cfg.get("realert_backoff", _DEFAULT_REALERT_BACKOFF)))
        self._realert_max_sec: float = float(
            cfg.get("realert_max_sec", _DEFAULT_REALERT_MAX_SEC))
        self._stale_hours: float = float(cfg.get("stale_hours", 0.0))

        # 治理化开关：pending 草稿自动作废（默认关）
        self._auto_expire_hours: float = float(cfg.get("auto_expire_hours", 0.0))
        self._auto_expire_levels: List[str] = [
            str(x) for x in (cfg.get("auto_expire_levels") or []) if str(x)
        ]

        # 积压汇总（默认关）：把逐草稿越线聚合成一条 draft_backlog_summary
        self._backlog_summary: bool = bool(cfg.get("backlog_summary", False))
        self._backlog_summary_min: int = int(cfg.get("backlog_summary_min", 1))
        self._backlog_summary_interval_sec: float = float(
            cfg.get("backlog_summary_interval_sec", 3600.0))
        self._last_summary_count: int = -1
        self._last_summary_ts: float = 0.0

        self._running = False
        self._stop_evt = asyncio.Event()

        # K1 告警态：draft_id → {count, next_ts, quiesced}
        #   count      已告警次数（用于指数退避）
        #   next_ts    下次允许告警的时间戳（now>=next_ts 才再告警）
        #   quiesced   陈旧封顶后置 True，永不再告警（直到草稿被处置移出）
        self._alert_state: Dict[str, Dict[str, Any]] = {}

        # K2 去重：已自动再分配的 draft_id 集合（避免重复分配）
        self._reassigned_draft_ids: Set[str] = set()
        self._last_dedup_clear_ts: float = time.time()

        # 指标
        self.total_breach_events: int = 0
        self.total_reassigned: int = 0
        self.total_expired: int = 0
        self.quiesced_count: int = 0
        self.total_summary_events: int = 0
        self.last_tick_ts: float = 0.0

    @property
    def _alerted_draft_ids(self) -> Set[str]:
        """向后兼容：曾是 Set[str]，现由 _alert_state 的键派生（供快照/测试沿用）。"""
        return set(self._alert_state.keys())

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info(
            "SLAWatcher 已启动（sla=%.0fh tick=%.0fs absent=%.0fs）",
            self._sla_hours, self._tick_sec, self._absent_sec,
        )
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("SLAWatcher tick 出错（已忽略）")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_evt.wait()),
                    timeout=self._tick_sec,
                )
                break  # stop 被触发
            except asyncio.TimeoutError:
                pass  # 正常 tick 间隔
        self._running = False
        logger.info("SLAWatcher 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 核心 tick ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        self.last_tick_ts = time.time()
        # K2 再分配去重集定期清空（避免长期运行内存泄漏）；
        # K1 告警态改用逐草稿指数退避 + 处置即移除，无需整集清空（那正是旧铃铛风暴之源）。
        if time.time() - self._last_dedup_clear_ts > _DEDUP_CLEAR_SEC:
            self._reassigned_draft_ids.clear()
            self._last_dedup_clear_ts = time.time()
            logger.debug("SLAWatcher 再分配去重集已清空")

        # 治理化开关：先作废搁置过久的 pending 草稿（默认关），再跑越线检查
        if self._auto_expire_hours > 0:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._expire_stale)
            except Exception:
                logger.debug("pending 草稿自动作废失败（已忽略）", exc_info=True)

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._check_sla_breach)
        except Exception:
            logger.debug("K1 SLA breach 检查失败（已忽略）", exc_info=True)

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._check_reassign)
        except Exception:
            logger.debug("K2 自动再分配检查失败（已忽略）", exc_info=True)

    def _expire_stale(self) -> None:
        """治理化开关：把搁置超过 auto_expire_hours 的 pending 草稿自动作废（审计留痕）。"""
        try:
            victims = self._store.expire_stale_pending_drafts(
                max_age_hours=self._auto_expire_hours,
                levels=self._auto_expire_levels or None,
                agent_id="system",
            )
        except Exception:
            logger.debug("expire_stale_pending_drafts 调用失败（已忽略）", exc_info=True)
            return
        if victims:
            self.total_expired += len(victims)
            # 作废的草稿已不再 pending，把其告警态一并清掉
            for v in victims:
                self._alert_state.pop(str(v.get("draft_id") or ""), None)

    # ── K1：SLA 红线检测 + 事件发布 ──────────────────────────────────────

    def _check_sla_breach(self) -> None:
        """检查 L3/L4 pending 草稿，对超 SLA 阈值的发布 draft_sla_breach 事件。"""
        from src.integrations.shared.event_bus import get_event_bus

        threshold_ts = time.time() - self._sla_hours * 3600
        try:
            drafts = self._svc.list_drafts(status="pending", limit=500)
        except Exception:
            return

        bus = get_event_bus()
        now = time.time()
        still_breaching: Set[str] = set()
        by_level: Dict[str, int] = {"L3": 0, "L4": 0}
        stale_cutoff_ts = (now - self._stale_hours * 3600) if self._stale_hours > 0 else None

        for d in drafts:
            level = d.get("autopilot_level")
            if level not in ("L3", "L4"):
                continue
            ca = float(d.get("created_at") or d.get("created_ts") or 0)
            if ca <= 0 or ca >= threshold_ts:
                continue  # 未超时

            draft_id = str(d.get("draft_id") or "")
            if not draft_id:
                continue
            still_breaching.add(draft_id)
            by_level[level] = by_level.get(level, 0) + 1

            st = self._alert_state.get(draft_id)
            if st is None:
                st = {"count": 0, "next_ts": 0.0, "quiesced": False}
                self._alert_state[draft_id] = st

            if st["quiesced"] or now < st["next_ts"]:
                continue  # 已静默 / 尚在退避窗口内，跳过

            # 越线且到点 → 发一次告警，并按指数退避安排下次
            st["count"] += 1
            self.total_breach_events += 1
            gap = min(
                self._realert_base_sec * (self._realert_backoff ** (st["count"] - 1)),
                self._realert_max_sec,
            )
            st["next_ts"] = now + gap

            # 陈旧封顶：草稿老到超过 stale_hours，首告警后即静默（不再反复打扰）
            if stale_cutoff_ts is not None and ca < stale_cutoff_ts:
                st["quiesced"] = True
                self.quiesced_count += 1

            wait_min = round((now - ca) / 60)
            bus.publish("draft_sla_breach", {
                "draft_id": draft_id,
                "conversation_id": str(d.get("conversation_id") or ""),
                "platform": str(d.get("platform") or ""),
                "autopilot_level": str(d.get("autopilot_level") or ""),
                "risk_level": str(d.get("risk_level") or ""),
                "wait_min": wait_min,
                "sla_hours": self._sla_hours,
                "alert_count": st["count"],
                "quiesced": st["quiesced"],
                "peer_text_preview": str(d.get("peer_text") or "")[:80],
            })
            logger.info(
                "K1 draft_sla_breach: %s (level=%s wait=%dm n=%d%s)",
                draft_id, d.get("autopilot_level"), wait_min, st["count"],
                " quiesced" if st["quiesced"] else "",
            )

        # 已恢复（不再超时）的从告警态移除，允许后续重新越线时再次告警
        for gone in [k for k in self._alert_state if k not in still_breaching]:
            self._alert_state.pop(gone, None)

        # 积压汇总（默认关）：把逐草稿越线聚合成一条滚动摘要
        self._maybe_emit_backlog_summary(bus, len(still_breaching), by_level, now)

    def _maybe_emit_backlog_summary(
        self, bus: Any, count: int, by_level: Dict[str, int], now: float,
    ) -> None:
        """发布聚合的 draft_backlog_summary（数量变化或超重发间隔时才发一条）。"""
        if not self._backlog_summary:
            return
        if count < self._backlog_summary_min:
            # 积压回落到阈值下：复位计数，使下次越线能立即再发一条
            self._last_summary_count = 0
            return
        changed = count != self._last_summary_count
        due = (now - self._last_summary_ts) >= self._backlog_summary_interval_sec
        if not (changed or due):
            return
        self._last_summary_count = count
        self._last_summary_ts = now
        self.total_summary_events += 1
        bus.publish("draft_backlog_summary", {
            "count": count,
            "l3": by_level.get("L3", 0),
            "l4": by_level.get("L4", 0),
            "sla_hours": self._sla_hours,
        })
        logger.info(
            "K1 draft_backlog_summary: %d 条积压 (L3=%d L4=%d)",
            count, by_level.get("L3", 0), by_level.get("L4", 0),
        )

    # ── K2：无人应答自动再分配 ────────────────────────────────────────────

    def _check_reassign(self) -> None:
        """K2：检测坐席断线且草稿无人处置 → 自动再分配给在线主管。"""
        from src.integrations.shared.event_bus import get_event_bus

        now = time.time()
        absent_cutoff = now - self._absent_sec

        # 1. 拿全量 presence，找"断线"坐席（last_seen_at < absent_cutoff）
        try:
            all_presence = self._store.list_agent_presence(active_within_sec=86400)
        except Exception:
            return

        offline_agents: List[str] = [
            str(p["agent_id"])
            for p in all_presence
            if float(p.get("last_seen_at") or 0) < absent_cutoff
        ]
        if not offline_agents:
            return

        # 2. 在线主管（last_seen_at >= absent_cutoff & status in online/busy）
        online_presence = [
            p for p in all_presence
            if float(p.get("last_seen_at") or 0) >= absent_cutoff
            and p.get("status") in ("online", "busy")
        ]
        if not online_presence:
            return  # 无可用主管，跳过

        # 3. 选负载最低的在线主管（按 pending 草稿草稿关联 conv_claim 数量）
        def _load(aid: str) -> int:
            try:
                return len(self._store.list_claims_by_agent(str(aid)))
            except Exception:
                return 0

        best_sup = min(online_presence, key=lambda p: _load(str(p["agent_id"])))
        sup_id = str(best_sup.get("agent_id") or "")
        sup_name = str(best_sup.get("display_name") or sup_id)

        # 4. 找断线坐席名下尚未再分配的 L3+ pending 草稿
        try:
            pending_drafts = self._svc.list_drafts(status="pending", limit=500)
        except Exception:
            return

        bus = get_event_bus()

        for d in pending_drafts:
            if d.get("autopilot_level") not in ("L3", "L4"):
                continue
            draft_id = str(d.get("draft_id") or "")
            conv_id = str(d.get("conversation_id") or "")
            if not draft_id or draft_id in self._reassigned_draft_ids:
                continue

            # 检查该 conversation 是否被断线坐席 claimed
            try:
                claim = self._store.get_conversation_claim(conv_id)
            except Exception:
                claim = None

            if claim is None:
                continue
            claimed_by = str(claim.get("agent_id") or "")
            if claimed_by not in offline_agents:
                continue

            # 5. 执行再分配：强制更新 claim → 在线主管
            try:
                self._store.set_conversation_claim(
                    conv_id,
                    sup_id,
                    agent_name=sup_name,
                    ttl_sec=7200,
                    force=True,
                )
            except Exception:
                logger.debug("K2 claim 更新失败（已忽略）", exc_info=True)
                continue

            # 6. 写审计日志
            try:
                self._store.record_draft_audit(
                    draft_id,
                    autopilot_level=str(d.get("autopilot_level") or ""),
                    action="auto_reassigned",
                    agent_id="system",
                    risk_level=str(d.get("risk_level") or ""),
                    conversation_id=conv_id,
                    reason=f"坐席 {claimed_by} 断线 > {self._absent_sec:.0f}s，自动转给 {sup_id}",
                )
            except Exception:
                logger.debug("K2 审计日志写入失败（已忽略）", exc_info=True)

            self._reassigned_draft_ids.add(draft_id)
            self.total_reassigned += 1

            bus.publish("draft_reassigned", {
                "draft_id": draft_id,
                "conversation_id": conv_id,
                "from_agent": claimed_by,
                "to_agent": sup_id,
                "to_agent_name": sup_name,
                "autopilot_level": str(d.get("autopilot_level") or ""),
                "reason": "agent_offline",
            })
            logger.info(
                "K2 draft_reassigned: %s (%s → %s)",
                draft_id, claimed_by, sup_id,
            )

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "sla_hours": self._sla_hours,
            "tick_sec": self._tick_sec,
            "absent_sec": self._absent_sec,
            "realert_base_sec": self._realert_base_sec,
            "realert_backoff": self._realert_backoff,
            "realert_max_sec": self._realert_max_sec,
            "stale_hours": self._stale_hours,
            "auto_expire_hours": self._auto_expire_hours,
            "backlog_summary": self._backlog_summary,
            "total_breach_events": self.total_breach_events,
            "total_reassigned": self.total_reassigned,
            "total_expired": self.total_expired,
            "quiesced_count": self.quiesced_count,
            "total_summary_events": self.total_summary_events,
            "alerted_count": len(self._alert_state),
            "last_tick_ts": self.last_tick_ts,
        }
