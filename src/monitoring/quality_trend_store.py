"""陪伴主动消息「质量趋势」的时序持久化（SQLite）。

背景与定位
----------
`metrics_store.companion_quality_overview()` 是基于内存 deque 的即时聚合，进程重启即丢，
看不到「质量在变好还是变差」的趋势。本模块周期性把那份聚合**快照**为时序行落地，供看板
画趋势线（care/reactivation 的 like_rate、skip、dry_run、黑名单规模随时间变化）。

读写解耦、可注入、可单测、默认随 companion.quality_trend.enabled 开（默认关）。
- `QualityTrendStore`：SQLite 持久化（扁平标量列便于趋势 + 原始 payload JSON 备查）。
- `QualityTrendSnapshotter`：周期循环，定时取 overview_fn() 落库 + 按保留期清理。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# overview 提供者：() -> companion_quality_overview() 的返回 dict
OverviewFn = Callable[[], Dict[str, Any]]

_DDL = """
CREATE TABLE IF NOT EXISTS quality_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    window_sec      INTEGER NOT NULL DEFAULT 86400,
    care_skipped    INTEGER NOT NULL DEFAULT 0,
    care_dry        INTEGER NOT NULL DEFAULT 0,
    care_like       INTEGER NOT NULL DEFAULT 0,
    care_dislike    INTEGER NOT NULL DEFAULT 0,
    re_scheduled    INTEGER NOT NULL DEFAULT 0,
    re_skipped      INTEGER NOT NULL DEFAULT 0,
    re_dry          INTEGER NOT NULL DEFAULT 0,
    re_like         INTEGER NOT NULL DEFAULT 0,
    re_dislike      INTEGER NOT NULL DEFAULT 0,
    blacklist_size  INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_quality_ts ON quality_snapshots (ts);
"""

_SCALAR_COLS = [
    "ts", "window_sec", "care_skipped", "care_dry", "care_like", "care_dislike",
    "re_scheduled", "re_skipped", "re_dry", "re_like", "re_dislike",
    "blacklist_size",
]


def _flatten(overview: Dict[str, Any]) -> Dict[str, int]:
    """把 companion_quality_overview() 的嵌套结构压成趋势用的扁平标量。"""
    care = overview.get("care") or {}
    re = overview.get("reactivation") or {}
    cfb = care.get("feedback") or {}
    rfb = re.get("feedback") or {}

    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "window_sec": _i(overview.get("window_sec") or 86400),
        "care_skipped": _i(care.get("skipped")),
        "care_dry": _i(care.get("dry_run")),
        "care_like": _i(cfb.get("like")),
        "care_dislike": _i(cfb.get("dislike")),
        "re_scheduled": _i(re.get("scheduled")),
        "re_skipped": _i(re.get("skipped")),
        "re_dry": _i(re.get("dry_run")),
        "re_like": _i(rfb.get("like")),
        "re_dislike": _i(rfb.get("dislike")),
        "blacklist_size": _i(overview.get("disliked_blacklist_size")),
    }


class QualityTrendStore:
    """质量趋势时序的持久化（线程安全 SQLite）。"""

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

    def record_snapshot(
        self, overview: Dict[str, Any], *, now: Optional[float] = None,
    ) -> int:
        """落一行快照。返回 row id（>0 成功）。overview 为空 → 不写，返回 0。"""
        if not overview:
            return 0
        n = float(now if now is not None else time.time())
        flat = _flatten(overview)
        try:
            payload = json.dumps(overview, ensure_ascii=False)
        except Exception:
            payload = "{}"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO quality_snapshots "
                "(ts, window_sec, care_skipped, care_dry, care_like, care_dislike, "
                " re_scheduled, re_skipped, re_dry, re_like, re_dislike, "
                " blacklist_size, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n, flat["window_sec"], flat["care_skipped"], flat["care_dry"],
                 flat["care_like"], flat["care_dislike"], flat["re_scheduled"],
                 flat["re_skipped"], flat["re_dry"], flat["re_like"],
                 flat["re_dislike"], flat["blacklist_size"], payload),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def recent(
        self, *, since_ts: Optional[float] = None, limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """取最近快照（按时间升序，便于直接画线）。"""
        lim = max(1, min(int(limit or 2000), 5000))
        with self._lock:
            if since_ts is not None:
                rows = self._conn.execute(
                    "SELECT * FROM quality_snapshots WHERE ts>=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (float(since_ts), lim),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM quality_snapshots ORDER BY ts DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        out = []
        for r in reversed(rows):  # DESC 取最近 N 条后反转 → 升序
            d = dict(r)
            d.pop("payload", None)  # 趋势线只用标量；payload 备查不外发
            out.append(d)
        return out

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quality_snapshots ORDER BY ts DESC LIMIT 1",
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:
            d["payload"] = {}
        return d

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM quality_snapshots").fetchone()
        return int(row["c"] if row else 0)

    def prune(
        self, *, older_than_sec: float = 2592000.0, now: Optional[float] = None,
    ) -> int:
        """删除超过保留期（默认 30 天）的旧快照。返回删除条数。"""
        n = float(now if now is not None else time.time())
        cut = n - max(0.0, float(older_than_sec))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM quality_snapshots WHERE ts < ?", (cut,))
            self._conn.commit()
            return cur.rowcount


class QualityTrendSnapshotter:
    """周期性把 overview_fn() 落库的循环（可注入、可单测、优雅停止）。"""

    def __init__(
        self,
        *,
        store: QualityTrendStore,
        overview_fn: OverviewFn,
        interval_sec: float = 300.0,
        retention_days: float = 30.0,
    ) -> None:
        self._store = store
        self._overview_fn = overview_fn
        self._interval = max(30.0, float(interval_sec))
        self._retention_sec = max(3600.0, float(retention_days) * 86400.0)
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._ticks = 0

    def snapshot_once(self, *, now: Optional[float] = None) -> int:
        """同步快照一次（取 overview → 落库）。返回 row id（0=无数据/失败）。"""
        try:
            overview = self._overview_fn() or {}
        except Exception:
            logger.debug("quality_trend overview_fn 取值失败", exc_info=True)
            return 0
        try:
            return self._store.record_snapshot(overview, now=now)
        except Exception:
            logger.debug("quality_trend record_snapshot 失败", exc_info=True)
            return 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="quality_trend")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                self.snapshot_once()
                self._ticks += 1
                # 每 ~每天的 tick 数清理一次旧数据（避免每 tick 都 DELETE）
                if self._ticks % max(1, int(86400 / self._interval)) == 0:
                    try:
                        self._store.prune(older_than_sec=self._retention_sec)
                    except Exception:
                        logger.debug("quality_trend prune 失败", exc_info=True)
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(
                            self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("quality_trend 循环退出")


__all__ = [
    "QualityTrendStore",
    "QualityTrendSnapshotter",
    "OverviewFn",
]
