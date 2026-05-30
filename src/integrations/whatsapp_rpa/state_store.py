"""WhatsApp RPA 状态存储（SQLite）。

与 LINE RPA state_store 结构对等，表前缀改为 wa_rpa_。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..rpa_shared import (
    compute_intent_tag as _compute_intent_tag,
    sessions_from_rows as _sessions_from_rows,
    compute_intent_stats as _compute_intent_stats,
    count_runs_for_chat_name as _count_runs_for_chat_name,
)

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS wa_rpa_chat_state (
    chat_key            TEXT PRIMARY KEY,
    last_peer_text      TEXT DEFAULT '',
    last_peer_hash      TEXT DEFAULT '',
    last_reply          TEXT DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wa_rpa_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    chat_key        TEXT DEFAULT '',
    ok              INTEGER NOT NULL,
    step            TEXT DEFAULT '',
    peer_text       TEXT DEFAULT '',
    reply_text      TEXT DEFAULT '',
    total_ms        REAL DEFAULT 0,
    error           TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wa_runs_ts ON wa_rpa_runs(ts DESC);

CREATE TABLE IF NOT EXISTS wa_rpa_pending (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    chat_key        TEXT NOT NULL,
    peer_name       TEXT DEFAULT '',
    peer_text       TEXT NOT NULL,
    proposed_reply  TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    resolved_at     REAL,
    resolved_by     TEXT DEFAULT '',
    error           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wa_pending_status ON wa_rpa_pending(status, ts DESC);

CREATE TABLE IF NOT EXISTS wa_rpa_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT DEFAULT 'warn',
    message         TEXT DEFAULT '',
    detail          TEXT DEFAULT '{}',
    acked           INTEGER DEFAULT 0,
    acked_at        REAL,
    acked_by        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wa_alerts_acked ON wa_rpa_alerts(acked, ts DESC);

CREATE TABLE IF NOT EXISTS wa_rpa_send_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    chat_key    TEXT NOT NULL,
    peer_name   TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    sent_at     REAL DEFAULT NULL,
    error       TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_wa_send_q_status ON wa_rpa_send_queue(status, ts);

CREATE TABLE IF NOT EXISTS wa_rpa_timeline (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT DEFAULT '',
    detail  TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_wa_timeline_ts ON wa_rpa_timeline(ts DESC);
"""

_MIGRATIONS: List[str] = [
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN last_peer_ts    REAL DEFAULT NULL",
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN last_reply_ts   REAL DEFAULT NULL",
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN intimacy_score  REAL DEFAULT NULL",
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN quiet_until     REAL DEFAULT NULL",
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN blacklist       INTEGER DEFAULT 0",
    "ALTER TABLE wa_rpa_runs ADD COLUMN intent_tag TEXT DEFAULT ''",
    # P8-3: 复合索引加速 chat_history / sessions_for_chat / search_history
    "CREATE INDEX IF NOT EXISTS idx_wa_runs_ck_ts ON wa_rpa_runs(chat_key, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_wa_runs_ts ON wa_rpa_runs(ts DESC)",
    # 多语言 TTS：对话级语言缓存，短消息保护
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN detected_lang TEXT DEFAULT NULL",
    # P4-A: 运营手动锁定对话语言，覆盖自动检测
    "ALTER TABLE wa_rpa_chat_state ADD COLUMN forced_lang  TEXT DEFAULT NULL",
    # P13-B: TTS 预览路径（approval mode pending 行）
    "ALTER TABLE wa_rpa_pending ADD COLUMN tts_path TEXT DEFAULT ''",
]


def default_state_db_path(config_path: str) -> Path:
    return Path(config_path).parent / "wa_rpa_state.db"


class WaRpaStateStore:
    """线程安全的 WhatsApp RPA 状态存储。"""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._apply_ddl()
        self._recover_stuck_send_queue()

    def _apply_ddl(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)
            for sql in _MIGRATIONS:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    def _recover_stuck_send_queue(self) -> None:
        """启动时：① 重置滞留 processing → queued；② 清理 >7d 已完成记录。
        wa_rpa_runs（客户聊天记录）永远不删除。"""
        cutoff = time.time() - 7 * 86400
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE wa_rpa_send_queue SET status='queued', error='recovered_on_startup'"
                    " WHERE status='processing'"
                )
                self._conn.execute(
                    "DELETE FROM wa_rpa_send_queue"
                    " WHERE status IN ('sent','failed','cancelled') AND ts < ?",
                    (cutoff,),
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # 表尚未创建（首次启动），忽略

    # ── 聊天状态 ──────────────────────────────────────────────────────────

    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wa_rpa_chat_state WHERE chat_key=?", (chat_key,)
            ).fetchone()
        return dict(row) if row else {}

    def upsert_chat_state(self, chat_key: str, **kwargs) -> None:
        _ALLOWED = {
            "last_peer_text", "last_peer_hash", "last_reply",
            "last_peer_ts", "last_reply_ts", "intimacy_score",
            "detected_lang", "forced_lang", "quiet_until", "blacklist",
            "last_proactive_template",  # P15-g: 记录最后一次主动续聊模板
        }
        fields = {k: v for k, v in kwargs.items() if k in _ALLOWED}
        fields["updated_at"] = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM wa_rpa_chat_state WHERE chat_key=?", (chat_key,)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(
                    f"UPDATE wa_rpa_chat_state SET {sets} WHERE chat_key=?",
                    [*fields.values(), chat_key],
                )
            else:
                fields["chat_key"] = chat_key
                cols = ", ".join(fields.keys())
                phs = ", ".join("?" * len(fields))
                self._conn.execute(
                    f"INSERT INTO wa_rpa_chat_state ({cols}) VALUES ({phs})",
                    list(fields.values()),
                )
            self._conn.commit()

    # ── 手动发送队列 ──────────────────────────────────────────────────────

    def enqueue_send(self, chat_key: str, peer_name: str, text: str) -> int:
        """插入一条待主动发送任务，返回新行 id。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO wa_rpa_send_queue (ts,chat_key,peer_name,text,status) VALUES (?,?,?,?,'queued')",
                (time.time(), chat_key, peer_name, text),
            )
            self._conn.commit()
            return cur.lastrowid

    def has_pending_send(self) -> bool:
        """检查是否有待发送的队列任务（不消费）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM wa_rpa_send_queue WHERE status='queued' LIMIT 1"
            ).fetchone()
            return row is not None

    def pop_send_queue_item(self) -> Optional[Dict[str, Any]]:
        """取出最早一条 queued 任务并将其标记为 processing，返回 dict 或 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wa_rpa_send_queue WHERE status='queued' ORDER BY ts LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE wa_rpa_send_queue SET status='processing' WHERE id=?", (row["id"],)
            )
            self._conn.commit()
            return dict(row)

    def mark_send_queue_item(
        self, item_id: int, status: str, error: Optional[str] = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE wa_rpa_send_queue SET status=?, sent_at=?, error=? WHERE id=?",
                (status, time.time(), error, item_id),
            )
            self._conn.commit()

    def list_send_queue(
        self, limit: int = 30, include_done: bool = False
    ) -> List[Dict[str, Any]]:
        clause = "" if include_done else "WHERE status NOT IN ('sent','failed')"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM wa_rpa_send_queue {clause} ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_send_queue_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """P15-C: 单条 send-queue 查询（替代 P14-C 拉 50 条找一条）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wa_rpa_send_queue WHERE id=?", (int(item_id),),
            ).fetchone()
        return dict(row) if row else None

    # ── 运行记录 ──────────────────────────────────────────────────────────

    def insert_run(self, **kwargs) -> int:
        allowed = {"chat_key", "ok", "step", "peer_text", "reply_text",
                   "total_ms", "error", "screenshot_path", "intent_tag"}
        row = {k: v for k, v in kwargs.items() if k in allowed}
        # P6-A: 自动计算意图标签（若调用方未提供）
        if "intent_tag" not in row and row.get("peer_text"):
            row["intent_tag"] = _compute_intent_tag(row["peer_text"])
        row.setdefault("ts", time.time())
        row.setdefault("ok", 0)
        cols = ", ".join(row.keys())
        phs = ", ".join("?" * len(row))
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO wa_rpa_runs ({cols}) VALUES ({phs})",
                list(row.values()),
            )
            self._conn.commit()
            return cur.lastrowid

    def recent_runs(self, limit: int = 50, only_with_peer: bool = False) -> List[Dict]:
        q = "SELECT * FROM wa_rpa_runs"
        if only_with_peer:
            q += " WHERE peer_text != ''"
        q += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def run_stats(self, window_hours: float = 24.0) -> Dict[str, Any]:
        since = time.time() - window_hours * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as total, SUM(ok) as sent, "
                "AVG(CASE WHEN ok THEN total_ms END) as avg_ms "
                "FROM wa_rpa_runs WHERE ts >= ?",
                (since,),
            ).fetchone()
        if not row:
            return {"total": 0, "sent": 0, "avg_ms": 0}
        return {
            "total": row["total"] or 0,
            "sent": int(row["sent"] or 0),
            "avg_ms": round(row["avg_ms"] or 0, 1),
        }

    def conversation_stats(self, window_hours: float = 24.0) -> Dict[str, Any]:
        """统计真实对话（有 peer_text 的 run），过滤掉纯轮询空转记录。"""
        since = time.time() - window_hours * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as conv_total, SUM(ok) as conv_sent, "
                "COUNT(DISTINCT chat_key) as unique_chats, "
                "AVG(CASE WHEN ok AND peer_text != '' THEN total_ms END) as conv_avg_ms "
                "FROM wa_rpa_runs WHERE ts >= ? AND peer_text != ''",
                (since,),
            ).fetchone()
        if not row:
            return {"conv_total": 0, "conv_sent": 0, "unique_chats": 0, "conv_avg_ms": 0}
        return {
            "conv_total": row["conv_total"] or 0,
            "conv_sent": int(row["conv_sent"] or 0),
            "unique_chats": row["unique_chats"] or 0,
            "conv_avg_ms": round(row["conv_avg_ms"] or 0, 1),
        }

    def recent_conversations(self, limit: int = 30, hours: float = 48.0) -> List[Dict]:
        """按 chat_key 聚合，每个联系人返回一行（最近有 peer_text 的那次 run）+统计。
        仅返回在 hours 时间窗内有真实消息的对话，信噪比 100%。"""
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.chat_key, r.peer_text, r.reply_text, r.ts, r.ok,
                       r.step, r.total_ms, r.error, r.screenshot_path,
                       c.total_turns, c.ok_turns, c.last_ts, c.last_peer_ts,
                       s.last_peer_text  AS state_last_peer,
                       s.last_reply      AS state_last_reply,
                       s.last_peer_ts    AS state_last_peer_ts,
                       s.last_reply_ts   AS state_last_reply_ts,
                       s.intimacy_score  AS state_intimacy,
                       s.detected_lang   AS state_detected_lang,
                       s.forced_lang     AS state_forced_lang,
                       s.quiet_until     AS state_quiet_until,
                       s.blacklist       AS state_blacklist,
                       s.updated_at      AS state_updated_at
                FROM wa_rpa_runs r
                INNER JOIN (
                    SELECT chat_key,
                       COUNT(CASE WHEN peer_text != '' THEN 1 END)            AS total_turns,
                       SUM(CASE WHEN peer_text != '' AND ok THEN 1 END)       AS ok_turns,
                       MAX(ts)                                                AS last_ts,
                       MAX(CASE WHEN peer_text != '' THEN ts  END)            AS last_peer_ts,
                       MAX(CASE WHEN peer_text != '' THEN id  END)            AS last_peer_id
                    FROM wa_rpa_runs
                    WHERE ts >= ? AND chat_key != ''
                    GROUP BY chat_key
                ) c ON r.chat_key = c.chat_key AND r.id = c.last_peer_id
                LEFT JOIN wa_rpa_chat_state s ON r.chat_key = s.chat_key
                ORDER BY c.last_peer_ts DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def chat_history(
        self, chat_key: str, limit: int = 10, offset: int = 0
    ) -> List[Dict]:
        """指定联系人的消息交换（分页，按时间升序）。P6-A: 含 intent_tag。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, peer_text, reply_text, ok, step, total_ms, error, intent_tag "
                "FROM wa_rpa_runs WHERE chat_key=? AND peer_text!='' "
                "ORDER BY ts DESC LIMIT ? OFFSET ?",
                (chat_key, limit, offset),
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def sessions_for_chat(
        self, chat_key: str, gap_sec: float = 14400
    ) -> List[Dict[str, Any]]:
        """P6-A/P7-C: 按 4h 间隔将历史分组为会话。调用共享工具。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, peer_text, reply_text, ok, intent_tag "
                "FROM wa_rpa_runs WHERE chat_key=? AND peer_text!='' "
                "ORDER BY ts ASC",
                (chat_key,),
            ).fetchall()
        return _sessions_from_rows([dict(r) for r in rows], gap_sec=gap_sec)

    def total_turns_for_chat(self, chat_key: str) -> int:
        """P6-A: 联系人全量对话条数（用于分页框）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM wa_rpa_runs WHERE chat_key=? AND peer_text!=''",
                (chat_key,),
            ).fetchone()
        return int((row["n"] if row else 0) or 0)

    def customer_profile(self, chat_key: str) -> Dict[str, Any]:
        """P7-B: 联系人全量画像（历史统计 + 当前状态 + 意图分布）。"""
        with self._lock:
            stats = self._conn.execute(
                "SELECT COUNT(*) as total, SUM(ok) as ok_cnt,"
                " MIN(ts) as first_ts, MAX(ts) as last_ts"
                " FROM wa_rpa_runs WHERE chat_key=? AND peer_text!=''",
                (chat_key,),
            ).fetchone()
            intent_rows = self._conn.execute(
                "SELECT COALESCE(intent_tag,'general') as tag, COUNT(*) as cnt"
                " FROM wa_rpa_runs WHERE chat_key=? AND peer_text!=''"
                " GROUP BY tag ORDER BY cnt DESC",
                (chat_key,),
            ).fetchall()
            state = self._conn.execute(
                "SELECT intimacy_score, last_peer_text, last_reply, last_peer_ts"
                " FROM wa_rpa_chat_state WHERE chat_key=?",
                (chat_key,),
            ).fetchone()
        total = int((stats["total"] if stats else 0) or 0)
        ok = int((stats["ok_cnt"] if stats else 0) or 0)
        dist = {r["tag"]: int(r["cnt"]) for r in intent_rows}
        dominant = intent_rows[0]["tag"] if intent_rows else "general"
        return {
            "total_turns": total,
            "reply_rate": round(ok / total * 100, 1) if total else 0.0,
            "first_ts": float((stats["first_ts"] if stats else 0) or 0),
            "last_ts": float((stats["last_ts"] if stats else 0) or 0),
            "dominant_intent": dominant,
            "intent_distribution": dist,
            "intimacy_score": float(state["intimacy_score"] or 0)
                if state and state["intimacy_score"] is not None else None,
            "last_peer_text": (state["last_peer_text"] or "")[:200] if state else "",
        }

    def search_history(
        self, q: str, *, intent: str = "", days: int = 30, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """P7-A: 跨联系人全文检索聊天记录（peer+reply LIKE）。支持意图过滤。"""
        q = (q or "").strip()
        if not q:
            return []
        since = time.time() - max(1, int(days)) * 86400
        pct = f"%{q}%"
        params: list = [pct, pct, since]
        intent_clause = ""
        if intent:
            intent_clause = "AND COALESCE(intent_tag,'general')=?"
            params.append(intent)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, ts, chat_key, peer_text, reply_text, ok, intent_tag"
                f" FROM wa_rpa_runs"
                f" WHERE (peer_text LIKE ? OR reply_text LIKE ?)"
                f" AND ts >= ? AND peer_text != '' {intent_clause}"
                f" ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def intent_stats(self, window_hours: float = 168.0) -> Dict[str, Any]:
        """P7-D / P10-C: 意图分布统计（委托给 rpa_shared.compute_intent_stats）。"""
        with self._lock:
            return _compute_intent_stats(
                self._conn, "wa_rpa_runs", window_hours=window_hours
            )

    def match_chat_name(self, name: str) -> Dict[str, Any]:
        """P12-A: 跨平台身份匹配 — 按 chat_key 后缀查 chat_name 的轮次/最后时间。"""
        with self._lock:
            return _count_runs_for_chat_name(self._conn, "wa_rpa_runs", name)

    # ── 待审批队列 ─────────────────────────────────────────────────────────

    def insert_pending(self, chat_key: str, peer_text: str,
                       proposed_reply: str = "", peer_name: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO wa_rpa_pending "
                "(ts, chat_key, peer_name, peer_text, proposed_reply, status) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), chat_key, peer_name, peer_text, proposed_reply, "pending"),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_pending(self, status: Optional[str] = None, limit: int = 50) -> List[Dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM wa_rpa_pending WHERE status=? ORDER BY ts DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM wa_rpa_pending ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_pending(self, pending_id: int, action: str,
                        by: str = "") -> Optional[Dict]:
        allowed = {"approve", "reject", "send"}
        if action not in allowed:
            return None
        status = "approved" if action in {"approve", "send"} else "rejected"
        with self._lock:
            self._conn.execute(
                "UPDATE wa_rpa_pending SET status=?, resolved_at=?, resolved_by=? WHERE id=?",
                (status, time.time(), by, pending_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM wa_rpa_pending WHERE id=?", (pending_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_pending(self, pending_id: int) -> Optional[Dict]:
        """P14-A: 按 id 取单条 pending 行。"""
        row = self._conn.execute(
            "SELECT * FROM wa_rpa_pending WHERE id=?", (pending_id,)
        ).fetchone()
        return dict(row) if row else None

    def reset_pending_tts(self, pending_id: int) -> bool:
        """P14-C: 清除 TTS ERROR 哨兵，触发重新生成。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, status FROM wa_rpa_pending WHERE id=?", (pending_id,)
            ).fetchone()
            if row is None or row["status"] not in ("pending", "approved"):
                return False
            self._conn.execute(
                "UPDATE wa_rpa_pending SET tts_path='' WHERE id=?", (pending_id,)
            )
            self._conn.commit()
        return True

    def update_pending_tts_path(self, pending_id: int, tts_path: str) -> None:
        """P13-B: 回写 TTS 预览路径（或 ERROR 哨兵）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE wa_rpa_pending SET tts_path=? WHERE id=?",
                (str(tts_path or ""), pending_id),
            )
            self._conn.commit()

    def cancel_all_open_pending(self) -> list:
        """P13-D: 立即取消所有 pending 行。"""
        return self.cancel_pending_by_ttl(ttl_sec=0.001)

    def cancel_pending_by_ttl(
        self, *, ttl_sec: float, reason: str = "ttl_expired"
    ) -> list:
        """P12-A: 将超过 ttl_sec 的 pending 行状态改为 cancelled。返回被取消的 id 列表。"""
        if ttl_sec <= 0:
            return []
        cutoff = time.time() - float(ttl_sec)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM wa_rpa_pending WHERE status='pending' AND ts<?",
                (cutoff,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                for pid in ids:
                    self._conn.execute(
                        "UPDATE wa_rpa_pending SET status='cancelled', resolved_at=?, resolved_by=? WHERE id=?",
                        (time.time(), f"auto:{reason}", pid),
                    )
                self._conn.commit()
        return ids

    def pending_stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) as cnt FROM wa_rpa_pending GROUP BY status"
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── 告警 ──────────────────────────────────────────────────────────────

    def insert_alert(self, *, kind: str, severity: str = "warn",
                     message: str = "", detail: Optional[Dict] = None,
                     dedup_window_sec: float = 300.0) -> Optional[int]:
        since = time.time() - dedup_window_sec
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM wa_rpa_alerts WHERE kind=? AND ts>=? LIMIT 1",
                (kind, since),
            ).fetchone()
            if dup:
                return None
            cur = self._conn.execute(
                "INSERT INTO wa_rpa_alerts (ts, kind, severity, message, detail) "
                "VALUES (?,?,?,?,?)",
                (time.time(), kind, severity, message,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_alerts(self, only_unacked: bool = True, limit: int = 50) -> List[Dict]:
        q = "SELECT * FROM wa_rpa_alerts"
        if only_unacked:
            q += " WHERE acked=0"
        q += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def ack_alert(self, alert_id: int, by: str = "") -> Optional[Dict]:
        with self._lock:
            self._conn.execute(
                "UPDATE wa_rpa_alerts SET acked=1, acked_at=?, acked_by=? WHERE id=?",
                (time.time(), by, alert_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM wa_rpa_alerts WHERE id=?", (alert_id,)
            ).fetchone()
        return dict(row) if row else None

    def ack_all_alerts(self, by: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE wa_rpa_alerts SET acked=1, acked_at=?, acked_by=? WHERE acked=0",
                (now, by),
            )
            self._conn.commit()
            return cur.rowcount

    def alerts_count_unacked(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as n FROM wa_rpa_alerts WHERE acked=0"
            ).fetchone()
        return int(row["n"]) if row else 0

    # ── 时间轴 ────────────────────────────────────────────────────────────

    def insert_timeline(self, kind: str, summary: str,
                        detail: Optional[Dict] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO wa_rpa_timeline (ts, kind, summary, detail) VALUES (?,?,?,?)",
                (time.time(), kind, summary,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def timeline(self, minutes: int = 60, limit: int = 200) -> List[Dict]:
        since = time.time() - minutes * 60
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wa_rpa_timeline WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stop_contact_stats(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        now_ts = now_ts or time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "SUM(CASE WHEN blacklist=1 THEN 1 ELSE 0 END) AS blacklist_cnt, "
                "SUM(CASE WHEN quiet_until IS NOT NULL AND quiet_until> ? THEN 1 ELSE 0 END) AS quiet_active "
                "FROM wa_rpa_chat_state",
                (now_ts,),
            ).fetchone()
        return {
            "blacklist": int((row["blacklist_cnt"] if row else 0) or 0),
            "quiet_active": int((row["quiet_active"] if row else 0) or 0),
        }
