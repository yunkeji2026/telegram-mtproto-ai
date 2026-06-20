"""R9 危机事件落库/审计。

R4→R6→R8 形成了"输入预防 → 输出兜底 → 真人接管"的安全链，但全程只有 webhook + 日志，
**留不下结构化记录**。一个会触及真实心理危机的陪聊产品，必须能事后复盘、合规审计、
追踪每起危机是否被人工处理。本模块在 SQLite 落一张轻量 ``crisis_event`` 表：

- 记录：时间 / 用户 / 会话 / 等级 / 类别 / 连击 / 是否触发升级 / 是否触发安全兜底 / 短摘要；
- 查询：最近事件、未处理事件、按用户筛；
- 处置：标记"已人工处理"+ 处理人 + 备注。

隐私：默认关（由 ``companion.wellbeing.crisis_audit`` 开），且只存**短摘要**（≤120 字）非全文。
纯存储、平台无关、可单测。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CrisisEventStore")


class CrisisEventStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS crisis_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        streak INTEGER NOT NULL DEFAULT 1,
        escalated INTEGER NOT NULL DEFAULT 0,
        safety_override INTEGER NOT NULL DEFAULT 0,
        excerpt TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        handled INTEGER NOT NULL DEFAULT 0,
        handled_by TEXT NOT NULL DEFAULT '',
        handled_at REAL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_crisis_created ON crisis_event(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_crisis_user ON crisis_event(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_crisis_handled ON crisis_event(handled, created_at DESC);
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def record(
        self,
        *,
        user_id: str,
        level: str,
        chat_id: str = "",
        category: str = "",
        streak: int = 1,
        escalated: bool = False,
        safety_override: bool = False,
        excerpt: str = "",
    ) -> Optional[int]:
        """落一条危机事件；返回行 id，失败返回 None（绝不抛，避免影响主回复）。"""
        try:
            cur = self._conn.execute(
                "INSERT INTO crisis_event (user_id, chat_id, level, category, streak,"
                " escalated, safety_override, excerpt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(user_id), str(chat_id), str(level), str(category)[:32],
                    int(streak), 1 if escalated else 0, 1 if safety_override else 0,
                    str(excerpt or "")[:120], time.time(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event record failed: %s", e)
            return None

    def list_recent(
        self,
        limit: int = 50,
        *,
        only_unhandled: bool = False,
        user_prefix: str = "",
        match_key: str = "",
    ) -> List[Dict[str, Any]]:
        """最近危机事件。

        ``user_prefix``：仅按 ``user_id`` 前缀筛（后台审计页用）。
        ``match_key``（R9e）：按 ``user_id`` 前缀**或** ``chat_id`` 精确匹配——一个 key
        同时覆盖 1:1 私聊（key=对端 user_id）与群聊（key=群 chat_id），供坐席侧栏用。
        """
        lim = max(1, min(int(limit or 50), 500))
        where = []
        params: List[Any] = []
        if only_unhandled:
            where.append("handled = 0")
        if user_prefix:
            where.append("user_id LIKE ?")
            params.append(f"{user_prefix}%")
        if match_key:
            where.append("(user_id LIKE ? OR chat_id = ?)")
            params.append(f"{match_key}%")
            params.append(str(match_key))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(lim)
        try:
            rows = self._conn.execute(
                "SELECT id, user_id, chat_id, level, category, streak, escalated,"
                " safety_override, excerpt, created_at, handled, handled_by, handled_at, note"
                f" FROM crisis_event{clause} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event list failed: %s", e)
            return []
        cols = [
            "id", "user_id", "chat_id", "level", "category", "streak", "escalated",
            "safety_override", "excerpt", "created_at", "handled", "handled_by",
            "handled_at", "note",
        ]
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(zip(cols, r))
            d["escalated"] = bool(d["escalated"])
            d["safety_override"] = bool(d["safety_override"])
            d["handled"] = bool(d["handled"])
            out.append(d)
        return out

    def mark_handled(
        self, event_id: int, *, handled_by: str = "", note: str = "",
    ) -> bool:
        try:
            cur = self._conn.execute(
                "UPDATE crisis_event SET handled = 1, handled_by = ?, handled_at = ?,"
                " note = ? WHERE id = ?",
                (str(handled_by)[:64], time.time(), str(note)[:500], int(event_id)),
            )
            self._conn.commit()
            return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("crisis_event mark_handled failed: %s", e)
            return False

    def count(self, *, only_unhandled: bool = False) -> int:
        try:
            q = "SELECT COUNT(*) FROM crisis_event"
            if only_unhandled:
                q += " WHERE handled = 0"
            row = self._conn.execute(q).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


__all__ = ["CrisisEventStore"]
