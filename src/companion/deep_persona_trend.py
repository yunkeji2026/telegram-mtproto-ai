"""深度人设「趋势/AB」观测骨架（默认关，仿 translation_trend_store）。

`deep_persona_stats` 是**进程级累计**（重启清零）。本模块按**天**快照落库，供 ops 卡出
7 天 sparkline、以及「开/关深度人设」的前后对比（A/B ROI）。默认关
（`companion.deep_persona.trend_log`）；开后由 /api/workspace/metrics 读出时机会式 upsert 当天。

纯轻量 SQLite，best-effort，绝不阻塞主链路，不记任何原文。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 快照字段（与 deep_persona_stats 计数键对齐 + embedder 关键指标）
_FIELDS = (
    "consolidations", "profiles_built", "jokes_detected", "experiential_added",
    "open_loops_added", "loops_resolved", "callbacks_emitted", "life_shares",
    "drift_blocked", "embed_calls", "embed_avg_latency_ms",
)


class DeepPersonaTrendStore:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        try:
            cols = ", ".join(f"{f} REAL NOT NULL DEFAULT 0" for f in _FIELDS)
            with self._lock, self._conn() as c:
                c.execute(
                    f"CREATE TABLE IF NOT EXISTS deep_persona_daily ("
                    f"day TEXT PRIMARY KEY, {cols})"
                )
        except Exception:
            logger.debug("[deep_persona_trend] ensure failed", exc_info=True)

    def upsert_today(self, snapshot: Dict[str, Any], *, day: Optional[str] = None) -> None:
        """用当天累计快照覆盖当天行（累计值随进程增长，覆盖即得当日最新态）。"""
        d = day or date.today().isoformat()
        vals = {}
        for f in _FIELDS:
            try:
                vals[f] = float(snapshot.get(f, 0) or 0)
            except (TypeError, ValueError):
                vals[f] = 0.0
        try:
            cols = ", ".join(_FIELDS)
            ph = ", ".join("?" for _ in _FIELDS)
            setc = ", ".join(f"{f}=excluded.{f}" for f in _FIELDS)
            with self._lock, self._conn() as c:
                c.execute(
                    f"INSERT INTO deep_persona_daily(day, {cols}) VALUES(?, {ph}) "
                    f"ON CONFLICT(day) DO UPDATE SET {setc}",
                    [d] + [vals[f] for f in _FIELDS],
                )
        except Exception:
            logger.debug("[deep_persona_trend] upsert failed", exc_info=True)

    def read_recent(self, days: int = 7) -> List[Dict[str, Any]]:
        try:
            with self._lock, self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM deep_persona_daily ORDER BY day DESC LIMIT ?",
                    (max(1, int(days)),),
                ).fetchall()
                return [dict(r) for r in reversed(rows)]
        except Exception:
            return []


def flatten_stats_for_trend(stats_dump: Dict[str, Any]) -> Dict[str, Any]:
    """把 deep_persona_stats.dump()（含嵌套 embedder）拍平成趋势快照字段。纯函数。"""
    out: Dict[str, Any] = {}
    d = stats_dump or {}
    for f in _FIELDS:
        if f in d:
            out[f] = d.get(f, 0)
    emb = d.get("embedder") or {}
    if isinstance(emb, dict):
        out["embed_calls"] = emb.get("calls", 0)
        out["embed_avg_latency_ms"] = emb.get("avg_latency_ms", 0)
    return out


_SINGLETON: Optional[DeepPersonaTrendStore] = None
_LOCK = threading.Lock()


def get_deep_persona_trend(db_path: str | Path | None = None) -> Optional[DeepPersonaTrendStore]:
    global _SINGLETON
    if _SINGLETON is None:
        if db_path is None:
            return None
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = DeepPersonaTrendStore(db_path)
    return _SINGLETON


def reset_deep_persona_trend() -> None:
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None


__all__ = [
    "DeepPersonaTrendStore", "get_deep_persona_trend", "reset_deep_persona_trend",
    "flatten_stats_for_trend",
]
