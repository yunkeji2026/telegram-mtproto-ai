"""事件追踪器 — SQLite 记录命令使用、响应时间，供分析和图表"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("EventTracker")


class EventTracker:

    _DDL = """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        ts_epoch REAL NOT NULL,
        event_type TEXT NOT NULL,
        chat_id INTEGER NOT NULL DEFAULT 0,
        user_id TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        response_ms INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
    CREATE INDEX IF NOT EXISTS idx_events_epoch ON events(ts_epoch);
    """

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def track(self, event_type: str, chat_id: int = 0, user_id: str = "",
              detail: str = "", response_ms: int = 0):
        now = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        try:
            self._conn.execute(
                "INSERT INTO events (ts, ts_epoch, event_type, chat_id, user_id, detail, response_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, now, event_type, chat_id, str(user_id), detail[:200], response_ms),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("事件追踪写入失败: %s", e)

    def command_stats(self, hours: int = 24) -> List[Dict]:
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT event_type, COUNT(*) as cnt, AVG(response_ms) as avg_ms "
            "FROM events WHERE ts_epoch >= ? GROUP BY event_type ORDER BY cnt DESC",
            (cutoff,)
        ).fetchall()
        return [{"type": r["event_type"], "count": r["cnt"],
                 "avg_ms": round(r["avg_ms"] or 0)} for r in rows]

    def hourly_trend(self, hours: int = 24) -> List[Dict]:
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT substr(ts, 1, 13) as hour, COUNT(*) as cnt "
            "FROM events WHERE ts_epoch >= ? GROUP BY hour ORDER BY hour",
            (cutoff,)
        ).fetchall()
        return [{"hour": r["hour"], "count": r["cnt"]} for r in rows]

    def top_users(self, hours: int = 24, limit: int = 10) -> List[Dict]:
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT user_id, COUNT(*) as cnt FROM events "
            "WHERE ts_epoch >= ? AND user_id != '' "
            "GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
            (cutoff, limit)
        ).fetchall()
        return [{"user_id": r["user_id"], "count": r["cnt"]} for r in rows]

    def response_time_distribution(self, hours: int = 24) -> Dict:
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT response_ms FROM events WHERE ts_epoch >= ? AND response_ms > 0",
            (cutoff,)
        ).fetchall()
        if not rows:
            return {"p50": 0, "p90": 0, "p99": 0, "avg": 0, "count": 0}
        vals = sorted(r["response_ms"] for r in rows)
        n = len(vals)
        return {
            "p50": vals[n // 2],
            "p90": vals[int(n * 0.9)],
            "p99": vals[int(n * 0.99)],
            "avg": round(sum(vals) / n),
            "count": n,
        }

    def total_events(self, hours: int = 24) -> int:
        cutoff = time.time() - hours * 3600
        return self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE ts_epoch >= ?", (cutoff,)
        ).fetchone()[0]

    def purge(self, keep_days: int = 90) -> int:
        """删除超过 keep_days 天的旧事件并回收磁盘空间"""
        cutoff = time.time() - keep_days * 86400
        try:
            cur = self._conn.execute(
                "DELETE FROM events WHERE ts_epoch < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted > 0:
                self._conn.execute("VACUUM")
            self._conn.commit()
            logger.info("事件清理: 删除 %d 条 (保留 %d 天)", deleted, keep_days)
            return deleted
        except Exception as e:
            logger.debug("事件清理失败: %s", e)
            return 0

    def close(self):
        if self._conn:
            self._conn.close()
