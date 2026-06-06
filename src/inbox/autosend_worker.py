"""AutosendWorker — L2 草稿自动发送后台任务（Phase A + C3 事件驱动升级）。

设计要点：
  C3 升级（事件驱动）：
  - 新 L2 草稿落库时，InboxStore 通过回调立即唤醒 worker（notify_new_l2）
  - 替代纯定时轮询：延迟从最多 60s 降至毫秒级，同时保留定时兜底
  - asyncio.Event + loop.call_soon_threadsafe 确保线程安全

  Phase A 保留：
  - 自适应间隔：有发送时使用 min_interval，静默时指数扩张到 max_interval
  - 熔断器：连续 circuit_threshold 次失败后进入 open 状态，等待 cooldown_sec 后重试
  - 每草稿隔离：单条发送失败不影响同批次其他草稿
  - 指标：total_sent / total_errors / last_run_ts / last_sent 供 /api/drafts/autosend-status 暴露

配置（config.yaml::inbox.l2_autosend）：
  enabled: true
  min_interval_sec: 60      # 有活动时的最短轮询间隔（也是定时兜底上限的起点）
  max_interval_sec: 600     # 静默时的最长兜底间隔
  circuit_threshold: 5      # 触发熔断的连续错误次数
  cooldown_sec: 300         # 熔断冷却时间
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AutosendWorker:
    """L2 草稿定时自动发后台任务。

    Usage::

        worker = AutosendWorker(draft_service=svc, config=cfg)
        task = asyncio.create_task(worker.run())
        # 关闭时：
        worker.stop()
        await task
    """

    def __init__(
        self,
        *,
        draft_service: Any,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        self._svc = draft_service
        self._enabled: bool = bool(cfg.get("enabled", True))
        self._min_interval: float = float(cfg.get("min_interval_sec", 60))
        self._max_interval: float = float(cfg.get("max_interval_sec", 600))
        self._circuit_threshold: int = int(cfg.get("circuit_threshold", 5))
        self._cooldown_sec: float = float(cfg.get("cooldown_sec", 300))

        # 运行时状态
        self._running = False
        self._current_interval = self._min_interval

        # C3：事件驱动——asyncio.Event（run() 内初始化，保证在正确的 loop 上）
        self._l2_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 熔断器
        self._consecutive_errors = 0
        self._circuit_open = False
        self._circuit_open_ts: float = 0.0

        # H3：草稿清理配置
        self._cleanup_age_days: int = int(cfg.get("cleanup_age_days", 7))
        self._cleanup_enabled: bool = bool(cfg.get("cleanup_enabled", True))
        self._last_cleanup_ts: float = 0.0
        self._cleanup_interval: float = 86400.0  # 每日执行一次

        # 指标
        self.total_sent: int = 0
        self.total_errors: int = 0
        self.last_run_ts: float = 0.0
        self.last_sent: int = 0
        self.last_error: str = ""
        self.cycles: int = 0
        self.event_triggers: int = 0  # C3：记录事件驱动触发次数
        self.total_cleaned: int = 0   # H3：历史清理草稿总数

    # ── 生命周期 ──────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    def notify_new_l2(self) -> None:
        """从任意线程安全地通知 worker：有新 L2 草稿已落库，立即唤醒（C3 事件驱动）。

        由 InboxStore.register_l2_callback 注册调用。
        使用 loop.call_soon_threadsafe 避免跨线程 asyncio 竞态。
        """
        if self._loop is not None and self._l2_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._l2_event.set)
            except RuntimeError:
                pass  # event loop 已停止

    async def run(self) -> None:
        if not self._enabled:
            logger.info("[AutosendWorker] L2 自动发送已禁用（config.enabled=false）")
            return
        # C3：在 run() 内初始化，确保绑定到正确的 event loop
        self._loop = asyncio.get_running_loop()
        self._l2_event = asyncio.Event()
        self._running = True
        logger.info(
            "[AutosendWorker] 启动（事件驱动+定时兜底）— min_interval=%.0fs max_interval=%.0fs "
            "circuit_threshold=%d cooldown=%.0fs",
            self._min_interval, self._max_interval,
            self._circuit_threshold, self._cooldown_sec,
        )
        # 首次启动延迟，避免与服务启动争资源
        await asyncio.sleep(self._min_interval)
        while self._running:
            await self._tick()
            jitter = random.uniform(-0.1, 0.1) * self._current_interval
            wait_sec = max(5.0, self._current_interval + jitter)
            # C3：等待事件或定时器兜底
            try:
                await asyncio.wait_for(self._l2_event.wait(), timeout=wait_sec)
                self._l2_event.clear()
                self.event_triggers += 1
                logger.debug("[AutosendWorker] L2 事件触发，提前唤醒")
            except asyncio.TimeoutError:
                pass  # 定时兜底触发，正常

    # ── 单轮逻辑 ─────────────────────────────────────────────

    async def _tick(self) -> None:
        self.cycles += 1
        self.last_run_ts = time.time()

        # 检查熔断器
        if self._circuit_open:
            elapsed = time.time() - self._circuit_open_ts
            if elapsed < self._cooldown_sec:
                logger.debug(
                    "[AutosendWorker] 熔断中，剩余冷却 %.0fs", self._cooldown_sec - elapsed
                )
                return
            # 半开：尝试恢复
            self._circuit_open = False
            self._consecutive_errors = 0
            logger.info("[AutosendWorker] 熔断冷却完毕，进入半开状态尝试恢复")

        try:
            sent, errors = await asyncio.get_event_loop().run_in_executor(
                None, self._process_batch
            )
        except Exception as exc:
            errors = 1
            sent = 0
            self.last_error = str(exc)
            logger.error("[AutosendWorker] 批次执行异常: %s", exc, exc_info=True)

        self.last_sent = sent
        self.total_sent += sent
        self.total_errors += errors

        if errors > 0:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._circuit_threshold:
                self._circuit_open = True
                self._circuit_open_ts = time.time()
                logger.warning(
                    "[AutosendWorker] 连续 %d 次错误，熔断器开启，冷却 %.0fs",
                    self._consecutive_errors, self._cooldown_sec,
                )
        else:
            self._consecutive_errors = 0

        # 自适应间隔
        self._adapt_interval(sent)

        if sent > 0:
            logger.info("[AutosendWorker] 本轮发送 %d 条，错误 %d 条", sent, errors)
        else:
            logger.debug("[AutosendWorker] 本轮无 L2 待发草稿")

        # H3：每日清理超龄已处理草稿（best-effort，不影响发送主流程）
        if self._cleanup_enabled and (time.time() - self._last_cleanup_ts) > self._cleanup_interval:
            try:
                store = getattr(self._svc, "_store", None)
                if store is not None and hasattr(store, "cleanup_old_drafts"):
                    n = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: store.cleanup_old_drafts(max_age_days=self._cleanup_age_days),
                    )
                    self.total_cleaned += n
                    self._last_cleanup_ts = time.time()
            except Exception:
                logger.debug("[AutosendWorker] cleanup_old_drafts 失败", exc_info=True)

    def _process_batch(self) -> tuple[int, int]:
        """同步：列出 L2 pending 草稿并逐条自动发送。每条隔离 try/except。"""
        drafts = self._svc.list_drafts(status="pending", limit=200)
        l2 = [d for d in drafts if d.get("autopilot_level") == "L2"]
        sent, errors = 0, 0
        for d in l2:
            draft_id = d.get("draft_id", "")
            try:
                result = self._svc.resolve_with_audit(draft_id, "autosend", by="autosend_worker")
                if result.get("ok"):
                    sent += 1
                else:
                    errors += 1
                    logger.debug(
                        "[AutosendWorker] draft_id=%s resolve 返回 not-ok: %s",
                        draft_id, result.get("error"),
                    )
            except Exception as exc:
                errors += 1
                logger.warning(
                    "[AutosendWorker] draft_id=%s 发送异常: %s", draft_id, exc
                )
        return sent, errors

    def _adapt_interval(self, sent: int) -> None:
        """自适应间隔：有发送→缩短；无发送→指数扩张到 max_interval。"""
        if sent > 0:
            self._current_interval = self._min_interval
        else:
            self._current_interval = min(
                self._current_interval * 1.5, self._max_interval
            )

    # ── 指标快照 ──────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "cycles": self.cycles,
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
            "last_sent": self.last_sent,
            "last_run_ts": self.last_run_ts,
            "last_error": self.last_error,
            "circuit_open": self._circuit_open,
            "consecutive_errors": self._consecutive_errors,
            "current_interval_sec": round(self._current_interval, 1),
            "event_triggers": self.event_triggers,  # C3：事件驱动唤醒次数
            "total_cleaned": self.total_cleaned,     # H3：历史清理草稿总数
            "total_sent_session": self.total_sent,   # E3 健康面板兼容字段
        }
