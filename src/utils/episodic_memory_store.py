"""
Episodic memory: short, persistent user-specific facts for multi-session continuity.
Stored in SQLite (default: same file as ContextStore bot.db), separate table.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("EpisodicMemoryStore")


def compute_memory_storage_key(scope: str, user_id_str: str, chat_id: Any) -> str:
    """
    scope=user → 仅 user_id；scope=chat_user → 群为「chat_user_id」，私聊 chat_id==user 时退化为 user。
    """
    if (scope or "user") != "chat_user":
        return user_id_str
    try:
        cid = int(chat_id) if chat_id is not None and str(chat_id).strip() != "" else 0
    except (TypeError, ValueError):
        cid = 0
    try:
        uid = int(str(user_id_str).strip()) if str(user_id_str).strip().isdigit() else 0
    except (TypeError, ValueError):
        uid = 0
    if cid != 0 and uid != 0 and cid == uid:
        return user_id_str
    if cid == 0:
        return user_id_str
    return f"{cid}_{user_id_str}"


def _norm_for_hash(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:500]


class EpisodicMemoryStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_epi_user_created ON episodic_memory(user_id, created_at DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_epi_user_hash ON episodic_memory(user_id, content_hash);
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _ensure_embedding_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE episodic_memory ADD COLUMN embedding BLOB")
            self._conn.commit()
            logger.info("episodic_memory: added column embedding")

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._ensure_embedding_column()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def add_fact(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding_blob: Optional[bytes] = None,
    ) -> Optional[int]:
        """Insert one fact; returns new row id, or None if duplicate / failed."""
        c = (content or "").strip()
        if len(c) < 2 or len(c) > 500:
            return None
        h = hashlib.sha256(_norm_for_hash(c).encode("utf-8")).hexdigest()
        now = time.time()
        try:
            cur = self._conn.execute(
                "INSERT INTO episodic_memory (user_id, content, content_hash, category, created_at, embedding)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, c, h, (category or "general")[:32], now, embedding_blob),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
        except sqlite3.IntegrityError:
            return None
        except Exception as e:
            logger.debug("episodic insert failed: %s", e)
            return None

    def update_embedding(self, row_id: int, embedding_blob: bytes) -> bool:
        if not embedding_blob:
            return False
        try:
            cur = self._conn.execute(
                "UPDATE episodic_memory SET embedding = ? WHERE id = ?",
                (embedding_blob, int(row_id)),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0
        except Exception as e:
            logger.debug("episodic update_embedding failed: %s", e)
            return False

    def count(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def prune_oldest(self, user_id: str, keep: int) -> int:
        """Keep at most `keep` rows (by recency). Returns deleted count."""
        n = self.count(user_id)
        if n <= keep:
            return 0
        to_drop = n - keep
        cur = self._conn.execute(
            """
            DELETE FROM episodic_memory WHERE id IN (
                SELECT id FROM episodic_memory WHERE user_id = ?
                ORDER BY created_at ASC LIMIT ?
            )
            """,
            (user_id, to_drop),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    @staticmethod
    def _keyword_overlap_score(query: str, content: str) -> float:
        """Lightweight overlap: 2–4 char substrings from query (no extra deps)."""
        q = (query or "").strip()
        c = (content or "").strip()
        if len(q) < 2 or not c:
            return 0.0
        q = q[:200]
        score = 0.0
        step = 1 if len(q) < 24 else 2
        for L in (4, 3, 2):
            for i in range(0, max(1, len(q) - L + 1), step):
                frag = q[i : i + L]
                if len(frag) < L:
                    break
                if frag in c:
                    score += float(L)
        return score

    def get_bullets_for_prompt(
        self,
        user_id: str,
        max_items: int = 8,
        max_chars: int = 1200,
        query_text: Optional[str] = None,
        rerank_keywords: bool = False,
        query_embedding: Optional[List[float]] = None,
        use_vector_fusion: bool = False,
        vector_weight: float = 0.5,
        keyword_weight: float = 0.5,
    ) -> str:
        """Newline bullets; optional vector+keyword fusion when query_embedding set."""
        from src.utils.episodic_vector import blob_to_vec, cosine_similarity

        max_items = max(1, min(int(max_items or 8), 40))
        max_chars = max(100, min(int(max_chars or 1200), 8000))
        qt = (query_text or "").strip()
        want_kw = rerank_keywords and len(qt) >= 2
        want_vec = bool(
            use_vector_fusion and query_embedding and len(query_embedding) >= 8
        )
        fetch_n = max_items * 6 if (want_kw or want_vec) else max_items * 2
        fetch_n = min(fetch_n, 120)

        rows = self._conn.execute(
            """
            SELECT content, embedding FROM episodic_memory WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, fetch_n),
        ).fetchall()
        if not rows:
            return ""

        pairs: List[Tuple[str, Optional[bytes]]] = [
            (r[0].strip(), r[1]) for r in rows if r and r[0]
        ]
        if not pairs:
            return ""

        contents: List[str]
        if want_vec:
            vw = max(0.0, min(1.0, float(vector_weight)))
            kw_w = max(0.0, min(1.0, float(keyword_weight)))
            s = vw + kw_w
            if s > 1e-9:
                vw, kw_w = vw / s, kw_w / s
            kws = [
                self._keyword_overlap_score(qt, t) if want_kw else 0.0 for t, _ in pairs
            ]
            max_kw = max(kws) if kws else 0.0
            scored_rows: List[Tuple[float, str]] = []
            for (t, emb_blob), kw in zip(pairs, kws):
                kw_n = (kw / max_kw) if max_kw > 1e-9 else 0.0
                ev = blob_to_vec(emb_blob)
                vs = cosine_similarity(query_embedding, ev) if ev else 0.0
                vs = max(0.0, min(1.0, (vs + 1.0) / 2.0))
                fusion = vw * vs + kw_w * kw_n
                scored_rows.append((fusion, t))
            scored_rows.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [x[1] for x in scored_rows]
        elif want_kw:
            scored: List[Tuple[float, str]] = []
            for t, _ in pairs:
                sc = self._keyword_overlap_score(qt, t)
                scored.append((sc, t))
            scored.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [x[1] for x in scored]
        else:
            contents = [p[0] for p in pairs]

        lines: List[str] = []
        total = 0
        for content in contents:
            line = f"- {content}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
            if len(lines) >= max_items:
                break
        return "\n".join(lines)

    def list_rows(
        self,
        prefix: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Admin: recent rows, optional filter on memory key (user_id column)."""
        limit = max(1, min(int(limit or 100), 500))
        p = (prefix or "").strip()
        if p:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content, category, created_at,
                  CASE WHEN embedding IS NOT NULL AND length(embedding) >= 8 THEN 1 ELSE 0 END
                FROM episodic_memory WHERE user_id LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (f"%{p}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content, category, created_at,
                  CASE WHEN embedding IS NOT NULL AND length(embedding) >= 8 THEN 1 ELSE 0 END
                FROM episodic_memory
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "memory_key": r[1],
                "content": r[2],
                "category": r[3],
                "created_at": r[4],
                "has_embedding": bool(r[5]),
            })
        return out

    def delete_by_id(self, row_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE id = ?", (int(row_id),)
        )
        self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def fetch_rows_missing_embedding(
        self, limit: int = 20, memory_key_prefix: str = ""
    ) -> List[Tuple[int, str, str]]:
        """Rows (id, memory_key, content) needing vector backfill. Optional filter on user_id."""
        limit = max(1, min(int(limit or 20), 200))
        p = (memory_key_prefix or "").strip()
        if p:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content FROM episodic_memory
                WHERE (embedding IS NULL OR length(embedding) < 8)
                  AND user_id LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{p}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content FROM episodic_memory
                WHERE embedding IS NULL OR length(embedding) < 8
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def clear_user(self, user_id: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)
