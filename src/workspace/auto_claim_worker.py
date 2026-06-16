"""AutoClaimWorker — auto_assign 守护版「执行端」（P3）。

承接 ``AssignmentService.plan_auto_claims``（纯决策、已单测）：周期性把「等待回复且
未认领」的会话**自动认领**给（语言匹配的）活跃坐席，把 auto_assign 从「只建议」推进到
「真派单」。与 ``match_language`` 协同——plan 内部已按会话语言优先匹配会该语言的坐席。

安全契约（勿误改）：
  - 默认关（``workspace.auto_assign.auto_claim.enabled=false``）；**每 tick 重读配置**，
    支持运行时热开关，无需重启。
  - ``force=False`` 认领：绝不抢占已认领会话（plan 已排除，这里双保险）。
  - 仅认领「末条入站＝等待回复」的会话；已在被回复/出向收尾的不动。
  - 仅派给活跃窗口内坐席（由 plan 的 ``active_within_sec`` 守门，避免锁给挂机坐席）。
  - 每 tick 限额 ``max_per_tick``，避免突发把大量会话一次性灌给少数坐席。
  - 认领即发 ``conversation_claim``(action=auto_claimed) SSE，坐席侧实时可见、可手动释放。
  - 与 AutosendWorker / SLAWatcher 同构（run/stop/status_snapshot），便于监控。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TICK_SEC: float = 30.0
_DEFAULT_MAX_PER_TICK: int = 20
_DEFAULT_TTL_SEC: float = 900.0
_PRESENCE_WINDOW_SEC: float = 86400.0  # 取宽口径 presence，活跃过滤交给 plan_auto_claims


class AutoClaimWorker:
    """auto_assign 后台自动认领任务。"""

    def __init__(
        self,
        *,
        inbox_store: Any,
        config_manager: Any = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        self._store = inbox_store
        self._config_manager = config_manager
        self._tick_sec: float = float(cfg.get("tick_sec", _DEFAULT_TICK_SEC))
        self._max_per_tick: int = max(1, int(cfg.get("max_per_tick", _DEFAULT_MAX_PER_TICK)))

        self._running = False
        self._stop_evt = asyncio.Event()

        self.total_claimed: int = 0
        self.total_lang_matched: int = 0
        self.last_tick_ts: float = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info("AutoClaimWorker 已启动（tick=%.0fs max_per_tick=%d）",
                    self._tick_sec, self._max_per_tick)
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("AutoClaimWorker tick 出错（已忽略）")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_evt.wait()), timeout=self._tick_sec)
                break
            except asyncio.TimeoutError:
                pass
        self._running = False
        logger.info("AutoClaimWorker 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 配置（每 tick 重读，支持热开关）──────────────────────────────────

    def _service(self):
        from src.workspace.assignment import AssignmentService
        full = (getattr(self._config_manager, "config", None) or {}) if self._config_manager else {}
        return AssignmentService.from_config(full if isinstance(full, dict) else {})

    def _claim_ttl_sec(self, svc) -> float:
        ac = svc.cfg.get("auto_claim") or {}
        ttl = float(ac.get("ttl_sec") or 0)
        if ttl > 0:
            return ttl
        try:
            ws = (getattr(self._config_manager, "config", None) or {}).get("workspace") or {}
            g = float(ws.get("claim_ttl_sec") or 0)
            if g > 0:
                return g
        except Exception:
            pass
        return _DEFAULT_TTL_SEC

    # ── 核心 tick ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        self.last_tick_ts = time.time()
        if self._store is None:
            return
        svc = self._service()
        if not svc.auto_claim_enabled:
            return  # 热开关：未开启则本 tick 空转
        await asyncio.get_event_loop().run_in_executor(None, self._do_claims, svc)

    def _do_claims(self, svc) -> None:
        # 1. 候选：等待回复（末条入站）的会话；plan 内部再排除已认领并按语言/负载选坐席
        try:
            convs = self._store.list_conversations(limit=500)
        except Exception:
            logger.debug("AutoClaimWorker 读取会话失败（已忽略）", exc_info=True)
            return
        if not convs:
            return
        cids = [str(c.get("conversation_id") or "") for c in convs if c.get("conversation_id")]
        try:
            dirs = self._store.last_message_dirs(cids)
        except Exception:
            dirs = {}
        waiting = [
            c for c in convs
            if dirs.get(str(c.get("conversation_id") or ""), {}).get("direction") == "in"
        ]
        if not waiting:
            return

        try:
            presence = self._store.list_agent_presence(active_within_sec=_PRESENCE_WINDOW_SEC)
            claims = self._store.list_conversation_claims()
        except Exception:
            logger.debug("AutoClaimWorker 读取 presence/claims 失败（已忽略）", exc_info=True)
            return

        plan = svc.plan_auto_claims(chats=waiting, presence=presence, claims=claims)
        if not plan:
            return
        ttl = self._claim_ttl_sec(svc)
        lang_by_cid = {
            str(c.get("conversation_id") or ""): str(c.get("language") or "")
            for c in waiting
        }
        bus = None
        try:
            from src.integrations.shared.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            bus = None

        done = 0
        for item in plan:
            if done >= self._max_per_tick:
                break
            cid = str(item.get("conversation_id") or "")
            aid = str(item.get("agent_id") or "")
            if not cid or not aid:
                continue
            try:
                res = self._store.set_conversation_claim(
                    cid, aid, agent_name=str(item.get("agent_name") or ""),
                    ttl_sec=ttl, force=False,
                )
            except Exception:
                logger.debug("AutoClaimWorker set_claim 失败（已忽略）", exc_info=True)
                continue
            if not res.get("ok"):
                continue  # 期间被他人认领 → 不抢占
            done += 1
            self.total_claimed += 1
            matched = bool(item.get("matched_language"))
            if matched:
                self.total_lang_matched += 1
            try:
                self._store.record_auto_claim(
                    matched=matched, lang=lang_by_cid.get(cid, ""))
            except Exception:
                logger.debug("AutoClaimWorker record_auto_claim 失败（已忽略）",
                             exc_info=True)
            if bus is not None:
                try:
                    bus.publish("conversation_claim", {
                        "action": "auto_claimed",
                        "conversation_id": cid,
                        "claim": res.get("claim"),
                        "matched_language": matched,
                        "conv_language": lang_by_cid.get(cid, ""),
                    })
                except Exception:
                    logger.debug("AutoClaimWorker 事件发布失败（已忽略）", exc_info=True)
            logger.info("AutoClaimWorker auto_claimed: %s → %s (lang_match=%s)",
                        cid, aid, matched)

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "tick_sec": self._tick_sec,
            "max_per_tick": self._max_per_tick,
            "total_claimed": self.total_claimed,
            "total_lang_matched": self.total_lang_matched,
            "last_tick_ts": self.last_tick_ts,
        }
