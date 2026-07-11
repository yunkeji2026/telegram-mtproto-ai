"""P8：出站「路由去向」按日落库时序持久化（SQLite）。

背景与定位
----------
``SendRouteStats``（P1）是**进程内**累计——重启即归零，ops 看板只能看「当下回落率」的
瞬时快照（P2-a 卡片），看不到「这几天回落率是不是在缓升」的趋势（运营据此判断某平台的
编排器 worker 是否在持续劣化、要不要排查）。本模块把 {编排器接管 / 回落适配器} 按日增量
upsert 落地，供看板画近 N 天回落率 sparkline。

设计（对齐 ``realtime_voice_trend_store`` 的 sync-from-stats 模型）：
- ``SendRouteStats`` 是**累计计数器**（非热路事件），故不在热路 record，而由 watchdog tick
  旁路 ``sync_send_route_trend_from_stats`` 把「相对上次同步的增量」写入当日聚合。
- **默认关**：未 ``configure_send_route_trend_store(enabled=True, ...)`` → sync 恒 no-op，零 IO。
- 只存计数（日期 / orchestrator / adapter），绝不记录任何消息内容。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS send_route_trend_daily (
    day           TEXT NOT NULL PRIMARY KEY,
    orchestrator  INTEGER NOT NULL DEFAULT 0,
    adapter       INTEGER NOT NULL DEFAULT 0
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（跨时区部署口径一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class SendRouteTrendStore:
    """出站路由去向按日聚合（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def add(
        self, *, orchestrator: int = 0, adapter: int = 0,
        now: Optional[float] = None,
    ) -> None:
        """把一组增量计入当日聚合。绝不抛。"""
        o, a = max(0, int(orchestrator)), max(0, int(adapter))
        if o == 0 and a == 0:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO send_route_trend_daily (day, orchestrator, adapter) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  orchestrator = orchestrator + excluded.orchestrator, "
                    "  adapter = adapter + excluded.adapter",
                    (day, o, a),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[send_route_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天的按日聚合（升序）。缺数据补零，曲线不断点；含回落率。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        rows: Dict[str, sqlite3.Row] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, orchestrator, adapter "
                    "FROM send_route_trend_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    rows[r["day"]] = r
        except Exception:
            logger.debug("[send_route_trend] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            r = rows.get(day)
            orch = int(r["orchestrator"]) if r else 0
            adp = int(r["adapter"]) if r else 0
            total = orch + adp
            out.append({
                "day": day,
                "orchestrator": orch,
                "adapter": adp,
                "total": total,
                "fallback_rate": round(adp / total, 4) if total else 0.0,
            })
        return out

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM send_route_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[send_route_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门 + sync-from-stats（对齐 realtime_voice_trend_store）───────
_STORE: Optional[SendRouteTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()
_SYNC_READY = False
_LAST_SYNCED: Dict[str, int] = {}
_SYNC_KEYS = ("orchestrator", "adapter")


def _stats_to_counts(stats: Dict[str, Any]) -> Dict[str, int]:
    return {
        "orchestrator": int((stats or {}).get("orchestrator_total") or 0),
        "adapter": int((stats or {}).get("adapter_total") or 0),
    }


def sync_send_route_trend_from_stats(stats: Optional[Dict[str, Any]]) -> None:
    """watchdog tick 兜底：进程 stats 相对上次 sync 的增量写入趋势库。

    首次调用只记基线快照（不写），此后每次写「累计计数的增量」——即便进程重启后
    stats 归零，也不会写负增量（下次以新基线重新累计）。
    """
    global _SYNC_READY, _LAST_SYNCED
    if not _ENABLED or _STORE is None:
        return
    cur = _stats_to_counts(stats or {})
    if not _SYNC_READY:
        _LAST_SYNCED = dict(cur)
        _SYNC_READY = True
        return
    kwargs: Dict[str, int] = {}
    for key in _SYNC_KEYS:
        delta = cur[key] - int(_LAST_SYNCED.get(key, 0))
        if delta > 0:
            kwargs[key] = delta
    if kwargs:
        _STORE.add(**kwargs)
    _LAST_SYNCED = cur


def configure_send_route_trend_store(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[SendRouteTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（sync 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = SendRouteTrendStore(db_path)
            except Exception:
                logger.warning("[send_route_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_send_route_trend_store() -> Optional[SendRouteTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def reset_send_route_trend_store() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED, _SYNC_READY, _LAST_SYNCED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False
        _SYNC_READY = False
        _LAST_SYNCED.clear()


__all__ = [
    "SendRouteTrendStore",
    "configure_send_route_trend_store",
    "get_send_route_trend_store",
    "sync_send_route_trend_from_stats",
    "reset_send_route_trend_store",
]
