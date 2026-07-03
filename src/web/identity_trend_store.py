"""F1：会话身份健康「按日落库」时序持久化（SQLite）。

背景与定位
----------
``PeerIdentityStats``（B/C/D/E）是**进程内**累计——重启即归零，ops 看板只能看「当下 raw% /
empty%」的瞬时快照，看不到「这几天名字/头像覆盖是不是在变好」的**趋势**（运营据此判断补名/
抓头像的改动有没有见效、要不要继续投入）。本模块把两条关键健康信号按日增量 upsert 落地：

- **入站裸 id 率**（``raw%``）：经 HTTP ingest 的号（WhatsApp/Messenger）里仍是「一排数字」的占比
  —— named/backfilled/raw 三分（对齐 ``PeerIdentityStats.record_ingest``）。
- **头像空率**（``empty%``）/命中率（``hit%``）：头像端点结局里「拿到图（cache_hit+fetched）」vs
  「空（无头像/抓取盲区）」占比（对齐 ``PeerIdentityStats.record_avatar``，覆盖 WA/Messenger/TG）。

设计（严格对齐 ``translation_trend_store`` / ``tts_cost_store``）：
- **纯增量 upsert**：``INSERT ... ON CONFLICT DO UPDATE``，无周期快照线程，写在 record 旁路。
- **默认关**：未 ``configure_identity_trend_store(enabled=True, ...)`` → record 恒 no-op，零 IO。
- **模块级单例**，只存元数据（日期/计数），绝不记录任何会话内容 / id。
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
CREATE TABLE IF NOT EXISTS identity_trend_daily (
    day            TEXT NOT NULL PRIMARY KEY,
    ing_named      INTEGER NOT NULL DEFAULT 0,
    ing_backfilled INTEGER NOT NULL DEFAULT 0,
    ing_raw        INTEGER NOT NULL DEFAULT 0,
    av_hit         INTEGER NOT NULL DEFAULT 0,
    av_empty       INTEGER NOT NULL DEFAULT 0,
    av_total       INTEGER NOT NULL DEFAULT 0
);
"""

_COLS = ("ing_named", "ing_backfilled", "ing_raw", "av_hit", "av_empty", "av_total")


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（跨时区部署口径一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class IdentityTrendStore:
    """会话身份健康按日聚合（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def add(
        self, *, ing_named: int = 0, ing_backfilled: int = 0, ing_raw: int = 0,
        av_hit: int = 0, av_empty: int = 0, av_total: int = 0,
        now: Optional[float] = None,
    ) -> None:
        """把一组增量计入当日聚合。全零跳过。绝不抛。"""
        vals = {
            "ing_named": max(0, int(ing_named)),
            "ing_backfilled": max(0, int(ing_backfilled)),
            "ing_raw": max(0, int(ing_raw)),
            "av_hit": max(0, int(av_hit)),
            "av_empty": max(0, int(av_empty)),
            "av_total": max(0, int(av_total)),
        }
        if not any(vals.values()):
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO identity_trend_daily "
                    "(day, ing_named, ing_backfilled, ing_raw, av_hit, av_empty, av_total) "
                    "VALUES (:day, :ing_named, :ing_backfilled, :ing_raw, :av_hit, :av_empty, :av_total) "
                    "ON CONFLICT(day) DO UPDATE SET "
                    "  ing_named = ing_named + excluded.ing_named, "
                    "  ing_backfilled = ing_backfilled + excluded.ing_backfilled, "
                    "  ing_raw = ing_raw + excluded.ing_raw, "
                    "  av_hit = av_hit + excluded.av_hit, "
                    "  av_empty = av_empty + excluded.av_empty, "
                    "  av_total = av_total + excluded.av_total",
                    {"day": day, **vals},
                )
                self._conn.commit()
        except Exception:
            logger.debug("[identity_trend] add 失败（已忽略）", exc_info=True)

    def daily(self, *, days: int = 7, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """近 N 天的按日聚合（升序）。缺数据补零，曲线不断点；含 raw%/empty%/hit%。"""
        n = max(1, min(int(days or 7), 90))
        base = now if now is not None else time.time()
        day_keys = [_day_str(base - i * 86400) for i in range(n - 1, -1, -1)]
        rows: Dict[str, sqlite3.Row] = {}
        try:
            with self._lock:
                for r in self._conn.execute(
                    "SELECT day, ing_named, ing_backfilled, ing_raw, av_hit, av_empty, av_total "
                    "FROM identity_trend_daily WHERE day >= ? ORDER BY day",
                    (day_keys[0],),
                ).fetchall():
                    rows[r["day"]] = r
        except Exception:
            logger.debug("[identity_trend] daily 读取失败（已忽略）", exc_info=True)
            return []

        out: List[Dict[str, Any]] = []
        for day in day_keys:
            r = rows.get(day)
            ing_named = int(r["ing_named"]) if r else 0
            ing_backfilled = int(r["ing_backfilled"]) if r else 0
            ing_raw = int(r["ing_raw"]) if r else 0
            av_hit = int(r["av_hit"]) if r else 0
            av_empty = int(r["av_empty"]) if r else 0
            av_total = int(r["av_total"]) if r else 0
            ing_total = ing_named + ing_backfilled + ing_raw
            out.append({
                "day": day,
                "ing_named": ing_named,
                "ing_backfilled": ing_backfilled,
                "ing_raw": ing_raw,
                "ing_total": ing_total,
                "raw_rate": round(ing_raw / ing_total, 4) if ing_total else 0.0,
                "av_hit": av_hit,
                "av_empty": av_empty,
                "av_total": av_total,
                "empty_rate": round(av_empty / av_total, 4) if av_total else 0.0,
                "hit_rate": round(av_hit / av_total, 4) if av_total else 0.0,
            })
        return out

    def prune(self, *, retention_days: float = 90.0, now: Optional[float] = None) -> int:
        """删除超过保留期的旧日聚合。返回删除条数。"""
        base = now if now is not None else time.time()
        cut = _day_str(base - max(0.0, float(retention_days)) * 86400)
        try:
            with self._lock:
                c = self._conn.execute(
                    "DELETE FROM identity_trend_daily WHERE day < ?", (cut,))
                self._conn.commit()
                return int(c.rowcount or 0)
        except Exception:
            logger.debug("[identity_trend] prune 失败（已忽略）", exc_info=True)
            return 0


# ── 模块级单例 + 默认关闸门（与 translation_trend_store 同构）────────────────
_STORE: Optional[IdentityTrendStore] = None
_ENABLED = False
_RETENTION_DAYS = 90.0
_CFG_LOCK = threading.Lock()


def configure_identity_trend_store(
    *, enabled: bool, db_path: Any = ":memory:", retention_days: float = 90.0,
) -> Optional[IdentityTrendStore]:
    """启动期装配（幂等）。``enabled=False`` → 关闭旁路写入（record 恒 no-op）。"""
    global _STORE, _ENABLED, _RETENTION_DAYS
    with _CFG_LOCK:
        _ENABLED = bool(enabled)
        _RETENTION_DAYS = max(1.0, float(retention_days or 90.0))
        if not _ENABLED:
            return _STORE
        if _STORE is None:
            try:
                _STORE = IdentityTrendStore(db_path)
            except Exception:
                logger.warning("[identity_trend] 建库失败，禁用落库", exc_info=True)
                _STORE = None
                _ENABLED = False
        return _STORE


def get_identity_trend_store() -> Optional[IdentityTrendStore]:
    """供读端点取 store；未配置 → None。"""
    return _STORE


def record_identity_trend(
    *, ing_named: int = 0, ing_backfilled: int = 0, ing_raw: int = 0,
    av_hit: int = 0, av_empty: int = 0, av_total: int = 0,
) -> None:
    """身份 record 旁路写入：未启用 / 无 store → 立即返回（零开销）。绝不抛。"""
    if not _ENABLED or _STORE is None:
        return
    _STORE.add(ing_named=ing_named, ing_backfilled=ing_backfilled, ing_raw=ing_raw,
               av_hit=av_hit, av_empty=av_empty, av_total=av_total)


def reset_identity_trend_store() -> None:
    """测试钩子：清空单例与开关。"""
    global _STORE, _ENABLED
    with _CFG_LOCK:
        _STORE = None
        _ENABLED = False


__all__ = [
    "IdentityTrendStore",
    "configure_identity_trend_store",
    "get_identity_trend_store",
    "record_identity_trend",
    "reset_identity_trend_store",
]
