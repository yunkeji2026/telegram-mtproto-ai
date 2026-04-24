"""Messenger RPA 状态存储（SQLite）。

设计参考 line_rpa.state_store，但更精简：
- per-chat 去重（按 fingerprint）
- 近期 run 历史（供 Web 卡片展示）
- 不实现复杂的审批队列（reply_mode=approve 留待后续接入 line_rpa.state_store 同款表）
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS messenger_rpa_chat_state (
    chat_key            TEXT PRIMARY KEY,
    chat_name           TEXT DEFAULT '',
    last_peer_text      TEXT DEFAULT '',
    last_peer_fp        TEXT DEFAULT '',
    last_peer_kind      TEXT DEFAULT '',
    last_reply          TEXT DEFAULT '',
    last_screen_sha256  TEXT DEFAULT '',
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messenger_rpa_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    chat_key        TEXT DEFAULT '',
    chat_name       TEXT DEFAULT '',
    ok              INTEGER NOT NULL,
    step            TEXT DEFAULT '',
    peer_text       TEXT DEFAULT '',
    peer_kind       TEXT DEFAULT '',
    reply_text      TEXT DEFAULT '',
    reader_path     TEXT DEFAULT '',
    total_ms        REAL DEFAULT 0,
    error           TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messenger_runs_ts ON messenger_rpa_runs(ts DESC);

CREATE TABLE IF NOT EXISTS messenger_rpa_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- reply_mode=approve 时把候选回复写在这里，等人或下游服务批准
CREATE TABLE IF NOT EXISTS messenger_rpa_approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    chat_key        TEXT NOT NULL,
    chat_name       TEXT DEFAULT '',
    peer_text       TEXT DEFAULT '',
    peer_kind       TEXT DEFAULT '',
    reply_text      TEXT NOT NULL,
    reply_lang      TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|sent|failed
    decided_at      REAL DEFAULT 0,
    decided_by      TEXT DEFAULT '',
    decision_note   TEXT DEFAULT '',
    sent_at         REAL DEFAULT 0,
    send_error      TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    run_id          TEXT DEFAULT '',
    extra_json      TEXT DEFAULT '',
    ai_tier         TEXT DEFAULT ''   -- P6-3 分级路由打标（premium/normal/low）
);
CREATE INDEX IF NOT EXISTS idx_messenger_approvals_status_ts
    ON messenger_rpa_approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messenger_approvals_chat
    ON messenger_rpa_approvals(chat_key, created_at DESC);

-- 跳过列表：被 spam 兜底命中的 chat、或人工标记的 chat 永久跳过
CREATE TABLE IF NOT EXISTS messenger_rpa_skipped_chats (
    chat_key   TEXT PRIMARY KEY,
    chat_name  TEXT DEFAULT '',
    reason     TEXT DEFAULT '',
    created_at REAL NOT NULL
);

-- P4-3：发送节奏学习（每次成功 send 记一条，保留 14 天）
CREATE TABLE IF NOT EXISTS messenger_rpa_send_log (
    ts         REAL NOT NULL,
    hour_local INTEGER NOT NULL  -- 本地时区小时 (0-23)
);
CREATE INDEX IF NOT EXISTS idx_messenger_send_log_ts
    ON messenger_rpa_send_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messenger_send_log_hour
    ON messenger_rpa_send_log(hour_local, ts DESC);

-- P4-7：chat 级信用分（credit 0-100，起始 100；每次 reject/escalation 扣分）
CREATE TABLE IF NOT EXISTS messenger_rpa_chat_credit (
    chat_key   TEXT PRIMARY KEY,
    credit     INTEGER NOT NULL DEFAULT 100,
    updated_at REAL NOT NULL DEFAULT 0,
    last_reason TEXT DEFAULT ''
);
"""


def default_state_db_path(
    config_path: Path | str, account_id: str = "default"
) -> Path:
    """状态库路径默认与 config.yaml 同目录。

    P5-1：account_id != 'default' 时使用独立文件
    ``messenger_rpa_state_{account_id}.db``；'default' 保留旧路径
    ``messenger_rpa_state.db`` 以实现零迁移向后兼容。
    """
    parent = Path(config_path).parent
    if account_id and account_id != "default":
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account_id)
        return parent / f"messenger_rpa_state_{safe}.db"
    return parent / "messenger_rpa_state.db"


class MessengerRpaStateStore:
    """SQLite 包装；线程安全（用 RLock 串行所有写入）。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_runs_kept: int = 500,
        account_id: str = "default",
    ) -> None:
        self._db_path = str(db_path)
        self._max_runs_kept = max(int(max_runs_kept or 500), 100)
        self._account_id = str(account_id or "default")
        self._lock = threading.RLock()
        self._init_db()

    @property
    def account_id(self) -> str:
        return self._account_id

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._lock, self._conn() as c:
            for stmt in _DDL.strip().split(";\n\n"):
                s = stmt.strip()
                if s:
                    c.executescript(s + ";")
            c.commit()
            # 幂等迁移：旧库可能没有 escalated_until_ts / escalation_reason
            for alter in (
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN escalated_until_ts REAL DEFAULT 0",
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN escalation_reason TEXT DEFAULT ''",
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN sends_today_ts TEXT DEFAULT ''",  # P1-6 预留
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN variant TEXT DEFAULT ''",  # P2-3 A/B persona
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN updated_at REAL DEFAULT 0",  # P2-3
                # P6-3/P7 补迁移：approvals 打 ai_tier 便于批量过滤
                "ALTER TABLE messenger_rpa_approvals "
                "ADD COLUMN ai_tier TEXT DEFAULT ''",
            ):
                try:
                    c.execute(alter)
                except sqlite3.OperationalError:
                    pass  # 已存在
            c.commit()

    # ── chat 状态 ────────────────────────────────
    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_chat_state WHERE chat_key=?",
                (chat_key,),
            ).fetchone()
        return dict(row) if row else {}

    def update_chat_state(
        self,
        chat_key: str,
        *,
        chat_name: Optional[str] = None,
        last_peer_text: Optional[str] = None,
        last_peer_fp: Optional[str] = None,
        last_peer_kind: Optional[str] = None,
        last_reply: Optional[str] = None,
        last_screen_sha256: Optional[str] = None,
    ) -> None:
        now = time.time()
        prev = self.get_chat_state(chat_key)
        merged = dict(prev) if prev else {}

        def _set(k: str, v: Optional[str]) -> None:
            if v is not None:
                merged[k] = v

        _set("chat_name", chat_name)
        _set("last_peer_text", last_peer_text)
        _set("last_peer_fp", last_peer_fp)
        _set("last_peer_kind", last_peer_kind)
        _set("last_reply", last_reply)
        _set("last_screen_sha256", last_screen_sha256)
        merged["chat_key"] = chat_key
        merged["updated_at"] = now

        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_chat_state
                    (chat_key, chat_name, last_peer_text, last_peer_fp,
                     last_peer_kind, last_reply, last_screen_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    chat_name=excluded.chat_name,
                    last_peer_text=excluded.last_peer_text,
                    last_peer_fp=excluded.last_peer_fp,
                    last_peer_kind=excluded.last_peer_kind,
                    last_reply=excluded.last_reply,
                    last_screen_sha256=excluded.last_screen_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_key,
                    merged.get("chat_name", ""),
                    merged.get("last_peer_text", ""),
                    merged.get("last_peer_fp", ""),
                    merged.get("last_peer_kind", ""),
                    merged.get("last_reply", ""),
                    merged.get("last_screen_sha256", ""),
                    now,
                ),
            )
            c.commit()

    def is_duplicate(self, chat_key: str, fp: str) -> bool:
        """该 chat 上一条对方消息的 fingerprint 是否与本次相同。"""
        if not fp:
            return False
        st = self.get_chat_state(chat_key)
        return bool(st) and st.get("last_peer_fp") == fp

    # ── P1-4：人工转接（escalation）──────────────
    def set_escalation(
        self,
        chat_key: str,
        *,
        until_ts: float,
        reason: str,
        chat_name: Optional[str] = None,
    ) -> None:
        """把 chat 标记为已人工转接，until_ts 之前禁止 auto 回复。"""
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_chat_state
                    (chat_key, chat_name, updated_at,
                     escalated_until_ts, escalation_reason)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    chat_name=COALESCE(NULLIF(excluded.chat_name, ''),
                                       messenger_rpa_chat_state.chat_name),
                    updated_at=excluded.updated_at,
                    escalated_until_ts=excluded.escalated_until_ts,
                    escalation_reason=excluded.escalation_reason
                """,
                (chat_key, chat_name or "", now, float(until_ts), reason or ""),
            )
            c.commit()

    def is_escalated(self, chat_key: str) -> Tuple[bool, Dict[str, Any]]:
        """返回 (是否仍在人工转接冷却期, 详细信息)。"""
        st = self.get_chat_state(chat_key)
        if not st:
            return False, {}
        until_ts = float(st.get("escalated_until_ts") or 0)
        if until_ts and until_ts > time.time():
            return True, {
                "escalated_until_ts": until_ts,
                "escalation_reason": st.get("escalation_reason") or "",
                "remaining_sec": int(until_ts - time.time()),
            }
        return False, {}

    def clear_escalation(self, chat_key: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE messenger_rpa_chat_state "
                "SET escalated_until_ts=0, escalation_reason='' "
                "WHERE chat_key=?",
                (chat_key,),
            )
            c.commit()

    # ── P1-6：反封号 — 发送速率 / 日额 ─────────────
    _SEND_COUNTER_KEY = "send_counters_v1"

    def _load_send_counters(self) -> Dict[str, Any]:
        raw = self.get_meta(self._SEND_COUNTER_KEY, "") or ""
        if not raw:
            return {"date": "", "count": 0, "last_send_ts": 0.0}
        try:
            d = json.loads(raw)
            return {
                "date": str(d.get("date") or ""),
                "count": int(d.get("count") or 0),
                "last_send_ts": float(d.get("last_send_ts") or 0),
            }
        except Exception:
            return {"date": "", "count": 0, "last_send_ts": 0.0}

    def _save_send_counters(self, data: Dict[str, Any]) -> None:
        try:
            self.set_meta(self._SEND_COUNTER_KEY, json.dumps(data))
        except Exception:
            logger.debug("save_send_counters failed", exc_info=True)

    @staticmethod
    def _today_str() -> str:
        import datetime as _dt
        return _dt.datetime.now().strftime("%Y-%m-%d")

    def get_send_stats(self) -> Dict[str, Any]:
        d = self._load_send_counters()
        today = self._today_str()
        if d.get("date") != today:
            d = {"date": today, "count": 0, "last_send_ts": d.get("last_send_ts", 0)}
        return d

    def record_send(self) -> Dict[str, Any]:
        """每次成功 send 后调用。返回更新后的计数。"""
        now = time.time()
        today = self._today_str()
        d = self._load_send_counters()
        if d.get("date") != today:
            d = {"date": today, "count": 0, "last_send_ts": 0}
        d["count"] = int(d.get("count") or 0) + 1
        d["last_send_ts"] = now
        self._save_send_counters(d)
        # ★ P4-3：追加到 send_log（用于节奏学习）
        try:
            hour = int(time.localtime(now).tm_hour)
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?, ?)",
                    (now, hour),
                )
                # 顺手清理 > 14 天的老数据
                c.execute(
                    "DELETE FROM messenger_rpa_send_log WHERE ts < ?",
                    (now - 14 * 86400,),
                )
                c.commit()
        except Exception:
            pass
        return d

    # ── P4-3：发送节奏学习 ───────────────────────
    def pace_check(
        self,
        *,
        min_samples: int = 20,
        median_multiplier: float = 1.5,
        block_multiplier: float = 2.5,
    ) -> Dict[str, Any]:
        """返回本小时是否允许继续发送。

        决策：
          - samples < min_samples → allow（冷启动，不做节奏约束）
          - current/hist_median >= block_multiplier → deny（推建议 skip 本条）
          - current/hist_median >= median_multiplier → throttle（建议走审批）
          - 否则 → allow

        返回 {allow, throttle, decision, ratio, current_hour_count, hist_median,
               samples, hour}
        """
        out: Dict[str, Any] = {
            "allow": True, "throttle": False, "decision": "allow",
            "ratio": 0.0, "current_hour_count": 0, "hist_median": 0.0,
            "samples": 0, "hour": -1,
        }
        try:
            now = time.time()
            hour = int(time.localtime(now).tm_hour)
            out["hour"] = hour
            since = now - 14 * 86400
            with self._lock, self._conn() as c:
                total = c.execute(
                    "SELECT COUNT(*) AS n FROM messenger_rpa_send_log WHERE ts >= ?",
                    (since,),
                ).fetchone()
                samples = int(total["n"] if total else 0)
                out["samples"] = samples
                if samples < int(min_samples):
                    return out

                # 本小时（本地时间）已发送
                hour_start = now - (now % 3600)
                cur = c.execute(
                    "SELECT COUNT(*) AS n FROM messenger_rpa_send_log "
                    "WHERE ts >= ?", (hour_start,),
                ).fetchone()
                current_count = int(cur["n"] if cur else 0)
                out["current_hour_count"] = current_count

                # 历史：按 (floor_day, hour) 分组得到每天该小时的总发送量，取中位数
                rows = c.execute(
                    "SELECT CAST(ts/86400 AS INTEGER) AS day, COUNT(*) AS n "
                    "FROM messenger_rpa_send_log "
                    "WHERE ts >= ? AND hour_local = ? "
                    "GROUP BY day ORDER BY day DESC LIMIT 14",
                    (since, hour),
                ).fetchall()
                day_counts = [int(r["n"]) for r in rows]
                if not day_counts:
                    return out
                day_counts.sort()
                mid = len(day_counts) // 2
                if len(day_counts) % 2 == 1:
                    median = float(day_counts[mid])
                else:
                    median = (day_counts[mid - 1] + day_counts[mid]) / 2.0
                # 中位数不能 < 1（否则小基数导致无意义的 ratio）
                median = max(median, 1.0)
                out["hist_median"] = round(median, 2)
                ratio = current_count / median
                out["ratio"] = round(ratio, 2)
                if ratio >= float(block_multiplier):
                    out["allow"] = False
                    out["decision"] = "deny"
                elif ratio >= float(median_multiplier):
                    out["throttle"] = True
                    out["decision"] = "throttle"
                else:
                    out["decision"] = "allow"
        except Exception:
            # 统计错误绝不拦截主流程
            out["allow"] = True
            out["throttle"] = False
            out["decision"] = "allow_on_error"
        return out

    # ── P4-7：chat 信用分 ────────────────────────
    def get_credit(self, chat_key: str) -> Dict[str, Any]:
        """读某 chat 的信用分。缺省 100。"""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT credit, updated_at, last_reason "
                "FROM messenger_rpa_chat_credit WHERE chat_key=?",
                (str(chat_key),),
            ).fetchone()
        if row:
            return {
                "chat_key": str(chat_key),
                "credit": int(row["credit"]),
                "updated_at": float(row["updated_at"] or 0),
                "last_reason": str(row["last_reason"] or ""),
            }
        return {
            "chat_key": str(chat_key),
            "credit": 100,
            "updated_at": 0.0,
            "last_reason": "",
        }

    def adjust_credit(
        self, chat_key: str, delta: int, *, reason: str = "",
        floor: int = 0, ceil: int = 100,
    ) -> Dict[str, Any]:
        """调整信用分，clamp 到 [floor, ceil]。"""
        if not chat_key:
            return {"credit": ceil}
        cur = self.get_credit(chat_key)
        new = max(int(floor), min(int(ceil), int(cur["credit"]) + int(delta)))
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messenger_rpa_chat_credit"
                "(chat_key, credit, updated_at, last_reason) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET "
                "credit=excluded.credit, updated_at=excluded.updated_at, "
                "last_reason=excluded.last_reason",
                (str(chat_key), int(new), now, str(reason)[:200]),
            )
            c.commit()
        return {
            "chat_key": str(chat_key),
            "credit": new,
            "delta": int(delta),
            "updated_at": now,
            "last_reason": str(reason)[:200],
        }

    def credit_stats(self) -> Dict[str, Any]:
        """给 Web/metrics 用：分布 + 异常 chat 列表。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT chat_key, credit, last_reason, updated_at "
                "FROM messenger_rpa_chat_credit "
                "ORDER BY credit ASC LIMIT 100"
            ).fetchall()
        items = [dict(r) for r in rows]
        dist = {"100": 0, "80_99": 0, "60_79": 0, "40_59": 0, "20_39": 0, "0_19": 0}
        for r in items:
            c = int(r["credit"])
            if c >= 100:
                dist["100"] += 1
            elif c >= 80:
                dist["80_99"] += 1
            elif c >= 60:
                dist["60_79"] += 1
            elif c >= 40:
                dist["40_59"] += 1
            elif c >= 20:
                dist["20_39"] += 1
            else:
                dist["0_19"] += 1
        low = [r for r in items if int(r["credit"]) < 40]
        return {"distribution": dist, "low_credit_chats": low[:20],
                 "total_tracked": len(items)}

    # ── run 历史 ────────────────────────────────
    def append_run(self, run: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_runs
                    (ts, chat_key, chat_name, ok, step, peer_text, peer_kind,
                     reply_text, reader_path, total_ms, error, screenshot_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(run.get("ts") or time.time()),
                    str(run.get("chat_key") or ""),
                    str(run.get("chat_name") or ""),
                    1 if run.get("ok") else 0,
                    str(run.get("step") or ""),
                    str(run.get("peer_text") or ""),
                    str(run.get("peer_kind") or ""),
                    str(run.get("reply_text") or ""),
                    str(run.get("reader_path") or ""),
                    float(run.get("total_ms") or 0),
                    str(run.get("error") or ""),
                    str(run.get("screenshot_path") or ""),
                ),
            )
            c.execute(
                """
                DELETE FROM messenger_rpa_runs
                WHERE id NOT IN (
                    SELECT id FROM messenger_rpa_runs
                    ORDER BY ts DESC LIMIT ?
                )
                """,
                (self._max_runs_kept,),
            )
            c.commit()

    def recent_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_runs ORDER BY ts DESC LIMIT ?",
                (max(int(limit or 50), 1),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 元数据 ────────────────────────────────
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT v FROM messenger_rpa_meta WHERE k=?", (key,)
            ).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messenger_rpa_meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            c.commit()

    # ── 审批队列（reply_mode=approve）────────────────
    def enqueue_approval(
        self,
        *,
        chat_key: str,
        chat_name: str,
        peer_text: str,
        peer_kind: str,
        reply_text: str,
        reply_lang: str = "",
        screenshot_path: str = "",
        run_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        ai_tier: str = "",
    ) -> int:
        """把待审批的回复写进 approvals 表；返回 row id。

        P6-3：``ai_tier`` 用于批量审批时按 tier 过滤（premium/normal/low）。
        旧调用者可忽略（默认空字符串）。
        """
        if not reply_text.strip():
            raise ValueError("reply_text 为空，不能入队审批")
        if not chat_key.strip():
            raise ValueError("chat_key 不能为空")
        extra_json = ""
        if extra:
            try:
                extra_json = json.dumps(extra, ensure_ascii=False)[:4000]
            except Exception:
                extra_json = ""
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO messenger_rpa_approvals
                  (created_at, chat_key, chat_name, peer_text, peer_kind,
                   reply_text, reply_lang, status, screenshot_path, run_id,
                   extra_json, ai_tier)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    now,
                    chat_key.strip(),
                    chat_name or "",
                    peer_text or "",
                    peer_kind or "",
                    reply_text.strip(),
                    reply_lang or "",
                    screenshot_path or "",
                    run_id or "",
                    extra_json,
                    (ai_tier or "").strip(),
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)

    def list_approvals(
        self,
        *,
        status: Optional[str] = None,
        chat_key: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列审批；status=None 时返回所有。"""
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if chat_key:
            clauses.append("chat_key=?")
            params.append(chat_key)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(int(limit or 50), 1))
        sql = (
            f"SELECT * FROM messenger_rpa_approvals {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        with self._lock, self._conn() as c:
            rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_approval(self, approval_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_approvals WHERE id=?",
                (int(approval_id),),
            ).fetchone()
        return dict(row) if row else None

    def decide_approval(
        self,
        approval_id: int,
        *,
        approve: bool,
        decided_by: str = "",
        decision_note: str = "",
        reply_text_override: Optional[str] = None,
    ) -> bool:
        """approve=True → status=approved；False → rejected。

        可选 reply_text_override：approve 时同步用新文案覆盖 reply_text，
        支持人工审批时修改措辞后再发出。
        """
        new_status = "approved" if approve else "rejected"
        with self._lock, self._conn() as c:
            if approve and reply_text_override is not None:
                cur = c.execute(
                    "UPDATE messenger_rpa_approvals "
                    "SET status=?, decided_at=?, decided_by=?, decision_note=?, "
                    "    reply_text=? "
                    "WHERE id=? AND status='pending'",
                    (
                        new_status,
                        time.time(),
                        decided_by or "",
                        decision_note or "",
                        str(reply_text_override),
                        int(approval_id),
                    ),
                )
            else:
                cur = c.execute(
                    "UPDATE messenger_rpa_approvals "
                    "SET status=?, decided_at=?, decided_by=?, decision_note=? "
                    "WHERE id=? AND status='pending'",
                    (
                        new_status,
                        time.time(),
                        decided_by or "",
                        decision_note or "",
                        int(approval_id),
                    ),
                )
            c.commit()
            return cur.rowcount > 0

    def update_approval_reply(
        self, approval_id: int, *, reply_text: str
    ) -> bool:
        """在 pending 状态下修改 reply_text（不改变 status）。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE messenger_rpa_approvals SET reply_text=? "
                "WHERE id=? AND status='pending'",
                (str(reply_text), int(approval_id)),
            )
            c.commit()
            return cur.rowcount > 0

    # ── P3-1：账号风控状态 ──────────────────────────
    def get_risk_state(self) -> Dict[str, Any]:
        """读 meta 表里的风控快照。

        字段：
          - status: normal | warning_once | blocked
          - hit_count: 连续命中次数（>0 才算有风险）
          - last_reason: 最近一次 vision 原文
          - last_severity: warn | block
          - last_hit_ts: 最近一次命中时间戳
          - blocked_until_ts: 风控 pause 到期时间戳
        """
        keys = [
            "risk.status", "risk.hit_count", "risk.last_reason",
            "risk.last_severity", "risk.last_hit_ts", "risk.blocked_until_ts",
        ]
        out: Dict[str, Any] = {}
        with self._lock, self._conn() as c:
            for k in keys:
                row = c.execute(
                    "SELECT v FROM messenger_rpa_meta WHERE k=?", (k,),
                ).fetchone()
                v = row["v"] if row else ""
                short = k.split(".", 1)[1]
                if short in ("hit_count",):
                    try:
                        out[short] = int(v or 0)
                    except ValueError:
                        out[short] = 0
                elif short in ("last_hit_ts", "blocked_until_ts"):
                    try:
                        out[short] = float(v or 0)
                    except ValueError:
                        out[short] = 0.0
                else:
                    out[short] = v or ""
        if not out.get("status"):
            out["status"] = "normal"
        return out

    def record_risk_hit(
        self, *, severity: str, reason: str,
        block_duration_sec: int = 86400,
        require_consecutive: int = 2,
    ) -> Dict[str, Any]:
        """记录一次 vision 报告的风险事件。

        策略：
          - severity=block 命中 >= require_consecutive 次 → 置 status=blocked，
            blocked_until_ts = now + block_duration_sec
          - severity=warn 命中 >= require_consecutive 次 → 置 status=warning_once
          - 首次命中 → hit_count=1，status 不升级（等下一次证实）

        返回 {status, hit_count, just_blocked, just_warned}。
        """
        now = time.time()
        state = self.get_risk_state()
        prev_hit = int(state.get("hit_count") or 0)
        prev_sev = str(state.get("last_severity") or "")
        # 只有**同级**命中才累加（warn 和 block 各自独立计数，避免 warn 污染 block）
        if prev_sev == severity and prev_hit > 0:
            new_hit = prev_hit + 1
        else:
            new_hit = 1

        just_blocked = False
        just_warned = False
        new_status = str(state.get("status") or "normal")
        blocked_until = float(state.get("blocked_until_ts") or 0)

        if severity == "block" and new_hit >= require_consecutive:
            new_status = "blocked"
            blocked_until = max(blocked_until, now + int(block_duration_sec))
            just_blocked = True
        elif severity == "warn" and new_hit >= require_consecutive and new_status == "normal":
            new_status = "warning_once"
            just_warned = True

        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT INTO messenger_rpa_meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                [
                    ("risk.status", new_status),
                    ("risk.hit_count", str(new_hit)),
                    ("risk.last_reason", str(reason)[:200]),
                    ("risk.last_severity", str(severity)),
                    ("risk.last_hit_ts", str(now)),
                    ("risk.blocked_until_ts", str(blocked_until)),
                ],
            )
            c.commit()
        return {
            "status": new_status,
            "hit_count": new_hit,
            "just_blocked": just_blocked,
            "just_warned": just_warned,
            "blocked_until_ts": blocked_until,
        }

    def clear_risk(self) -> None:
        """run 成功发送后调用，把 hit_count 归零、status 降到 normal。
        （blocked_until_ts 不清 — 只能等到期）"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "SELECT v FROM messenger_rpa_meta WHERE k='risk.status'"
            ).fetchone()
            if cur and cur["v"] == "blocked":
                return  # blocked 只能靠 blocked_until_ts 到期
            c.executemany(
                "INSERT INTO messenger_rpa_meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                [
                    ("risk.status", "normal"),
                    ("risk.hit_count", "0"),
                ],
            )
            c.commit()

    def is_risk_blocked_now(self) -> Tuple[bool, float]:
        """当前是否处于 block 状态（未到 blocked_until_ts）。

        返回 (blocked, blocked_until_ts)。
        """
        state = self.get_risk_state()
        status = str(state.get("status") or "normal")
        until = float(state.get("blocked_until_ts") or 0)
        if status == "blocked" and until > time.time():
            return True, until
        # 到期自动解除
        if status == "blocked" and until and until <= time.time():
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO messenger_rpa_meta(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    ("risk.status", "normal"),
                )
                c.commit()
            return False, 0.0
        return False, 0.0

    # ── P2-3：A/B persona 实验 ───────────────────────
    def assign_variant(
        self, chat_key: str, *, weights: Dict[str, float]
    ) -> Optional[str]:
        """为 chat_key 分配 variant，sticky（已分配过直接返回旧值）。

        weights 是 {variant_name: weight}。空/权重和为 0 → 返回 None。
        """
        if not chat_key or not weights:
            return None
        total = sum(max(float(w), 0.0) for w in weights.values())
        if total <= 0:
            return None
        # 读现有
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT variant FROM messenger_rpa_chat_state WHERE chat_key=?",
                (str(chat_key),),
            ).fetchone()
            if row and row["variant"]:
                # 命中旧的，但如果旧的已不在 weights 里说明实验变了，重新分配
                if str(row["variant"]) in weights:
                    return str(row["variant"])
            # 新分配：hash-based deterministic（同 chat_key 永远落同 variant）
            h = int(hashlib.md5(str(chat_key).encode("utf-8")).hexdigest(), 16)
            r = (h % 10_000) / 10_000.0  # [0, 1)
            # 按 weights 顺序累积分桶
            cum = 0.0
            picked = None
            for name, w in weights.items():
                cum += max(float(w), 0.0) / total
                if r < cum:
                    picked = name
                    break
            if picked is None:
                picked = next(iter(weights.keys()))
            # 写 messenger_rpa_chat_state（使用 UPSERT）
            c.execute(
                "INSERT INTO messenger_rpa_chat_state(chat_key, variant, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET "
                "  variant=excluded.variant, updated_at=excluded.updated_at",
                (str(chat_key), picked, time.time()),
            )
            c.commit()
            return picked

    def variant_stats(self) -> Dict[str, Any]:
        """按 variant 聚合 Messenger run/approval 指标。

        返回：{
          variants: {
            A: {chats, sends, approvals_pending, approvals_approved,
                approvals_rejected, escalations},
            B: { ... },
            _none: { ... },   # 未分配或旧数据
          }
        }
        """
        out: Dict[str, Dict[str, int]] = {}
        with self._lock, self._conn() as c:
            # 1) 每个 variant 的 chat 数
            for r in c.execute(
                "SELECT COALESCE(variant,'') AS v, COUNT(*) AS n "
                "FROM messenger_rpa_chat_state GROUP BY COALESCE(variant,'')"
            ).fetchall():
                k = (r["v"] or "_none")
                out.setdefault(k, {}).update({"chats": int(r["n"])})
            # 2) escalation 计数（当前处于升级状态）
            for r in c.execute(
                "SELECT COALESCE(variant,'') AS v, COUNT(*) AS n "
                "FROM messenger_rpa_chat_state "
                "WHERE escalated_until_ts IS NOT NULL "
                "  AND escalated_until_ts > ? "
                "GROUP BY COALESCE(variant,'')",
                (time.time(),),
            ).fetchall():
                k = (r["v"] or "_none")
                out.setdefault(k, {}).setdefault("chats", 0)
                out[k]["escalations_active"] = int(r["n"])
            # 3) approvals 汇总（join chat_state）
            for r in c.execute(
                "SELECT COALESCE(s.variant,'') AS v, a.status AS st, "
                "       COUNT(*) AS n "
                "FROM messenger_rpa_approvals a "
                "LEFT JOIN messenger_rpa_chat_state s "
                "  ON s.chat_key = a.chat_key "
                "GROUP BY COALESCE(s.variant,''), a.status"
            ).fetchall():
                k = (r["v"] or "_none")
                out.setdefault(k, {}).setdefault("chats", 0)
                st = str(r["st"] or "unknown")
                out[k][f"apr_{st}"] = int(r["n"])
        # 计算派生指标
        for k, d in out.items():
            apr_sent = int(d.get("apr_sent", 0))
            apr_rej = int(d.get("apr_rejected", 0))
            total = apr_sent + apr_rej
            d["approve_ratio"] = (
                round(apr_sent / total, 4) if total else None
            )
        return {"variants": out, "ts": time.time()}

    def pending_sla_stats(self, *, threshold_sec: int = 600) -> Dict[str, Any]:
        """统计 pending 审批的 SLA 情况（P2-6）。

        返回：
          - pending_count: 当前 pending 总数
          - oldest_age_sec: 最老 pending 的年龄（秒），没有时为 0
          - overdue_count: 超过 threshold_sec 的 pending 条数
          - overdue_ids: 超时 id 列表（限 20 条，供告警 dedup）
        """
        now = time.time()
        thr = max(int(threshold_sec), 1)
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, created_at FROM messenger_rpa_approvals "
                "WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall()
        ages = []
        overdue_ids: List[int] = []
        for r in rows:
            age = max(0.0, now - float(r["created_at"] or now))
            ages.append(age)
            if age >= thr:
                overdue_ids.append(int(r["id"]))
        return {
            "pending_count": len(rows),
            "oldest_age_sec": int(ages[-1]) if ages else 0,
            "overdue_count": len(overdue_ids),
            "overdue_ids": overdue_ids[:20],
            "threshold_sec": thr,
        }

    def patch_approval_extra(
        self, approval_id: int, *, patch: Dict[str, Any]
    ) -> bool:
        """合并 patch 到 approval 的 extra_json（JSON 对象）。任意状态都允许。"""
        if not patch:
            return False
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT extra_json FROM messenger_rpa_approvals WHERE id=?",
                (int(approval_id),),
            ).fetchone()
            if not row:
                return False
            try:
                cur_extra = json.loads(row["extra_json"] or "{}") or {}
            except Exception:
                cur_extra = {}
            if not isinstance(cur_extra, dict):
                cur_extra = {}
            cur_extra.update(patch)
            c.execute(
                "UPDATE messenger_rpa_approvals SET extra_json=? WHERE id=?",
                (json.dumps(cur_extra, ensure_ascii=False), int(approval_id)),
            )
            c.commit()
            return True

    # ── 跳过列表（spam / 黑名单）─────────────────
    def add_skipped_chat(
        self, chat_key: str, *, chat_name: str = "", reason: str = ""
    ) -> None:
        if not chat_key.strip():
            return
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messenger_rpa_skipped_chats "
                "(chat_key, chat_name, reason, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET "
                "  chat_name=excluded.chat_name, reason=excluded.reason",
                (chat_key.strip(), chat_name or "", reason or "", time.time()),
            )
            c.commit()

    def is_skipped_chat(self, chat_key: str) -> bool:
        if not chat_key.strip():
            return False
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM messenger_rpa_skipped_chats WHERE chat_key=?",
                (chat_key.strip(),),
            ).fetchone()
        return row is not None

    def list_skipped_chats(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_skipped_chats "
                "ORDER BY created_at DESC LIMIT ?",
                (max(int(limit or 100), 1),),
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_skipped_chat(self, chat_key: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM messenger_rpa_skipped_chats WHERE chat_key=?",
                (chat_key.strip(),),
            )
            c.commit()
            return cur.rowcount > 0

    def mark_approval_sent(
        self,
        approval_id: int,
        *,
        ok: bool,
        send_error: str = "",
    ) -> bool:
        new_status = "sent" if ok else "failed"
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE messenger_rpa_approvals "
                "SET status=?, sent_at=?, send_error=? WHERE id=?",
                (
                    new_status,
                    time.time() if ok else 0,
                    send_error or "",
                    int(approval_id),
                ),
            )
            c.commit()
            return cur.rowcount > 0
