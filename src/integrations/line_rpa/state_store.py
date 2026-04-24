"""LINE RPA 按会话状态 + 近期运行历史（SQLite）。

替代早期的单文件 `line_rpa_state.json`：
- 支持多会话 per-chat 去重，避免不同会话切换互相污染
- 记录最近 N 次 run_once 结果，供 Web 卡片「会话流」展示
- 记录设备/LINE 版本缓存，加速状态卡渲染
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

_DDL = """
CREATE TABLE IF NOT EXISTS line_rpa_chat_state (
    chat_key            TEXT PRIMARY KEY,
    last_peer_text      TEXT DEFAULT '',
    last_peer_hash      TEXT DEFAULT '',
    last_reply          TEXT DEFAULT '',
    last_screen_sha256  TEXT DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS line_rpa_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    chat_key        TEXT DEFAULT '',
    ok              INTEGER NOT NULL,
    step            TEXT DEFAULT '',
    peer_text       TEXT DEFAULT '',
    reply_text      TEXT DEFAULT '',
    reader_path     TEXT DEFAULT '',
    total_ms        REAL DEFAULT 0,
    error           TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_line_runs_ts ON line_rpa_runs(ts DESC);

CREATE TABLE IF NOT EXISTS line_rpa_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- P4-3：Human-in-the-Loop 审核队列
CREATE TABLE IF NOT EXISTS line_rpa_pending (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    chat_key      TEXT NOT NULL,
    chat_name     TEXT DEFAULT '',
    peer_text     TEXT DEFAULT '',
    draft_reply   TEXT NOT NULL,
    final_reply   TEXT DEFAULT '',
    status        TEXT NOT NULL,   -- pending | approved | rejected | sent | cancelled | error
    resolved_at   REAL DEFAULT 0,
    resolved_by   TEXT DEFAULT '',
    send_attempts INTEGER DEFAULT 0,
    last_error    TEXT DEFAULT '',
    peer_hash     TEXT DEFAULT ''  -- P5-1：入队时对 peer_text 的 sha256 前 16 位，用于防陈旧
);

-- P5-5：Web 审计日志（人工对 pending / alert 的处理记录）
CREATE TABLE IF NOT EXISTS line_rpa_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    actor        TEXT DEFAULT '',     -- 当前登录用户/脚本来源
    action       TEXT NOT NULL,       -- approve | reject | edit | cancel | ack | ack_all | auto_cancel
    target_type  TEXT NOT NULL,       -- pending | alert
    target_id    INTEGER NOT NULL,
    before_status TEXT DEFAULT '',
    after_status  TEXT DEFAULT '',
    note         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_line_rpa_audit_ts ON line_rpa_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pending_status_ts ON line_rpa_pending(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pending_chat_key   ON line_rpa_pending(chat_key);

-- P4-5：告警闭环
CREATE TABLE IF NOT EXISTS line_rpa_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    kind            TEXT NOT NULL,           -- possibly_missed | send_fail_streak | ...
    severity        TEXT NOT NULL,           -- info | warn | error
    message         TEXT DEFAULT '',
    detail_json     TEXT DEFAULT '{}',
    acknowledged_at REAL DEFAULT 0,
    acknowledged_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_ack_ts ON line_rpa_alerts(acknowledged_at, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_kind   ON line_rpa_alerts(kind);
"""


class LineRpaStateStore:
    """线程安全的 SQLite 封装，供同步 runner 与 Web 路由共用。"""

    def __init__(self, db_path: Path, *, max_runs_kept: int = 500) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._max_runs = max(50, int(max_runs_kept))
        with self._lock:
            self._conn.executescript(_DDL)
            self._commit_migrations()
            self._conn.commit()

    def _commit_migrations(self) -> None:
        """对老库幂等追加列（SQLite ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS）。"""
        try:
            cols = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(line_rpa_runs)"
                ).fetchall()
            }
        except Exception:
            cols = set()
        if "screenshot_path" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE line_rpa_runs ADD COLUMN screenshot_path TEXT DEFAULT ''"
                )
            except Exception as e:
                logger.debug("ALTER screenshot_path 跳过: %s", e)

        # P5-1：pending 表追加 peer_hash（防陈旧校验）
        try:
            pcols = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(line_rpa_pending)"
                ).fetchall()
            }
        except Exception:
            pcols = set()
        if pcols and "peer_hash" not in pcols:
            try:
                self._conn.execute(
                    "ALTER TABLE line_rpa_pending ADD COLUMN peer_hash TEXT DEFAULT ''"
                )
            except Exception as e:
                logger.debug("ALTER peer_hash 跳过: %s", e)

    # ── 会话状态 ─────────────────────────────────────────

    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM line_rpa_chat_state WHERE chat_key=?",
                (chat_key,),
            ).fetchone()
        if not row:
            return {}
        return dict(row)

    def update_chat_state(
        self,
        chat_key: str,
        *,
        last_peer_text: Optional[str] = None,
        last_peer_hash: Optional[str] = None,
        last_reply: Optional[str] = None,
        last_screen_sha256: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_key FROM line_rpa_chat_state WHERE chat_key=?",
                (chat_key,),
            ).fetchone()
            if row:
                sets = ["updated_at=?"]
                vals: List[Any] = [now]
                if last_peer_text is not None:
                    sets.append("last_peer_text=?")
                    vals.append(last_peer_text[:4000])
                if last_peer_hash is not None:
                    sets.append("last_peer_hash=?")
                    vals.append(last_peer_hash[:64])
                if last_reply is not None:
                    sets.append("last_reply=?")
                    vals.append(last_reply[:4000])
                if last_screen_sha256 is not None:
                    sets.append("last_screen_sha256=?")
                    vals.append(last_screen_sha256[:64])
                vals.append(chat_key)
                self._conn.execute(
                    f"UPDATE line_rpa_chat_state SET {', '.join(sets)} WHERE chat_key=?",
                    vals,
                )
            else:
                self._conn.execute(
                    "INSERT INTO line_rpa_chat_state"
                    "(chat_key,last_peer_text,last_peer_hash,last_reply,"
                    " last_screen_sha256,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        chat_key,
                        (last_peer_text or "")[:4000],
                        (last_peer_hash or "")[:64],
                        (last_reply or "")[:4000],
                        (last_screen_sha256 or "")[:64],
                        now,
                    ),
                )
            self._conn.commit()

    def list_chats(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM line_rpa_chat_state ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 运行历史 ─────────────────────────────────────────

    def record_run(
        self,
        *,
        chat_key: str,
        ok: bool,
        step: str,
        peer_text: Optional[str],
        reply_text: Optional[str],
        reader_path: str,
        total_ms: float,
        error: Optional[str] = None,
        screenshot_path: Optional[str] = None,
    ) -> None:
        # 节流：仅当 peer_text 非空或有明确 error/screenshot 时才持久化，避免空转淹没
        if not (
            peer_text or error or reply_text or screenshot_path
            or step not in (
                "screen_unchanged_skipped", "no_peer_text", "duplicate_peer_skipped",
            )
        ):
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO line_rpa_runs"
                "(ts,chat_key,ok,step,peer_text,reply_text,reader_path,"
                " total_ms,error,screenshot_path)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    (chat_key or "")[:120],
                    1 if ok else 0,
                    (step or "")[:80],
                    (peer_text or "")[:1000],
                    (reply_text or "")[:2000],
                    (reader_path or "")[:160],
                    float(max(0.0, total_ms)),
                    (error or "")[:500],
                    (screenshot_path or "")[:200],
                ),
            )
            cnt_row = self._conn.execute(
                "SELECT COUNT(1) AS n FROM line_rpa_runs"
            ).fetchone()
            cnt = int(cnt_row["n"]) if cnt_row else 0
            if cnt > self._max_runs:
                self._conn.execute(
                    "DELETE FROM line_rpa_runs WHERE id IN ("
                    " SELECT id FROM line_rpa_runs ORDER BY ts ASC LIMIT ?"
                    ")",
                    (cnt - self._max_runs,),
                )
            self._conn.commit()

    def recent_runs(self, limit: int = 50, *, only_with_peer: bool = False) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit), 500))
        with self._lock:
            if only_with_peer:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_runs WHERE peer_text<>'' "
                    "ORDER BY ts DESC LIMIT ?",
                    (lim,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_runs ORDER BY ts DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [dict(r) for r in rows]

    def timeline(self, *, minutes: int = 60, limit: int = 200) -> List[Dict[str, Any]]:
        """P5-3：合并 runs + pending 事件 + alerts 的时间轴。按 ts 倒序。"""
        since = time.time() - max(1, int(minutes)) * 60.0
        lim = max(1, min(int(limit), 1000))
        out: List[Dict[str, Any]] = []
        with self._lock:
            # runs
            for r in self._conn.execute(
                "SELECT ts, step, ok, peer_text, reply_text, total_ms "
                "FROM line_rpa_runs WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (since, lim),
            ).fetchall():
                step = r["step"] or ""
                out.append({
                    "type": "run",
                    "ts": float(r["ts"]),
                    "kind": step,
                    "ok": bool(r["ok"]),
                    "label": step,
                    "detail": (r["reply_text"] or r["peer_text"] or "")[:120],
                    "ms": int(r["total_ms"] or 0),
                })
            # pending：用 ts + resolved_at 生成两条事件（入队、结案）
            for r in self._conn.execute(
                "SELECT id, ts, chat_name, status, resolved_at, resolved_by, last_error "
                "FROM line_rpa_pending "
                "WHERE ts>=? OR resolved_at>=? ORDER BY ts DESC LIMIT ?",
                (since, since, lim),
            ).fetchall():
                if float(r["ts"] or 0) >= since:
                    out.append({
                        "type": "pending",
                        "ts": float(r["ts"]),
                        "kind": "pending_created",
                        "label": f"pending#{r['id']} {r['chat_name']}",
                        "detail": "草稿入队待审",
                    })
                ra = float(r["resolved_at"] or 0)
                if ra >= since and str(r["status"]) not in ("pending",):
                    out.append({
                        "type": "pending",
                        "ts": ra,
                        "kind": f"pending_{r['status']}",
                        "label": f"pending#{r['id']} {r['chat_name']}",
                        "detail": f"by {r['resolved_by'] or '-'}"
                                  + (f" · {r['last_error']}" if r['last_error'] else ""),
                    })
            # alerts
            for r in self._conn.execute(
                "SELECT id, ts, kind, severity, message, acknowledged_at "
                "FROM line_rpa_alerts WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (since, lim),
            ).fetchall():
                out.append({
                    "type": "alert",
                    "ts": float(r["ts"]),
                    "kind": r["kind"],
                    "severity": r["severity"],
                    "label": r["kind"],
                    "detail": r["message"],
                    "ack": bool(r["acknowledged_at"]),
                })
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:lim]

    def run_stats(self, window_hours: float = 24.0) -> Dict[str, Any]:
        since = time.time() - max(0.1, float(window_hours)) * 3600.0
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(1) AS n FROM line_rpa_runs WHERE ts>=?",
                (since,),
            ).fetchone()
            ok_cnt = self._conn.execute(
                "SELECT COUNT(1) AS n FROM line_rpa_runs WHERE ts>=? AND ok=1",
                (since,),
            ).fetchone()
            sent = self._conn.execute(
                "SELECT COUNT(1) AS n FROM line_rpa_runs "
                "WHERE ts>=? AND step='sent'",
                (since,),
            ).fetchone()
            avg_ms = self._conn.execute(
                "SELECT AVG(total_ms) AS v FROM line_rpa_runs "
                "WHERE ts>=? AND step='sent'",
                (since,),
            ).fetchone()
            top_steps = self._conn.execute(
                "SELECT step, COUNT(1) AS n FROM line_rpa_runs "
                "WHERE ts>=? GROUP BY step ORDER BY n DESC LIMIT 8",
                (since,),
            ).fetchall()
        n_total = int(total["n"]) if total else 0
        n_ok = int(ok_cnt["n"]) if ok_cnt else 0
        n_sent = int(sent["n"]) if sent else 0
        return {
            "window_hours": float(window_hours),
            "total": n_total,
            "ok": n_ok,
            "ok_rate": round(n_ok * 100.0 / n_total, 1) if n_total else 0.0,
            "sent": n_sent,
            "avg_send_ms": round(float(avg_ms["v"] or 0.0), 1),
            "steps": [{"step": r["step"], "count": int(r["n"])} for r in top_steps],
        }

    # ── 元数据（设备 serial / LINE 版本 / pause_until 等）─
    def set_meta(self, key: str, value: Any) -> None:
        s = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO line_rpa_meta(k,v) VALUES(?,?)",
                (key, s),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM line_rpa_meta WHERE k=?", (key,)
            ).fetchone()
        if not row:
            return default
        v = row["v"]
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return v

    # ── P4-3：Human-in-the-Loop 审核队列 ─────────────────

    @staticmethod
    def compute_peer_hash(peer_text: Optional[str]) -> str:
        """P5-1：稳定计算 peer_text 哈希（用于防陈旧校验）。空文本返回空串。"""
        import hashlib
        s = (peer_text or "").strip()
        if not s:
            return ""
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

    def insert_pending(
        self,
        *,
        chat_key: str,
        chat_name: str,
        peer_text: str,
        draft_reply: str,
        peer_hash: Optional[str] = None,
    ) -> int:
        if peer_hash is None:
            peer_hash = self.compute_peer_hash(peer_text)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO line_rpa_pending(ts, chat_key, chat_name, peer_text, "
                "draft_reply, final_reply, status, peer_hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    str(chat_key)[:200],
                    str(chat_name)[:120],
                    str(peer_text or "")[:2000],
                    str(draft_reply or "")[:4000],
                    str(draft_reply or "")[:4000],
                    "pending",
                    str(peer_hash)[:40],
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def sweep_stale_pending(
        self,
        *,
        ttl_sec: float,
        reason: str = "ttl_expired",
    ) -> List[int]:
        """P5-1：把 ts 早于 ttl 的 pending/approved 行自动取消。返回被取消的 id 列表。"""
        if ttl_sec <= 0:
            return []
        now = time.time()
        cutoff = now - float(ttl_sec)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM line_rpa_pending "
                "WHERE status IN ('pending','approved') AND ts<?",
                (cutoff,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                # 逐条更新以写入 last_error
                for pid in ids:
                    self._conn.execute(
                        "UPDATE line_rpa_pending SET status='cancelled', "
                        "last_error=?, resolved_at=?, resolved_by='auto:ttl' "
                        "WHERE id=?",
                        (str(reason)[:60], now, pid),
                    )
                self._conn.commit()
        if ids:
            for pid in ids:
                try:
                    self.insert_audit(
                        actor="auto:ttl", action="auto_cancel",
                        target_type="pending", target_id=pid,
                        before_status="pending", after_status="cancelled",
                        note=str(reason)[:120],
                    )
                except Exception:
                    pass
        return ids

    def cancel_pending_with_reason(
        self,
        pending_id: int,
        *,
        reason: str,
        by: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        """P5-1：带原因的强制取消（用于 stale_peer 等）。"""
        now = time.time()
        before_status = ""
        changed = 0
        with self._lock:
            pre = self._conn.execute(
                "SELECT status FROM line_rpa_pending WHERE id=?",
                (int(pending_id),),
            ).fetchone()
            before_status = str(pre["status"]) if pre else ""
            cur = self._conn.execute(
                "UPDATE line_rpa_pending SET status='cancelled', "
                "last_error=?, resolved_at=?, resolved_by=? WHERE id=? "
                "AND status IN ('pending','approved')",
                (str(reason)[:60], now, str(by)[:60], int(pending_id)),
            )
            changed = int(cur.rowcount or 0)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM line_rpa_pending WHERE id=?",
                (int(pending_id),),
            ).fetchone()
        if changed:
            try:
                self.insert_audit(
                    actor=by, action="auto_cancel", target_type="pending",
                    target_id=int(pending_id),
                    before_status=before_status, after_status="cancelled",
                    note=str(reason)[:120],
                )
            except Exception:
                pass
        return dict(row) if row else None

    def list_pending(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_pending WHERE status=? "
                    "ORDER BY id DESC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_pending "
                    "ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_pending(self, pending_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM line_rpa_pending WHERE id=?",
                (int(pending_id),),
            ).fetchone()
        return dict(row) if row else None

    def resolve_pending(
        self,
        pending_id: int,
        *,
        action: str,
        final_reply: Optional[str] = None,
        by: str = "",
    ) -> Optional[Dict[str, Any]]:
        """action ∈ {approve, reject, edit_approve, cancel}. 返回更新后的行或 None。"""
        a = (action or "").strip().lower()
        if a == "edit_approve":
            target_status = "approved"
        elif a == "approve":
            target_status = "approved"
        elif a == "reject":
            target_status = "rejected"
        elif a == "cancel":
            target_status = "cancelled"
        else:
            return None
        now = time.time()
        before_status = ""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM line_rpa_pending WHERE id=?",
                (int(pending_id),),
            ).fetchone()
            if not row:
                return None
            before_status = str(row["status"])
            if before_status not in {"pending", "rejected"}:
                # 已 sent/approved 的不再允许改
                return dict(row)
            text = (
                str(final_reply)[:4000]
                if final_reply is not None
                else str(row["draft_reply"] or "")[:4000]
            )
            self._conn.execute(
                "UPDATE line_rpa_pending SET status=?, final_reply=?, "
                "resolved_at=?, resolved_by=? WHERE id=?",
                (target_status, text, now, str(by)[:60], int(pending_id)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM line_rpa_pending WHERE id=?",
                (int(pending_id),),
            ).fetchone()
        # P5-5：审计
        try:
            self.insert_audit(
                actor=by, action=a, target_type="pending",
                target_id=int(pending_id),
                before_status=before_status, after_status=target_status,
                note=(("edited: " + text[:80]) if a == "edit_approve" else ""),
            )
        except Exception:
            pass
        return dict(row) if row else None

    def insert_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: int,
        before_status: str = "",
        after_status: str = "",
        note: str = "",
    ) -> int:
        """P5-5：统一审计日志写入。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO line_rpa_audit(ts, actor, action, target_type, "
                "target_id, before_status, after_status, note) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    str(actor or "")[:60],
                    str(action or "")[:40],
                    str(target_type or "")[:20],
                    int(target_id),
                    str(before_status or "")[:20],
                    str(after_status or "")[:20],
                    str(note or "")[:500],
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_audit(
        self,
        *,
        target_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """P5-5：查询审计日志。target_type 为空则全部。"""
        lim = max(1, min(int(limit), 500))
        with self._lock:
            if target_type:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_audit WHERE target_type=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (str(target_type)[:20], lim),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_audit ORDER BY ts DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        return [dict(r) for r in rows]

    def mark_pending_sent(
        self, pending_id: int, *, error: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE line_rpa_pending SET status=?, last_error=?, "
                "send_attempts=send_attempts+1 WHERE id=?",
                ("sent" if not error else "error", error[:200], int(pending_id)),
            )
            self._conn.commit()

    def pending_stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(1) AS n FROM line_rpa_pending GROUP BY status"
            ).fetchall()
        out = {"pending": 0, "approved": 0, "rejected": 0, "sent": 0, "cancelled": 0, "error": 0}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        return out

    # ── P4-5：告警闭环 ──────────────────────────────────

    def insert_alert(
        self,
        *,
        kind: str,
        severity: str = "warn",
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
        dedup_window_sec: float = 300.0,
    ) -> Optional[int]:
        """插入告警；在 dedup 窗口内 kind 相同的未 ack 告警将被合并（跳过新增）。"""
        now = time.time()
        with self._lock:
            if dedup_window_sec > 0:
                existing = self._conn.execute(
                    "SELECT id FROM line_rpa_alerts WHERE kind=? AND acknowledged_at=0 "
                    "AND ts>=? ORDER BY ts DESC LIMIT 1",
                    (kind, now - float(dedup_window_sec)),
                ).fetchone()
                if existing:
                    return None
            cur = self._conn.execute(
                "INSERT INTO line_rpa_alerts(ts, kind, severity, message, detail_json) "
                "VALUES(?,?,?,?,?)",
                (
                    now,
                    str(kind)[:40],
                    str(severity)[:16],
                    str(message)[:400],
                    json.dumps(detail or {}, ensure_ascii=False)[:2000],
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_alerts(
        self,
        *,
        only_unacked: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            if only_unacked:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_alerts WHERE acknowledged_at=0 "
                    "ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM line_rpa_alerts ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail_json") or "{}")
            except Exception:
                d["detail"] = {}
            out.append(d)
        return out

    def ack_alert(self, alert_id: int, *, by: str = "") -> Optional[Dict[str, Any]]:
        now = time.time()
        changed = 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE line_rpa_alerts SET acknowledged_at=?, acknowledged_by=? "
                "WHERE id=? AND acknowledged_at=0",
                (now, str(by)[:60], int(alert_id)),
            )
            changed = int(cur.rowcount or 0)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM line_rpa_alerts WHERE id=?", (int(alert_id),),
            ).fetchone()
        if changed:
            try:
                self.insert_audit(
                    actor=by, action="ack", target_type="alert",
                    target_id=int(alert_id),
                    before_status="unacked", after_status="acked",
                )
            except Exception:
                pass
        return dict(row) if row else None

    def ack_all_alerts(self, *, by: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE line_rpa_alerts SET acknowledged_at=?, acknowledged_by=? "
                "WHERE acknowledged_at=0",
                (now, str(by)[:60]),
            )
            self._conn.commit()
            n = int(cur.rowcount or 0)
        if n:
            try:
                self.insert_audit(
                    actor=by, action="ack_all", target_type="alert",
                    target_id=0, after_status="acked",
                    note=f"count={n}",
                )
            except Exception:
                pass
        return n

    def alerts_count_unacked(self, *, kind: Optional[str] = None) -> int:
        with self._lock:
            if kind:
                row = self._conn.execute(
                    "SELECT COUNT(1) AS n FROM line_rpa_alerts "
                    "WHERE acknowledged_at=0 AND kind=?",
                    (str(kind)[:40],),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(1) AS n FROM line_rpa_alerts WHERE acknowledged_at=0"
                ).fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass


def default_state_db_path(config_path: Path) -> Path:
    return Path(config_path).parent / "line_rpa_state.db"


# ── 兼容层：从旧 JSON 迁移（仅读一次，迁入 chat_key='default'）──

def migrate_from_legacy_json(db: LineRpaStateStore, legacy_json: Path) -> bool:
    try:
        if not legacy_json.exists():
            return False
        raw = legacy_json.read_text(encoding="utf-8")
        if not raw.strip():
            return False
        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            return False
        # 迁移标记：已迁移则跳过
        if db.get_meta("legacy_json_migrated"):
            return False
        db.update_chat_state(
            "line_rpa:default",
            last_peer_text=str(data.get("last_peer_text") or ""),
            last_reply=str(data.get("last_reply") or ""),
            last_screen_sha256=str(data.get("last_screen_crop_sha256") or ""),
        )
        db.set_meta("legacy_json_migrated", {"at": time.time(), "from": str(legacy_json)})
        logger.info("line_rpa 状态已从 %s 迁入 SQLite", legacy_json)
        return True
    except Exception as e:
        logger.debug("迁移 legacy json 失败: %s", e)
        return False
