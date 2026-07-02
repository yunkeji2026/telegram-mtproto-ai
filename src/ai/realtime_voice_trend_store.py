"""E 线：实时语音通话「按日落库」时序持久化（SQLite）。

``realtime_voice_stats`` 是进程内累计——重启归零。本模块把 attempt/connected/health/
挂断原因等按日增量 upsert，供 ops 看板画近 N 天 sparkline，并喂
:func:`calibrate_realtime_voice_alert` 做告警回放。

设计对齐 :mod:`translation_trend_store`：
- 纯增量 upsert，写在 stats 热路旁路；
- 默认关（``realtime_voice.trend_log=false``）→ ``record`` 恒 no-op；
- 只存计数，不存音频/转写。
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
CREATE TABLE IF NOT EXISTS rtv_trend_daily (
    day               TEXT NOT NULL PRIMARY KEY,
    attempts          INTEGER NOT NULL DEFAULT 0,
    connected         INTEGER NOT NULL DEFAULT 0,
    health_ok         INTEGER NOT NULL DEFAULT 0,
    health_fail       INTEGER NOT NULL DEFAULT 0,
    host_unreachable  INTEGER NOT NULL DEFAULT 0,
    connect_failed    INTEGER NOT NULL DEFAULT 0
);
"""


def _day_str(now: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _row_to_stats(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if row is None:
        return {
            "attempts": 0, "connected": 0, "connect_rate": 0.0,
            "health_ok": 0, "health_fail": 0, "health_ok_rate": 0.0,
            "by_end_reason": {},
        }
    att = int(row["attempts"])
    conn = int(row["connected"])
    h_ok = int(row["health_ok"])
    h_fail = int(row["health_fail"])
    h_total = h_ok + h_fail
    by_reason: Dict[str, int] = {}
    hu = int(row["host_unreachable"])
    cf = int(row["connect_failed"])
    if hu:
        by_reason["host_unreachable"] = hu
    if cf:
        by_reason["connect_failed"] = cf
    return {
        "attempts": att,
        "connected": conn,
        "connect_rate": round(conn / att, 4) if att else 0.0,
        "health_ok": h_ok,
        "health_fail": h_fail,
        "health_ok_rate": round(h_ok / h_total, 4) if h_total else 0.0,
        "host_unreachable": hu,
        "connect_failed": cf,
        "by_end_reason": by_reason,
    }


class RealtimeVoiceTrendStore:
    """实时语音按日聚合（线程安全 SQLite）。"""

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
        self,
        *,
        attempts: int = 0,
        connected: int = 0,
        health_ok: int = 0,
        health_fail: int = 0,
        host_unreachable: int = 0,
        connect_failed: int = 0,
        now: Optional[float] = None,
    ) -> None:
        vals = [max(0, int(x)) for x in (
            attempts, connected, health_ok, health_fail, host_unreachable, connect_failed)]
        if not any(vals):
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO rtv_trend_daily "
                    "(day, attempts, connected, health_ok, health_fail, "
                    " host_unreachable, connect_failed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  attempts = attempts + excluded.attempts, "
                    "  connected = connected + excluded.connected, "
                    "  health_ok = health_ok + excluded.health_ok, "
                    "  health_fail = health_fail + excluded.health_fail, "
                    "  host_unreachable = host_unreachable + excluded.host_unreachable, "
                    "  connect_failed = connect_failed + excluded.connect_failed",
                    (day, *vals),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[rtv_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天按日聚合（升序）。缺日补零；含 connect_rate / health_ok_rate。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        rows: Dict[str, sqlite3.Row] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, attempts, connected, health_ok, health_fail, "
                    "host_unreachable, connect_failed "
                    "FROM rtv_trend_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    rows[r["day"]] = r
        except Exception:
            logger.debug("[rtv_trend] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            stats = _row_to_stats(rows.get(day))
            out.append({"day": day, **stats})
        return out

    def daily_for_calibrate(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """供 :func:`calibrate_realtime_voice_alert` 回放用的 stats 序列。"""
        return self.daily(days=days, now=now)

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM rtv_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[rtv_trend] prune 失败（已忽略）", exc_info=True)
            return 0


_STORE: Optional[RealtimeVoiceTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()
_SYNC_READY = False
_LAST_SYNCED: Dict[str, int] = {}
_SYNC_KEYS = (
    "attempts", "connected", "health_ok", "health_fail",
    "host_unreachable", "connect_failed",
)


def _stats_to_counts(stats: Dict[str, Any]) -> Dict[str, int]:
    by = stats.get("by_end_reason") or {}
    return {
        "attempts": int(stats.get("attempts") or 0),
        "connected": int(stats.get("connected") or 0),
        "health_ok": int(stats.get("health_ok") or 0),
        "health_fail": int(stats.get("health_fail") or 0),
        "host_unreachable": int(by.get("host_unreachable") or 0),
        "connect_failed": int(by.get("connect_failed") or 0),
    }


def sync_realtime_voice_trend_from_stats(stats: Optional[Dict[str, Any]]) -> None:
    """watchdog tick 兜底：进程 stats 相对上次 sync 的增量写入趋势库（旁路漏记时补）。"""
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


def configure_realtime_voice_trend_store(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[RealtimeVoiceTrendStore]:
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = RealtimeVoiceTrendStore(db_path)
            except Exception:
                logger.warning("[rtv_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_realtime_voice_trend_store() -> Optional[RealtimeVoiceTrendStore]:
    return _STORE


def record_realtime_voice_trend(
    *,
    attempts: int = 0,
    connected: int = 0,
    health_ok: int = 0,
    health_fail: int = 0,
    host_unreachable: int = 0,
    connect_failed: int = 0,
) -> None:
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(
        attempts=attempts, connected=connected,
        health_ok=health_ok, health_fail=health_fail,
        host_unreachable=host_unreachable, connect_failed=connect_failed,
    )


def reset_realtime_voice_trend_store() -> None:
    global _STORE, _ENABLED, _SYNC_READY, _LAST_SYNCED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False
        _SYNC_READY = False
        _LAST_SYNCED.clear()


__all__ = [
    "RealtimeVoiceTrendStore",
    "configure_realtime_voice_trend_store",
    "get_realtime_voice_trend_store",
    "record_realtime_voice_trend",
    "sync_realtime_voice_trend_from_stats",
    "reset_realtime_voice_trend_store",
]
