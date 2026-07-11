"""S：翻译置信度「按日落库」时序持久化（SQLite）。

背景与定位
----------
``translation_engine_stats``（K/M）是**进程内**累计——重启即归零，ops 看板只能看「当下
低置信率/切换率」的瞬时快照，看不到「这几天是不是越来越糟」的趋势（运营据此判断主引擎
质量是否在劣化、要不要换引擎）。本模块把每次 translate 的 {尝试/低置信/切换} 按日增量
upsert 落地，供看板画近 N 天 sparkline（与 ``tts_cost_store`` 同构）。

设计（对齐 tts_cost_store）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，无周期快照线程，写在翻译热路上
  （仅在置信度智能切换开启时才有切换/低置信发生；attempts 每次 translate +1）。
- **默认关**：未 ``configure_translation_trend_store(enabled=True, ...)`` → record 恒 no-op，零 IO。
- **模块级单例**：与 translation_engine_stats 同构，路由旁路调用一行。
- 只存元数据（日期/计数），绝不记录任何译文。
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
CREATE TABLE IF NOT EXISTS xlate_trend_daily (
    day       TEXT NOT NULL PRIMARY KEY,
    attempts  INTEGER NOT NULL DEFAULT 0,
    low_conf  INTEGER NOT NULL DEFAULT 0,
    switches  INTEGER NOT NULL DEFAULT 0,
    sem_low   INTEGER NOT NULL DEFAULT 0
);
"""

# 既有库升级（sem_low 列 2026-07 加入；ALTER 幂等失败即已存在）
_MIGRATIONS = [
    "ALTER TABLE xlate_trend_daily ADD COLUMN sem_low INTEGER NOT NULL DEFAULT 0",
]


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（跨时区部署口径一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class TranslationTrendStore:
    """翻译置信度按日聚合（线程安全 SQLite）。"""

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
            for mig in _MIGRATIONS:
                try:
                    self._conn.execute(mig)
                except sqlite3.OperationalError:
                    pass  # 列已存在（新建库走 DDL）
            self._conn.commit()

    def add(
        self, *, attempts: int = 0, low_conf: int = 0, switches: int = 0,
        sem_low: int = 0, now: Optional[float] = None,
    ) -> None:
        """把一组增量计入当日聚合。绝不抛。"""
        a, l, s = max(0, int(attempts)), max(0, int(low_conf)), max(0, int(switches))
        m = max(0, int(sem_low))
        if a == 0 and l == 0 and s == 0 and m == 0:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO xlate_trend_daily (day, attempts, low_conf, switches, sem_low) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  attempts = attempts + excluded.attempts, "
                    "  low_conf = low_conf + excluded.low_conf, "
                    "  switches = switches + excluded.switches, "
                    "  sem_low = sem_low + excluded.sem_low",
                    (day, a, l, s, m),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[xlate_trend] add 失败（已忽略）", exc_info=True)

    def daily(
        self, *, days: int = 7, now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近 N 天的按日聚合（升序）。缺数据补零，曲线不断点；含低置信率/切换率。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        rows: Dict[str, sqlite3.Row] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, attempts, low_conf, switches, sem_low "
                    "FROM xlate_trend_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    rows[r["day"]] = r
        except Exception:
            logger.debug("[xlate_trend] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            r = rows.get(day)
            att = int(r["attempts"]) if r else 0
            low = int(r["low_conf"]) if r else 0
            sw = int(r["switches"]) if r else 0
            sl = int(r["sem_low"]) if r else 0
            out.append({
                "day": day,
                "attempts": att,
                "low_conf": low,
                "switches": sw,
                "sem_low": sl,
                "low_conf_rate": round(low / att, 4) if att else 0.0,
                "switch_rate": round(sw / att, 4) if att else 0.0,
                "sem_low_rate": round(sl / att, 4) if att else 0.0,
            })
        return out

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM xlate_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[xlate_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 tts_cost_store 同构）──────────────────────────
_STORE: Optional[TranslationTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_translation_trend_store(
    *,
    enabled: bool,
    db_path: Any = ":memory:",
    retention_days: float = 90.0,
) -> Optional[TranslationTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = TranslationTrendStore(db_path)
            except Exception:
                logger.warning("[xlate_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_translation_trend_store() -> Optional[TranslationTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_translation_trend(
    *, attempts: int = 0, low_conf: int = 0, switches: int = 0, sem_low: int = 0,
) -> None:
    """翻译热路旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(attempts=attempts, low_conf=low_conf, switches=switches,
               sem_low=sem_low)


def reset_translation_trend_store() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "TranslationTrendStore",
    "configure_translation_trend_store",
    "get_translation_trend_store",
    "record_translation_trend",
    "reset_translation_trend_store",
]
