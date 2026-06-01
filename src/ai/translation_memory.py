"""TranslationMemoryStore — 翻译记忆持久层（Phase C2）。

复刻 src/contacts/store.py 范式（单 connection + Lock + WAL + 幂等迁移）。
替换 TranslationService 的进程内 TTL 缓存：跨重启命中、可统计命中次数。

cache_key 由调用方算（含 glossary_ver），术语库改版即自动失效旧译。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS translation_memory (
    cache_key       TEXT PRIMARY KEY,
    source_text     TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    source_lang     TEXT NOT NULL,
    target_lang     TEXT NOT NULL,
    style           TEXT NOT NULL DEFAULT 'chat',
    engine          TEXT NOT NULL DEFAULT 'ai',
    glossary_ver    TEXT NOT NULL DEFAULT '',
    hit_count       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_hit_at     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tm_lang ON translation_memory(source_lang, target_lang);
CREATE INDEX IF NOT EXISTS idx_tm_hits ON translation_memory(hit_count DESC);
"""


class TranslationMemoryStore:
    def __init__(self, db_path) -> None:
        self._db_path = Path(db_path) if str(db_path) != ":memory:" else ":memory:"
        if self._db_path != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if self._db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @staticmethod
    def _now() -> float:
        return time.time()

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """命中则 hit_count++ 并返回行 dict；未命中返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM translation_memory WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE translation_memory SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE cache_key = ?",
                (self._now(), cache_key),
            )
            self._conn.commit()
            return dict(row)

    def put(
        self,
        cache_key: str,
        *,
        source_text: str,
        translated_text: str,
        source_lang: str,
        target_lang: str,
        style: str = "chat",
        engine: str = "ai",
        glossary_ver: str = "",
    ) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO translation_memory
                    (cache_key, source_text, translated_text, source_lang, target_lang,
                     style, engine, glossary_ver, hit_count, created_at, last_hit_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translated_text = excluded.translated_text,
                    engine = excluded.engine,
                    glossary_ver = excluded.glossary_ver
                """,
                (
                    cache_key, source_text[:4000], translated_text[:4000],
                    source_lang, target_lang, style, engine, glossary_ver, now,
                ),
            )
            self._conn.commit()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count),0) AS hits "
                "FROM translation_memory"
            ).fetchone()
        return {"entries": int(row["n"]), "total_hits": int(row["hits"])}
