"""SLAWatcher — K1 草稿 SLA 红线预警推送 + K2 无人应答自动再分配。

K1 — 草稿 SLA 预警（SSE push）：
  - 每 60s 检查一次 L3/L4 pending 草稿，对超过 SLA 阈值的草稿发布 `draft_sla_breach` 事件
  - 边沿触发（同一草稿只告警一次；超时解除后可再次告警）
  - 去重集每 1 小时清空一次

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
  sla_hours: 4          # L3/L4 草稿超过此时长未处置 → 触发预警
  tick_sec: 60          # 后台检查间隔（秒）
  absent_sec: 300       # 坐席心跳消失超过此时长 → 视为断线，触发再分配
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
_DEDUP_CLEAR_SEC: float = 3600.0    # 去重集每小时清空


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

        self._running = False
        self._stop_evt = asyncio.Event()

        # K1 去重：已发出告警的 draft_id 集合
        self._alerted_draft_ids: Set[str] = set()
        self._last_dedup_clear_ts: float = time.time()

        # K2 去重：已自动再分配的 draft_id 集合（避免重复分配）
        self._reassigned_draft_ids: Set[str] = set()

        # 指标
        self.total_breach_events: int = 0
        self.total_reassigned: int = 0
        self.last_tick_ts: float = 0.0

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
        # 去重集定期清空（避免长期运行内存泄漏）
        if time.time() - self._last_dedup_clear_ts > _DEDUP_CLEAR_SEC:
            self._alerted_draft_ids.clear()
            self._reassigned_draft_ids.clear()
            self._last_dedup_clear_ts = time.time()
            logger.debug("SLAWatcher 去重集已清空")

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._check_sla_breach)
        except Exception:
            logger.debug("K1 SLA breach 检查失败（已忽略）", exc_info=True)

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._check_reassign)
        except Exception:
            logger.debug("K2 自动再分配检查失败（已忽略）", exc_info=True)

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
        still_breaching: Set[str] = set()

        for d in drafts:
            if d.get("autopilot_level") not in ("L3", "L4"):
                continue
            ca = float(d.get("created_at") or d.get("created_ts") or 0)
            if ca <= 0 or ca >= threshold_ts:
                continue  # 未超时

            draft_id = str(d.get("draft_id") or "")
            if not draft_id:
                continue
            still_breaching.add(draft_id)

            if draft_id in self._alerted_draft_ids:
                continue  # 已告警，跳过

            self._alerted_draft_ids.add(draft_id)
            self.total_breach_events += 1

            wait_min = round((time.time() - ca) / 60)
            bus.publish("draft_sla_breach", {
                "draft_id": draft_id,
                "conversation_id": str(d.get("conversation_id") or ""),
                "platform": str(d.get("platform") or ""),
                "autopilot_level": str(d.get("autopilot_level") or ""),
                "risk_level": str(d.get("risk_level") or ""),
                "wait_min": wait_min,
                "sla_hours": self._sla_hours,
                "peer_text_preview": str(d.get("peer_text") or "")[:80],
            })
            logger.info(
                "K1 draft_sla_breach: %s (level=%s wait=%dm)",
                draft_id, d.get("autopilot_level"), wait_min,
            )

        # 已恢复（不再超时）的从告警集移除，允许再次告警
        self._alerted_draft_ids.intersection_update(still_breaching)

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
            "total_breach_events": self.total_breach_events,
            "total_reassigned": self.total_reassigned,
            "alerted_count": len(self._alerted_draft_ids),
            "last_tick_ts": self.last_tick_ts,
        }
