"""InboundXlateBackfillWorker — 入站翻译「存量消化」低频巡检（默认关）。

背景（2026-07 /thread 性能重构收尾）：入站翻译改为「打开会话时同步 2 条 + 其余后台补译」
后，**老会话首开**仍要经历一次「同步 2 条 + 后台消化」的数秒窗口（一次性）。本 worker
把这次消化挪到闲时：低频扫最近活跃会话，把未译存量提前译好落库——坐席首开任何会话
都毫秒级 + 译文已备好。

设计要点：
- **完全复用** ``enrich_inbound_translations``（shim request 提供 app.state.inbox_store）：
  候选判定 / noop 打标 / 失败负缓存 / 会话+消息级 in-flight 锁 / 观测计数 全部同一套，
  与在线路径零漂移、零重复翻译（会话级 ``_BG_CONVS`` 锁对 worker 与 /thread 同样生效）。
- **默认关**（``workspace.auto_translate_inbound.backfill.enabled=false``，feature flag
  约定）；**每 tick 重读配置**热生效，无需重启。主开关（auto_translate_inbound.enabled）
  关闭时 enrich 直接空转，worker 天然跟随。
- **限流**：每 tick 最多扫 ``scan_convs`` 个最近活跃会话；其中「实际产生翻译工作」
  （同步译出/转后台）的会话数达到 ``max_active_convs`` 即停止本 tick——防冷启动时几十个
  会话同时后台补译打满翻译引擎、挤占在线请求。已消化完的会话候选为 0，扫描零成本。
- 与 AutosendWorker / SLAWatcher / AutoClaimWorker 同构（run/stop/status_snapshot）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SEC = 300.0     # 巡检间隔（低频，闲时消化）
_DEFAULT_SCAN_CONVS = 20          # 每 tick 扫描的最近活跃会话数
_DEFAULT_MAX_ACTIVE = 2           # 每 tick 允许「实际开工」的会话数（控引擎并发）
_MSGS_PER_CONV = 100              # 每会话读取的最近消息数（与 /thread limit 同口径）


def parse_backfill_cfg(config_manager) -> Dict[str, Any]:
    """读 ``workspace.auto_translate_inbound.backfill``（缺省全关/保守值）。"""
    out = {
        "enabled": False,
        "interval_sec": _DEFAULT_INTERVAL_SEC,
        "scan_convs": _DEFAULT_SCAN_CONVS,
        "max_active_convs": _DEFAULT_MAX_ACTIVE,
    }
    try:
        root = (getattr(config_manager, "config", None) or {}) if config_manager else {}
        ws = (root.get("workspace") or {}) if isinstance(root, dict) else {}
        ax = ws.get("auto_translate_inbound") or {}
        bf = ax.get("backfill") if isinstance(ax, dict) else None
        if isinstance(bf, dict):
            out["enabled"] = bool(bf.get("enabled", False))
            if bf.get("interval_sec") is not None:
                out["interval_sec"] = max(30.0, float(bf["interval_sec"]))
            if bf.get("scan_convs") is not None:
                out["scan_convs"] = max(1, min(100, int(bf["scan_convs"])))
            if bf.get("max_active_convs") is not None:
                out["max_active_convs"] = max(1, min(10, int(bf["max_active_convs"])))
    except Exception:
        logger.debug("parse backfill cfg 失败，用默认关", exc_info=True)
    return out


class InboundXlateBackfillWorker:
    """入站翻译存量消化后台任务（默认关，热开关）。"""

    def __init__(
        self,
        *,
        inbox_store: Any,
        config_manager: Any = None,
        translation_svc_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = inbox_store
        self._config_manager = config_manager
        # translation_service 在 main.py 晚于本 worker 挂到 web_app.state → 懒取
        self._svc_getter = translation_svc_getter or (lambda: None)
        self._running = False
        self._stop_evt = asyncio.Event()

        self.total_ticks = 0
        self.total_convs_scanned = 0
        self.total_convs_worked = 0     # 实际产生翻译工作（同步译出/转后台）的会话数
        self.total_translated = 0       # worker 同步侧译出（后台侧计入 inbound_translation.bg_*）
        self.total_deferred = 0
        self.last_tick_ts = 0.0

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._stop_evt.clear()
        logger.info("InboundXlateBackfillWorker 已启动（默认关，按 backfill.enabled 热生效）")
        while not self._stop_evt.is_set():
            interval = _DEFAULT_INTERVAL_SEC
            try:
                cfg = parse_backfill_cfg(self._config_manager)
                interval = float(cfg.get("interval_sec") or _DEFAULT_INTERVAL_SEC)
                if cfg.get("enabled"):
                    await self._tick(cfg)
            except Exception:
                logger.exception("InboundXlateBackfillWorker tick 出错（已忽略）")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_evt.wait()), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
        self._running = False
        logger.info("InboundXlateBackfillWorker 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 核心 tick ─────────────────────────────────────────────────────────

    async def _tick(self, cfg: Dict[str, Any]) -> None:
        self.total_ticks += 1
        self.last_tick_ts = time.time()
        if self._store is None:
            return
        svc = None
        try:
            svc = self._svc_getter()
        except Exception:
            svc = None
        if svc is None:
            return  # translation_service 未就绪（启动早期）→ 下 tick 再试
        try:
            convs = self._store.list_conversations(limit=int(cfg["scan_convs"]))
        except Exception:
            logger.debug("backfill 读取会话列表失败（已忽略）", exc_info=True)
            return

        from src.inbox.normalizer import store_message_to_obj
        from src.workspace.inbound_translate import enrich_inbound_translations

        shim = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(inbox_store=self._store)))
        worked = 0
        for c in convs or []:
            if self._stop_evt.is_set() or worked >= int(cfg["max_active_convs"]):
                break
            cid = str(c.get("conversation_id") or "")
            if not cid:
                continue
            self.total_convs_scanned += 1
            try:
                rows = self._store.list_recent_messages(cid, limit=_MSGS_PER_CONV)
            except Exception:
                continue
            if not rows:
                continue
            msgs = [store_message_to_obj(r) for r in rows]
            try:
                _, stats = await enrich_inbound_translations(
                    shim, msgs, conversation_id=cid,
                    config_manager=self._config_manager, translation_svc=svc,
                )
            except Exception:
                logger.debug("backfill enrich 失败 conv=%s（已忽略）", cid, exc_info=True)
                continue
            if not stats.get("enabled"):
                return  # 主开关关着 → 本 tick 直接收工（配置热跟随）
            did = int(stats.get("translated") or 0) + int(stats.get("deferred") or 0)
            self.total_translated += int(stats.get("translated") or 0)
            self.total_deferred += int(stats.get("deferred") or 0)
            if did > 0:
                worked += 1
                self.total_convs_worked += 1
                logger.info(
                    "backfill 消化 conv=%s 同步=%d 转后台=%d noop=%d",
                    cid, stats.get("translated", 0), stats.get("deferred", 0),
                    stats.get("noop", 0))

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        cfg = parse_backfill_cfg(self._config_manager)
        return {
            "running": self._running,
            "enabled": bool(cfg.get("enabled")),
            "interval_sec": cfg.get("interval_sec"),
            "total_ticks": self.total_ticks,
            "total_convs_scanned": self.total_convs_scanned,
            "total_convs_worked": self.total_convs_worked,
            "total_translated": self.total_translated,
            "total_deferred": self.total_deferred,
            "last_tick_ts": self.last_tick_ts,
        }
