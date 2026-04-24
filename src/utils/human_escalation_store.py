"""
人工客服转接：值班状态 + 重复问句计数 + 冷却（SQLite）。
"""

import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple


class HumanEscalationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS human_shift (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                on_duty INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO human_shift (id, on_duty, updated_at) VALUES (1, 0, 0);
            CREATE TABLE IF NOT EXISTS repeat_streak (
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                norm_key TEXT NOT NULL,
                cnt INTEGER NOT NULL DEFAULT 1,
                first_ts REAL NOT NULL,
                last_ts REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id, norm_key)
            );
            CREATE INDEX IF NOT EXISTS idx_repeat_last ON repeat_streak(last_ts);
            CREATE TABLE IF NOT EXISTS escalation_cooldown (
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                last_ts REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            -- 按「归一化问句」维度的转接冷却：A 句刚 @ 过，B 句仍可在凑满阈值后再次 @
            CREATE TABLE IF NOT EXISTS escalation_cooldown_by_norm (
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                norm_key TEXT NOT NULL,
                last_ts REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id, norm_key)
            );
            CREATE INDEX IF NOT EXISTS idx_esc_norm_ts ON escalation_cooldown_by_norm(last_ts);
            CREATE TABLE IF NOT EXISTS mention_round_robin (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                idx INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO mention_round_robin (id, idx, updated_at) VALUES (1, 0, 0);
            CREATE TABLE IF NOT EXISTS mention_round_robin_chat (
                chat_id TEXT PRIMARY KEY,
                idx INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            """)

    def get_shift_on_duty(self) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT on_duty FROM human_shift WHERE id=1").fetchone()
            return bool(r and r["on_duty"])

    def set_shift_on_duty(self, on: bool) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE human_shift SET on_duty=?, updated_at=? WHERE id=1",
                (1 if on else 0, now),
            )

    def record_repeat(
        self, chat_id: str, user_id: str, norm_key: str, window_sec: float
    ) -> int:
        """返回当前连续同文案计数（窗口外重置为 1）。"""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT cnt, last_ts FROM repeat_streak WHERE chat_id=? AND user_id=? AND norm_key=?",
                (chat_id, user_id, norm_key),
            ).fetchone()
            if not row:
                c.execute(
                    "INSERT INTO repeat_streak (chat_id, user_id, norm_key, cnt, first_ts, last_ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (chat_id, user_id, norm_key, 1, now, now),
                )
                return 1
            last_ts = float(row["last_ts"])
            if now - last_ts > window_sec:
                c.execute(
                    "UPDATE repeat_streak SET cnt=1, first_ts=?, last_ts=? "
                    "WHERE chat_id=? AND user_id=? AND norm_key=?",
                    (now, now, chat_id, user_id, norm_key),
                )
                return 1
            new_cnt = int(row["cnt"]) + 1
            c.execute(
                "UPDATE repeat_streak SET cnt=?, last_ts=? "
                "WHERE chat_id=? AND user_id=? AND norm_key=?",
                (new_cnt, now, chat_id, user_id, norm_key),
            )
            return new_cnt

    def reset_repeat_key(self, chat_id: str, user_id: str, norm_key: str) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM repeat_streak WHERE chat_id=? AND user_id=? AND norm_key=?",
                (chat_id, user_id, norm_key),
            )

    def cooldown_remaining(
        self, chat_id: str, user_id: str, cooldown_sec: float
    ) -> Tuple[bool, float]:
        """(可触发 True / 不可触发 False, 剩余秒数)"""
        if cooldown_sec <= 0:
            return True, 0.0
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT last_ts FROM escalation_cooldown WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return True, 0.0
            elapsed = now - float(row["last_ts"])
            if elapsed >= cooldown_sec:
                return True, 0.0
            return False, cooldown_sec - elapsed

    def mark_escalation(self, chat_id: str, user_id: str) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO escalation_cooldown (chat_id, user_id, last_ts) VALUES (?,?,?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_ts=excluded.last_ts",
                (chat_id, user_id, now),
            )

    def cooldown_remaining_norm(
        self,
        chat_id: str,
        user_id: str,
        norm_key: str,
        cooldown_sec: float,
    ) -> Tuple[bool, float]:
        """按 (chat, user, 问句指纹) 的转接冷却；与 repeat_streak 的 norm_key 一致。"""
        if cooldown_sec <= 0:
            return True, 0.0
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT last_ts FROM escalation_cooldown_by_norm "
                "WHERE chat_id=? AND user_id=? AND norm_key=?",
                (chat_id, user_id, norm_key),
            ).fetchone()
            if not row:
                return True, 0.0
            elapsed = now - float(row["last_ts"])
            if elapsed >= cooldown_sec:
                return True, 0.0
            return False, cooldown_sec - elapsed

    def mark_escalation_norm(self, chat_id: str, user_id: str, norm_key: str) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO escalation_cooldown_by_norm (chat_id, user_id, norm_key, last_ts) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id, user_id, norm_key) DO UPDATE SET last_ts=excluded.last_ts",
                (chat_id, user_id, norm_key, now),
            )

    def round_robin_next_index(
        self, n: int, chat_id: Optional[str] = None
    ) -> int:
        """
        返回 [0,n) 内下一名客服下标并递增计数。
        chat_id 非空时使用 per-chat 计数（多群独立轮询）；否则全局一条计数。
        """
        if n <= 1:
            return 0
        now = time.time()
        with self._conn() as c:
            if chat_id:
                r = c.execute(
                    "SELECT idx FROM mention_round_robin_chat WHERE chat_id=?",
                    (chat_id,),
                ).fetchone()
                cur = int(r["idx"]) if r else 0
                out = cur % n
                c.execute(
                    "INSERT INTO mention_round_robin_chat (chat_id, idx, updated_at) "
                    "VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                    "idx=excluded.idx, updated_at=excluded.updated_at",
                    (chat_id, cur + 1, now),
                )
                return out
            r = c.execute("SELECT idx FROM mention_round_robin WHERE id=1").fetchone()
            cur = int(r["idx"]) if r else 0
            out = cur % n
            c.execute(
                "UPDATE mention_round_robin SET idx=?, updated_at=? WHERE id=1",
                (cur + 1, now),
            )
            return out

    def get_round_robin_snapshot(self, limit: int = 50) -> Tuple[int, List[Tuple[str, int, float]]]:
        """
        全局轮询计数 + 最近更新的 per-chat 行（运维调试）。
        """
        lim = max(1, min(int(limit), 200))
        with self._conn() as c:
            r = c.execute("SELECT idx FROM mention_round_robin WHERE id=1").fetchone()
            gidx = int(r["idx"]) if r else 0
            rows = c.execute(
                "SELECT chat_id, idx, updated_at FROM mention_round_robin_chat "
                "ORDER BY updated_at DESC LIMIT ?",
                (lim,),
            ).fetchall()
        out: List[Tuple[str, int, float]] = [
            (str(row["chat_id"]), int(row["idx"]), float(row["updated_at"]))
            for row in rows
        ]
        return gidx, out
