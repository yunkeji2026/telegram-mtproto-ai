"""持久化用户对话上下文 — SQLite 后端 + 内存 LRU 缓存"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("ContextStore")

_PERSIST_KEYS = frozenset({
    "user_id", "last_message", "last_reply", "last_reply_time",
    "recent_replies",
    "reply_count", "stage", "topic", "current_intent",
    "gxp_pending_order_no", "gxp_pending_time", "gxp_pending_chat_id",
    "gxp_last_ask", "chat_id", "chat_title",
    "_bot_question_ts", "_bot_question_intent",
    "_conversation_history", "_conversation_summary", "_user_profile",
    "_intent_chain", "_case_id",
    "companion_relationship",
})

_NON_PERSIST = frozenset({
    "_send_to_chat", "_record_gxp_cmd", "_i18n", "_event_tracker",
    "context_analysis", "image_ocr_text", "recent_bot_messages",
    "request_id", "user_emotion_hint", "user_msg_id", "batch_pending",
    "_channel_followup_brief",
    "_episodic_memory_text",
    "_slow_think_outline",
    "_relationship_prompt_block",
    "relationship_stage",
    "_funnel_directive",   # W3-3M: per-request, re-computed each turn
    "funnel_stage",        # W3-3M: injected by runner, not persistent
    "_bond_level_block",   # Phase ②: per-request 关系成长厚度/里程碑感知块
    "_story_block",        # Phase ③: per-request 剧情场景导演指令（story_state 才持久）
})


class ContextStore:

    _DDL = """
    CREATE TABLE IF NOT EXISTS user_context (
        user_id TEXT PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ctx_updated ON user_context(updated_at);
    """

    def __init__(self, db_path: Path, ttl_days: int = 30, max_memory: int = 500):
        self._db_path = db_path
        self._ttl = ttl_days * 86400
        self._max_memory = max_memory
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._dirty: set = set()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._cleanup_expired()

    def _cleanup_expired(self):
        cutoff = time.time() - self._ttl
        try:
            deleted = self._conn.execute(
                "DELETE FROM user_context WHERE updated_at < ?", (cutoff,)
            ).rowcount
            self._conn.commit()
            if deleted:
                logger.info("清理过期上下文: %d 条", deleted)
        except Exception:
            pass

    def get(self, user_id: str) -> Dict[str, Any]:
        if user_id in self._cache:
            return self._cache[user_id]
        row = self._conn.execute(
            "SELECT data FROM user_context WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            try:
                ctx = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        else:
            ctx = {}
        ctx.setdefault("user_id", user_id)
        ctx.setdefault("last_message", "")
        ctx.setdefault("last_reply", "")
        ctx.setdefault("last_reply_time", 0)
        ctx.setdefault("reply_count", 0)
        ctx.setdefault("stage", "start")
        ctx.setdefault("topic", "")
        ctx.setdefault("data", {})
        self._cache[user_id] = ctx
        self._evict_if_needed()
        return ctx

    def mark_dirty(self, user_id: str):
        self._dirty.add(user_id)

    def flush(self, user_id: str = ""):
        targets = [user_id] if user_id else list(self._dirty)
        now = time.time()
        for uid in targets:
            ctx = self._cache.get(uid)
            if not ctx:
                continue
            persist = {k: v for k, v in ctx.items()
                       if k in _PERSIST_KEYS or (k not in _NON_PERSIST and _is_serializable(v))}
            try:
                data_json = json.dumps(persist, ensure_ascii=False, default=str)
                self._conn.execute(
                    "INSERT OR REPLACE INTO user_context (user_id, data, updated_at) VALUES (?, ?, ?)",
                    (uid, data_json, now)
                )
            except Exception as e:
                logger.debug("上下文持久化失败 %s: %s", uid, e)
            self._dirty.discard(uid)
        try:
            self._conn.commit()
        except Exception:
            pass

    def flush_all(self):
        self.flush()

    def _evict_if_needed(self):
        if len(self._cache) <= self._max_memory:
            return
        self.flush()
        sorted_users = sorted(
            self._cache.items(),
            key=lambda kv: kv[1].get("last_reply_time", 0)
        )
        remove_count = len(self._cache) - self._max_memory // 2
        for uid, _ in sorted_users[:remove_count]:
            self._cache.pop(uid, None)

    def close(self):
        self.flush_all()
        if self._conn:
            self._conn.close()
            self._conn = None


def _is_serializable(v) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), list, dict))
