"""深度人设运行期状态存储（轻量 SQLite，append-only 语义）。

存 L2/L3b/L4 需要的**跨对话累积**数据，与既有大 store 解耦、独立库文件：
  - relationship_profile：每会话一段"关系画像"（L5 巩固产物，覆盖式更新 + 留时间戳）
  - inside_jokes：每会话的内部梗清单（并集累积，去重 + 上限）
  - open_loops：未收尾话题（append-only + 上限，供"不问就回指"挑选）
  - experiential：经历式记忆（事件+情感+叙事，append-only + 上限）

设计对齐仓库风格：懒建表、best-effort（任何异常吞掉不阻塞主链路）、时间戳可复现。
**隐私**：只存消毒后的短摘要/短语，绝不存原始消息全文。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_JOKES = 12
_MAX_OPEN_LOOPS = 20
_MAX_EXPERIENTIAL = 40


class DeepPersonaStore:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._last_consolidate: Dict[str, float] = {}  # 进程级巩固节流（cid → ts）
        self._ensure_schema()

    def due_for_consolidation(self, conversation_id: str, min_interval_sec: float = 900.0) -> bool:
        """进程级节流：距上次巩固超过 min_interval_sec 才返回 True（并标记）。

        默认 15 分钟一次/会话——避免每条消息都重建关系画像/检测内部梗（省 CPU）。
        进程重启后重置（首条消息会触发一次，可接受）。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        now = time.time()
        with self._lock:
            last = self._last_consolidate.get(cid, 0.0)
            if now - last < float(min_interval_sec):
                return False
            self._last_consolidate[cid] = now
            return True

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure_schema(self) -> None:
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """CREATE TABLE IF NOT EXISTS relationship_profile (
                        conversation_id TEXT PRIMARY KEY,
                        profile TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL DEFAULT 0
                    )"""
                )
                c.execute(
                    """CREATE TABLE IF NOT EXISTS inside_jokes (
                        conversation_id TEXT PRIMARY KEY,
                        jokes_json TEXT NOT NULL DEFAULT '[]',
                        updated_at REAL NOT NULL DEFAULT 0
                    )"""
                )
                c.execute(
                    """CREATE TABLE IF NOT EXISTS open_loops (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        salience REAL NOT NULL DEFAULT 0,
                        ts REAL NOT NULL DEFAULT 0,
                        resolved INTEGER NOT NULL DEFAULT 0
                    )"""
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_open_loops_conv "
                    "ON open_loops(conversation_id, resolved)"
                )
                c.execute(
                    """CREATE TABLE IF NOT EXISTS experiential (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        what TEXT NOT NULL,
                        emotion TEXT NOT NULL DEFAULT '',
                        salience REAL NOT NULL DEFAULT 0,
                        ts REAL NOT NULL DEFAULT 0
                    )"""
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_exp_conv ON experiential(conversation_id)"
                )
                # E1：经历向量缓存列（语义召回用；migration 式 ADD COLUMN，旧库自动补）
                try:
                    _cols = [r[1] for r in c.execute("PRAGMA table_info(experiential)").fetchall()]
                    if "emb" not in _cols:
                        c.execute("ALTER TABLE experiential ADD COLUMN emb TEXT NOT NULL DEFAULT ''")
                except Exception:
                    pass
                c.execute(
                    """CREATE TABLE IF NOT EXISTS life_shares (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        ts REAL NOT NULL DEFAULT 0
                    )"""
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_life_shares_conv "
                    "ON life_shares(conversation_id)"
                )
        except Exception:
            logger.debug("[deep_persona_store] ensure_schema failed", exc_info=True)

    # ── relationship_profile ──────────────────────────────────────────
    def set_relationship_profile(self, conversation_id: str, profile: str) -> None:
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO relationship_profile(conversation_id, profile, updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                    "profile=excluded.profile, updated_at=excluded.updated_at",
                    (cid, str(profile or ""), time.time()),
                )
        except Exception:
            logger.debug("[deep_persona_store] set_profile failed", exc_info=True)

    def get_relationship_profile(self, conversation_id: str) -> str:
        cid = str(conversation_id or "").strip()
        if not cid:
            return ""
        try:
            with self._lock, self._conn() as c:
                row = c.execute(
                    "SELECT profile FROM relationship_profile WHERE conversation_id=?",
                    (cid,),
                ).fetchone()
                return str(row["profile"]) if row else ""
        except Exception:
            return ""

    # ── inside_jokes（并集累积）────────────────────────────────────────
    def add_inside_jokes(self, conversation_id: str, jokes: List[str]) -> None:
        cid = str(conversation_id or "").strip()
        new = [str(j).strip() for j in (jokes or []) if str(j).strip()]
        if not cid or not new:
            return
        try:
            with self._lock, self._conn() as c:
                row = c.execute(
                    "SELECT jokes_json FROM inside_jokes WHERE conversation_id=?", (cid,)
                ).fetchone()
                cur = json.loads(row["jokes_json"]) if row else []
                if not isinstance(cur, list):
                    cur = []
                for j in new:
                    if j not in cur:
                        cur.append(j)
                cur = cur[-_MAX_JOKES:]
                c.execute(
                    "INSERT INTO inside_jokes(conversation_id, jokes_json, updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET "
                    "jokes_json=excluded.jokes_json, updated_at=excluded.updated_at",
                    (cid, json.dumps(cur, ensure_ascii=False), time.time()),
                )
        except Exception:
            logger.debug("[deep_persona_store] add_jokes failed", exc_info=True)

    def get_inside_jokes(self, conversation_id: str) -> List[str]:
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        try:
            with self._lock, self._conn() as c:
                row = c.execute(
                    "SELECT jokes_json FROM inside_jokes WHERE conversation_id=?", (cid,)
                ).fetchone()
                if not row:
                    return []
                v = json.loads(row["jokes_json"])
                return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []

    # ── open_loops（append + 取未收尾）─────────────────────────────────
    def add_open_loop(
        self, conversation_id: str, topic: str, *, salience: float = 0.0,
        ts: Optional[float] = None,
    ) -> None:
        cid = str(conversation_id or "").strip()
        tp = str(topic or "").strip()
        if not cid or not tp:
            return
        try:
            with self._lock, self._conn() as c:
                # 去重：同会话已有相同未收尾 topic 则跳过
                exists = c.execute(
                    "SELECT 1 FROM open_loops WHERE conversation_id=? AND topic=? AND resolved=0",
                    (cid, tp),
                ).fetchone()
                if exists:
                    return
                c.execute(
                    "INSERT INTO open_loops(conversation_id, topic, salience, ts, resolved) "
                    "VALUES(?,?,?,?,0)",
                    (cid, tp, float(salience or 0.0), float(ts or time.time())),
                )
                # 上限裁剪：只保留最近的 N 条未收尾
                c.execute(
                    "DELETE FROM open_loops WHERE conversation_id=? AND resolved=0 AND id NOT IN "
                    "(SELECT id FROM open_loops WHERE conversation_id=? AND resolved=0 "
                    "ORDER BY id DESC LIMIT ?)",
                    (cid, cid, _MAX_OPEN_LOOPS),
                )
        except Exception:
            logger.debug("[deep_persona_store] add_open_loop failed", exc_info=True)

    def get_open_loops(self, conversation_id: str) -> List[Dict[str, Any]]:
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        try:
            with self._lock, self._conn() as c:
                rows = c.execute(
                    "SELECT topic, salience, ts FROM open_loops "
                    "WHERE conversation_id=? AND resolved=0 ORDER BY id DESC",
                    (cid,),
                ).fetchall()
                return [
                    {"topic": r["topic"], "salience": r["salience"], "ts": r["ts"]}
                    for r in rows
                ]
        except Exception:
            return []

    def resolve_open_loop(self, conversation_id: str, topic: str) -> None:
        cid = str(conversation_id or "").strip()
        tp = str(topic or "").strip()
        if not cid or not tp:
            return
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "UPDATE open_loops SET resolved=1 WHERE conversation_id=? AND topic=?",
                    (cid, tp),
                )
        except Exception:
            logger.debug("[deep_persona_store] resolve_open_loop failed", exc_info=True)

    # ── experiential（append + 取）─────────────────────────────────────
    def add_experiential(
        self, conversation_id: str, what: str, *, emotion: str = "",
        salience: float = 0.0, ts: Optional[float] = None,
        emb: Optional[List[float]] = None,
    ) -> None:
        cid = str(conversation_id or "").strip()
        w = str(what or "").strip()
        if not cid or not w:
            return
        _emb_json = ""
        if emb:
            try:
                _emb_json = json.dumps([float(x) for x in emb])
            except Exception:
                _emb_json = ""
        try:
            with self._lock, self._conn() as c:
                exists = c.execute(
                    "SELECT 1 FROM experiential WHERE conversation_id=? AND what=?",
                    (cid, w),
                ).fetchone()
                if exists:
                    return
                c.execute(
                    "INSERT INTO experiential(conversation_id, what, emotion, salience, ts, emb) "
                    "VALUES(?,?,?,?,?,?)",
                    (cid, w, str(emotion or ""), float(salience or 0.0),
                     float(ts or time.time()), _emb_json),
                )
                c.execute(
                    "DELETE FROM experiential WHERE conversation_id=? AND id NOT IN "
                    "(SELECT id FROM experiential WHERE conversation_id=? "
                    "ORDER BY salience DESC, id DESC LIMIT ?)",
                    (cid, cid, _MAX_EXPERIENTIAL),
                )
        except Exception:
            logger.debug("[deep_persona_store] add_experiential failed", exc_info=True)

    def set_experiential_embedding(
        self, conversation_id: str, what: str, emb: List[float]
    ) -> None:
        """E1：为已存在的经历回填向量（语义召回缓存）。"""
        cid = str(conversation_id or "").strip()
        w = str(what or "").strip()
        if not cid or not w or not emb:
            return
        try:
            _j = json.dumps([float(x) for x in emb])
        except Exception:
            return
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "UPDATE experiential SET emb=? WHERE conversation_id=? AND what=?",
                    (_j, cid, w),
                )
        except Exception:
            logger.debug("[deep_persona_store] set_exp_emb failed", exc_info=True)

    def get_experiential(self, conversation_id: str) -> List[Dict[str, Any]]:
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        try:
            with self._lock, self._conn() as c:
                rows = c.execute(
                    "SELECT what, emotion, salience, ts, emb FROM experiential "
                    "WHERE conversation_id=? ORDER BY salience DESC, id DESC",
                    (cid,),
                ).fetchall()
                out = []
                for r in rows:
                    _emb = []
                    try:
                        _raw = r["emb"] if "emb" in r.keys() else ""
                        if _raw:
                            _emb = json.loads(_raw)
                    except Exception:
                        _emb = []
                    out.append({"what": r["what"], "emotion": r["emotion"],
                                "salience": r["salience"], "ts": r["ts"], "emb": _emb})
                return out
        except Exception:
            return []


    # ── life_shares（D2 反打扰节奏）─────────────────────────────────────
    def record_life_share(self, conversation_id: str, *, ts: Optional[float] = None) -> None:
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO life_shares(conversation_id, ts) VALUES(?,?)",
                    (cid, float(ts or time.time())),
                )
                # 只保留最近 10 条
                c.execute(
                    "DELETE FROM life_shares WHERE conversation_id=? AND id NOT IN "
                    "(SELECT id FROM life_shares WHERE conversation_id=? ORDER BY id DESC LIMIT 10)",
                    (cid, cid),
                )
        except Exception:
            logger.debug("[deep_persona_store] record_life_share failed", exc_info=True)

    def get_life_shares(self, conversation_id: str) -> List[float]:
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        try:
            with self._lock, self._conn() as c:
                rows = c.execute(
                    "SELECT ts FROM life_shares WHERE conversation_id=? ORDER BY id DESC LIMIT 10",
                    (cid,),
                ).fetchall()
                return [float(r["ts"]) for r in rows]
        except Exception:
            return []


_SINGLETON: Optional[DeepPersonaStore] = None
_LOCK = threading.Lock()


def get_deep_persona_store(db_path: str | Path | None = None) -> Optional[DeepPersonaStore]:
    """进程级单例（首次传 db_path 定型）。未定型且未传 → None（调用方优雅跳过）。"""
    global _SINGLETON
    if _SINGLETON is None:
        if db_path is None:
            return None
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = DeepPersonaStore(db_path)
    return _SINGLETON


def reset_deep_persona_store() -> None:
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None


__all__ = ["DeepPersonaStore", "get_deep_persona_store", "reset_deep_persona_store"]
