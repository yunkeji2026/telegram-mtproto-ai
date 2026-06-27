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
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 投递回调签名：async (platform, account_id, chat_key, text) -> dict
SendCallback = Callable[[str, str, str, str], Awaitable[Dict[str, Any]]]


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
        send_callback: Optional[SendCallback] = None,
        sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    ) -> None:
        cfg = config or {}
        self._svc = draft_service
        self._enabled: bool = bool(cfg.get("enabled", True))
        # 真实投递回调（None=仅 DB 标记不发，保持旧行为；非 None=L2 草稿 resolve 后真投递）。
        # 由 main.py 在 inbox.l2_autosend.deliver=true 时注入，gating 在注入处。
        self._send_callback: Optional[SendCallback] = send_callback
        self._min_interval: float = float(cfg.get("min_interval_sec", 60))
        self._max_interval: float = float(cfg.get("max_interval_sec", 600))
        # 首次启动延迟（默认=min_interval 保持兼容）。可被新 L2 草稿事件提前唤醒，
        # 因此全自动首条回复实际延迟 ≈ 草稿落库瞬间，而非固定等满此值。
        self._startup_delay: float = float(cfg.get("startup_delay_sec", self._min_interval))
        self._circuit_threshold: int = int(cfg.get("circuit_threshold", 5))
        self._cooldown_sec: float = float(cfg.get("cooldown_sec", 300))

        # Phase 4：投递前拟人延迟（模拟打字，降低秒回露馅/反封号）。
        # config.inbox.l2_autosend.deliver_delay = {min_sec, max_sec}；默认 0=不延迟（向后兼容）。
        _dd = cfg.get("deliver_delay") or {}
        try:
            self._deliver_delay_min: float = float(_dd.get("min_sec", 0) or 0)
            self._deliver_delay_max: float = float(_dd.get("max_sec", 0) or 0)
        except Exception:
            self._deliver_delay_min, self._deliver_delay_max = 0.0, 0.0
        # 可注入 sleep（测试用）；生产用 asyncio.sleep
        self._sleep: Callable[[float], Awaitable[Any]] = sleep or asyncio.sleep

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
        self.total_delivered: int = 0       # 真正投递到平台的条数
        self.total_deliver_errors: int = 0  # 投递失败条数（已 resolve 但平台发送失败）

    # ── 生命周期 ──────────────────────────────────────────────

    def stop(self) -> None:
        self._running = False

    def _pick_deliver_delay(self) -> float:
        """按 deliver_delay 配置取随机拟人延迟（秒）。未配置/非法 → 0。"""
        lo, hi = self._deliver_delay_min, self._deliver_delay_max
        if hi <= 0 or hi < lo:
            return 0.0
        return random.uniform(max(0.0, lo), hi)

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
        # 首次启动延迟，避免与服务启动争资源；但用可中断等待——
        # 若启动延迟期间有新 L2 草稿落库（事件触发），立即提前唤醒，不再傻等满 startup_delay。
        if self._startup_delay > 0:
            try:
                await asyncio.wait_for(self._l2_event.wait(), timeout=self._startup_delay)
                self._l2_event.clear()
                self.event_triggers += 1
            except asyncio.TimeoutError:
                pass
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

        to_deliver: List[Dict[str, Any]] = []
        try:
            sent, errors, to_deliver = await asyncio.get_event_loop().run_in_executor(
                None, self._process_batch
            )
        except Exception as exc:
            errors = 1
            sent = 0
            self.last_error = str(exc)
            logger.error("[AutosendWorker] 批次执行异常: %s", exc, exc_info=True)

        # 真实投递：草稿已 resolve（DB 标记 approved + autosend 审计），现把文本发到平台。
        # resolve-先于-deliver：投递失败宁可丢一条也不重发（draft 已非 pending，下轮不再选中），
        # 杜绝向客户刷屏。失败计入 deliver_errors 但不触发熔断（熔断只看 resolve 错误）。
        if self._send_callback is not None and to_deliver:
            for item in to_deliver:
                try:
                    # Phase 4：投递前拟人延迟（模拟打字；只在确定要发时才等）
                    _delay = self._pick_deliver_delay()
                    if _delay > 0:
                        await self._sleep(_delay)
                    res = await self._send_callback(
                        item.get("platform", ""), item.get("account_id", "default"),
                        item.get("chat_key", ""), item.get("text", ""),
                    )
                    if isinstance(res, dict) and res.get("ok") is False:
                        raise RuntimeError(str(res.get("error") or "send not ok"))
                    self.total_delivered += 1
                except Exception as exc:  # noqa: BLE001
                    self.total_deliver_errors += 1
                    self.last_error = f"deliver: {exc}"
                    logger.warning(
                        "[AutosendWorker] 投递失败 conv=%s platform=%s: %s",
                        item.get("conversation_id", "?"), item.get("platform", "?"), exc,
                    )
                    # 写 autosend_failed 审计，让安全条/记录弹窗看见「自动发了但没送达」
                    try:
                        rec = getattr(self._svc, "record_autosend_failure", None)
                        if rec is not None:
                            rec(
                                item.get("draft_id", ""),
                                conversation_id=item.get("conversation_id", ""),
                                reason=f"平台投递失败: {exc}",
                            )
                    except Exception:
                        logger.debug("[AutosendWorker] autosend_failed 审计写入失败", exc_info=True)

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
                    # P3：顺带清理超龄出向译文旁路记录（best-effort，独立 try）
                    if hasattr(store, "cleanup_outbound_translations"):
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None, store.cleanup_outbound_translations,
                            )
                        except Exception:
                            logger.debug(
                                "[AutosendWorker] cleanup_outbound_translations 失败",
                                exc_info=True)
                    self._last_cleanup_ts = time.time()
            except Exception:
                logger.debug("[AutosendWorker] cleanup_old_drafts 失败", exc_info=True)

    def _process_batch(self) -> "tuple[int, int, List[Dict[str, Any]]]":
        """同步：列出 L2 pending 草稿并逐条 resolve（DB 标记 + 审计）。每条隔离 try/except。

        返回 (sent, errors, to_deliver)：to_deliver 是已成功 resolve、需要真正投递到平台的
        草稿载荷列表（platform/account_id/chat_key/text）。投递本身在 async 的 _tick 里做。
        """
        drafts = self._svc.list_drafts(status="pending", limit=200)
        l2 = [d for d in drafts if d.get("autopilot_level") == "L2"]
        sent, errors = 0, 0
        to_deliver: List[Dict[str, Any]] = []
        for d in l2:
            draft_id = d.get("draft_id", "")
            # 投递用文本优先取最终文本，回落草稿文本
            text = str(d.get("final_text") or d.get("draft_text") or "").strip()
            try:
                result = self._svc.resolve_with_audit(draft_id, "autosend", by="autosend_worker")
                if result.get("ok"):
                    sent += 1
                    if self._send_callback is not None and text:
                        to_deliver.append({
                            "draft_id": draft_id,
                            "conversation_id": d.get("conversation_id", ""),
                            "platform": str(d.get("platform") or ""),
                            "account_id": str(d.get("account_id") or "default"),
                            "chat_key": str(d.get("chat_key") or ""),
                            "text": text,
                        })
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
        return sent, errors, to_deliver

    def _adapt_interval(self, sent: int) -> None:
        """自适应间隔：有发送→缩短；无发送→指数扩张到 max_interval。"""
        if sent > 0:
            self._current_interval = self._min_interval
        else:
            self._current_interval = min(
                self._current_interval * 1.5, self._max_interval
            )

    # ── 运维动作 ──────────────────────────────────────────────

    def reset_circuit(self) -> bool:
        """手动重置熔断器（H2 一键动作）。

        当熔断因连续错误开启、但根因已被人工排除时，主管可立即闭合熔断让 worker
        恢复，无需等冷却期。返回「调用前是否处于熔断态」（True=确实做了重置）。
        """
        was_open = self._circuit_open
        self._circuit_open = False
        self._circuit_open_ts = 0.0
        self._consecutive_errors = 0
        if was_open:
            logger.info("[AutosendWorker] 熔断器被手动重置（运维一键动作）")
        return was_open

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
            "deliver_enabled": self._send_callback is not None,  # 是否真正投递到平台
            "total_delivered": self.total_delivered,
            "total_deliver_errors": self.total_deliver_errors,
        }
