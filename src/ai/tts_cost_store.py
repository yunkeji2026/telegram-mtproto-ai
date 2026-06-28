"""P4-B：TTS 成本 / 用量「按日落库」时序持久化（SQLite）。

背景与定位
----------
``provider_stats`` 的 "tts" namespace 是**进程内**累计，重启即归零——看板只能看「当下快照」，
看不到「每天花多少、缓存省了多少」的趋势。本模块把每次合成按 ``(日期, 引擎)`` 增量 upsert 落地，
供 ops 看板画近 N 天花费/缓存命中曲线（运营做成本决策最需要的历史视图）。

设计（对齐 quality_trend_store / provider_stats 风格）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，无周期快照线程，写在合成热路上（语音
  非高频，IO 可忽略）。
- **默认关**：未 ``configure_tts_cost_store(enabled=True, ...)`` → ``record_tts_cost`` 恒 no-op，
  无 voice 用量的部署零 IO（遵循"新子系统默认关"）。
- **模块级单例**：与 ``provider_stats`` 同构，pipeline 旁路调用一行，无需把 db 配置层层穿参。
- 只存元数据（日期/引擎/计数/花费），绝不记录任何文本。
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
CREATE TABLE IF NOT EXISTS tts_cost_daily (
    day        TEXT NOT NULL,
    provider   TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    fail       INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (day, provider)
);
CREATE TABLE IF NOT EXISTS tts_cache_daily (
    day         TEXT NOT NULL PRIMARY KEY,
    cache_hits  INTEGER NOT NULL DEFAULT 0
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（跨时区部署口径一致，便于对账）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class TtsCostStore:
    """TTS 按日 (provider) 成本/用量聚合（线程安全 SQLite）。"""

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

    def record(
        self,
        provider: str,
        *,
        ok: bool = True,
        cost_usd: float = 0.0,
        cache_hit: bool = False,
        now: Optional[float] = None,
    ) -> None:
        """记一次合成（或缓存命中）到当日聚合。绝不抛（吞掉所有异常）。"""
        day = _day_str(now)
        try:
            with self._lock:
                if cache_hit:
                    self._conn.execute(
                        "INSERT INTO tts_cache_daily (day, cache_hits) VALUES (?, 1) "
                        "ON CONFLICT(day) DO UPDATE SET cache_hits = cache_hits + 1",
                        (day,),
                    )
                else:
                    p = str(provider or "unknown")
                    c = max(0.0, float(cost_usd or 0.0))
                    self._conn.execute(
                        "INSERT INTO tts_cost_daily (day, provider, calls, ok, fail, cost_usd) "
                        "VALUES (?, ?, 1, ?, ?, ?) "
                        "ON CONFLICT(day, provider) DO UPDATE SET "
                        "  calls = calls + 1, "
                        "  ok = ok + excluded.ok, "
                        "  fail = fail + excluded.fail, "
                        "  cost_usd = cost_usd + excluded.cost_usd",
                        (day, p, 1 if ok else 0, 0 if ok else 1, c),
                    )
                self._conn.commit()
        except Exception:
            logger.debug("[tts_cost] record 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天的按日聚合（升序，便于直接画线）。缺数据的日期补零，曲线不断点。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        # 生成连续 N 天的日期键（含今天），缺的补零。
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        cost_rows: Dict[str, List[sqlite3.Row]] = {}
        cache_map: Dict[str, int] = {}
        try:
            with self._lock:
                since = day_keys[0]
                for r in self._conn.execute(
                    "SELECT day, provider, calls, ok, fail, cost_usd "
                    "FROM tts_cost_daily WHERE day >= ? ORDER BY day",
                    (since,),
                ).fetchall():
                    cost_rows.setdefault(r["day"], []).append(r)
                for r in self._conn.execute(
                    "SELECT day, cache_hits FROM tts_cache_daily WHERE day >= ?",
                    (since,),
                ).fetchall():
                    cache_map[r["day"]] = int(r["cache_hits"] or 0)
        except Exception:
            logger.debug("[tts_cost] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            rows = cost_rows.get(day, [])
            calls = sum(int(r["calls"]) for r in rows)
            ok = sum(int(r["ok"]) for r in rows)
            fail = sum(int(r["fail"]) for r in rows)
            cost = round(sum(float(r["cost_usd"]) for r in rows), 4)
            by_provider = {
                str(r["provider"]): {
                    "calls": int(r["calls"]),
                    "cost_usd": round(float(r["cost_usd"]), 4),
                }
                for r in rows
            }
            out.append({
                "day": day,
                "calls": calls,
                "ok": ok,
                "fail": fail,
                "cost_usd": cost,
                "cache_hits": int(cache_map.get(day, 0)),
                "by_provider": by_provider,
            })
        return out

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数（两表合计）。"""
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c1 = self._conn.execute(
                    "DELETE FROM tts_cost_daily WHERE day < ?", (cut,))
                c2 = self._conn.execute(
                    "DELETE FROM tts_cache_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int((c1.rowcount or 0) + (c2.rowcount or 0))
        except Exception:
            logger.debug("[tts_cost] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 provider_stats 同构）─────────────────────────
_STORE: Optional[TtsCostStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_tts_cost_store(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[TtsCostStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。

    返回 store（启用且建库成功）或 None。建库失败不影响主流程（降级为不落库）。
    """
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = TtsCostStore(db_path)
            except Exception:
                logger.warning("[tts_cost] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_tts_cost_store() -> Optional[TtsCostStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_tts_cost(
    provider: str,
    *,
    ok: bool = True,
    cost_usd: float = 0.0,
    cache_hit: bool = False,
) -> None:
    """合成热路旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.record(provider, ok=ok, cost_usd=cost_usd, cache_hit=cache_hit)


def reset_tts_cost_store() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "TtsCostStore",
    "configure_tts_cost_store",
    "get_tts_cost_store",
    "record_tts_cost",
    "reset_tts_cost_store",
]
