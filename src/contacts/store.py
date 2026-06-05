"""ContactStore — SQLite 封装。

设计参考 src/integrations/line_rpa/state_store.py：
- 单进程单 connection + threading.Lock
- WAL + busy_timeout + row_factory=Row
- 多表 DDL 一次 executescript
- 幂等 migration（PRAGMA table_info + ALTER TABLE ADD COLUMN）

核心原则：
1. ensure_channel_identity 是 onboarding 的唯一入口，自动建 Contact + Journey
2. 所有写操作在一个事务内完成（含关联 journey_event），避免脏状态
3. 合并操作通过 relink_channel_identity + 孤岛 Contact 清理，保持数据一致
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .ids import new_id
from .models import (
    Contact,
    ChannelIdentity,
    HandoffToken,
    Journey,
    STAGE_INITIAL,
    VALID_CHANNELS,
)

logger = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS contacts (
    contact_id        TEXT PRIMARY KEY,
    primary_name      TEXT NOT NULL DEFAULT '',
    language_hint     TEXT NOT NULL DEFAULT '',
    timezone_hint     TEXT NOT NULL DEFAULT '',
    country_hint      TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    last_active_at    INTEGER NOT NULL DEFAULT 0,
    notes             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_contacts_last_active ON contacts(last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(primary_name);

CREATE TABLE IF NOT EXISTS channel_identities (
    channel_identity_id     TEXT PRIMARY KEY,
    contact_id              TEXT NOT NULL,
    channel                 TEXT NOT NULL,
    account_id              TEXT NOT NULL,
    external_id             TEXT NOT NULL,
    direction               TEXT NOT NULL DEFAULT 'first_seen',
    linked_at               INTEGER NOT NULL DEFAULT 0,
    linked_via              TEXT NOT NULL DEFAULT '',
    attribution_confidence  REAL NOT NULL DEFAULT 0.0,
    display_name            TEXT NOT NULL DEFAULT '',
    UNIQUE(channel, account_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_ci_contact ON channel_identities(contact_id);
CREATE INDEX IF NOT EXISTS idx_ci_channel ON channel_identities(channel);
CREATE INDEX IF NOT EXISTS idx_ci_external_id ON channel_identities(external_id);

CREATE TABLE IF NOT EXISTS handoff_tokens (
    token                TEXT PRIMARY KEY,
    issued_from_ci_id    TEXT NOT NULL,
    issued_at            INTEGER NOT NULL,
    expires_at           INTEGER NOT NULL,
    consumed_by_ci_id    TEXT NOT NULL DEFAULT '',
    consumed_at          INTEGER NOT NULL DEFAULT 0,
    revoked_reason       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tokens_issued_from ON handoff_tokens(issued_from_ci_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON handoff_tokens(expires_at);

CREATE TABLE IF NOT EXISTS journeys (
    journey_id              TEXT PRIMARY KEY,
    contact_id              TEXT NOT NULL UNIQUE,
    persona_id              TEXT NOT NULL DEFAULT '',
    funnel_stage            TEXT NOT NULL,
    intimacy_score          REAL NOT NULL DEFAULT 0.0,
    engagement_score        REAL NOT NULL DEFAULT 0.0,
    readiness_score         REAL NOT NULL DEFAULT 0.0,
    intimacy_updated_at     INTEGER NOT NULL DEFAULT 0,
    context_snapshot_json   TEXT NOT NULL DEFAULT '',
    snapshot_refreshed_at   INTEGER NOT NULL DEFAULT 0,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journeys_stage ON journeys(funnel_stage);
CREATE INDEX IF NOT EXISTS idx_journeys_updated ON journeys(updated_at DESC);

CREATE TABLE IF NOT EXISTS journey_events (
    event_id       TEXT PRIMARY KEY,
    journey_id     TEXT NOT NULL,
    trace_id       TEXT NOT NULL DEFAULT '',
    event_type     TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_journey_ts ON journey_events(journey_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON journey_events(event_type);

CREATE TABLE IF NOT EXISTS merge_review_queue (
    review_id          TEXT PRIMARY KEY,
    candidate_ci_id    TEXT NOT NULL,
    target_contact_id  TEXT NOT NULL,
    confidence         REAL NOT NULL,
    breakdown_json     TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    created_at         INTEGER NOT NULL,
    resolved_at        INTEGER NOT NULL DEFAULT 0,
    resolved_by        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_status ON merge_review_queue(status, created_at DESC);

-- W3-3G：reunion 草稿日志（关联生成 → 发送 → 回复评估的完整闭环）
CREATE TABLE IF NOT EXISTS draft_log (
    draft_id          TEXT PRIMARY KEY,
    journey_id        TEXT NOT NULL,
    contact_id        TEXT NOT NULL DEFAULT '',
    draft_text        TEXT NOT NULL,
    draft_lang        TEXT NOT NULL DEFAULT 'zh',
    intimacy_score    REAL NOT NULL DEFAULT 0,
    silent_days       INTEGER NOT NULL DEFAULT 0,
    funnel_stage      TEXT NOT NULL DEFAULT '',
    prompt_variant    TEXT NOT NULL DEFAULT 'v1',
    -- W3-3I.5：prompt 文本的 stable hash（前 8 字节 SHA-256 hex）
    -- 用于"这条 draft 当时用的什么 prompt 文本"回溯（持久化的 yaml 改了之后仍能对账）
    prompt_snapshot_hash TEXT NOT NULL DEFAULT '',
    generated_at      INTEGER NOT NULL,
    -- 发送态：sent_ts NULL = 还没标记已发
    sent_ts           INTEGER,
    sent_by           TEXT,
    -- 评估态：success_eval_ts NULL = 还没评估
    success_eval_ts   INTEGER,
    success           INTEGER,        -- 1=有回复 / 0=无回复 / NULL=未评估
    reply_event_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_jid ON draft_log(journey_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_draft_sent ON draft_log(sent_ts) WHERE sent_ts IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_draft_pending_eval ON draft_log(sent_ts) WHERE sent_ts IS NOT NULL AND success_eval_ts IS NULL;

CREATE TABLE IF NOT EXISTS account_handoff_counters (
    account_id  TEXT NOT NULL,
    day         TEXT NOT NULL,      -- YYYY-MM-DD (UTC)
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day)
);
CREATE INDEX IF NOT EXISTS idx_counters_day ON account_handoff_counters(day);

-- B2: KPI 漏斗告警（种类：kpi_drop_engaged_rate / kpi_drop_handoff_rate / ...）
CREATE TABLE IF NOT EXISTS kpi_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT DEFAULT 'warn',
    message     TEXT DEFAULT '',
    detail_json TEXT DEFAULT '{}',
    acked       INTEGER DEFAULT 0,
    acked_at    REAL,
    acked_by    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kpi_alerts_ts   ON kpi_alerts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_kpi_alerts_kind ON kpi_alerts(kind, ts DESC);

-- Phase 5-4：pre-chat 留资属性（phone/email/name 等），支持按属性去重合并身份。
CREATE TABLE IF NOT EXISTS contact_attributes (
    contact_id   TEXT NOT NULL,
    attr_key     TEXT NOT NULL,
    attr_value   TEXT NOT NULL DEFAULT '',
    updated_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contact_id, attr_key)
);
CREATE INDEX IF NOT EXISTS idx_attr_lookup ON contact_attributes(attr_key, attr_value);
CREATE INDEX IF NOT EXISTS idx_attr_contact ON contact_attributes(contact_id);

-- Phase 6-3：客户标签（多对一），支持标签筛选与聚合自动补全。
CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id   TEXT NOT NULL,
    tag          TEXT NOT NULL,
    created_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contact_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON contact_tags(tag);

-- Phase 6-4：跟进任务化（可完成/带备注/指派），contacts.follow_up_at 作为「最近未完成到期」缓存。
CREATE TABLE IF NOT EXISTS follow_up_tasks (
    task_id     TEXT PRIMARY KEY,
    contact_id  TEXT NOT NULL,
    due_at      INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    assignee    TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL DEFAULT '',
    done_at     INTEGER NOT NULL DEFAULT 0,
    done_by     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fut_contact ON follow_up_tasks(contact_id);
CREATE INDEX IF NOT EXISTS idx_fut_open ON follow_up_tasks(done_at, due_at);
CREATE INDEX IF NOT EXISTS idx_fut_assignee ON follow_up_tasks(assignee, done_at, due_at);

-- Phase 6-4：预设标签库（名称/颜色/排序），自由标签仍可临时添加。
CREATE TABLE IF NOT EXISTS tag_library (
    tag         TEXT PRIMARY KEY,
    color       TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT 0
);
"""


class ContactStore:
    """线程安全的 SQLite 封装。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_DDL)
            # W3-3I.5：幂等 migration —— 老 DB 没 prompt_snapshot_hash 列时补上
            cols = {
                row[1] for row in self._conn.execute(
                    "PRAGMA table_info(draft_log)"
                ).fetchall()
            }
            if "prompt_snapshot_hash" not in cols:
                self._conn.execute(
                    "ALTER TABLE draft_log ADD COLUMN "
                    "prompt_snapshot_hash TEXT NOT NULL DEFAULT ''"
                )
            # Phase 6-3：contacts.follow_up_at（跟进提醒时间戳，0=无）
            ccols = {
                row[1] for row in self._conn.execute(
                    "PRAGMA table_info(contacts)"
                ).fetchall()
            }
            if "follow_up_at" not in ccols:
                self._conn.execute(
                    "ALTER TABLE contacts ADD COLUMN follow_up_at INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_contacts_followup "
                    "ON contacts(follow_up_at) WHERE follow_up_at > 0"
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── 通用 ────────────────────────────────────────────────
    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ── Contact ────────────────────────────────────────────
    def create_contact(
        self,
        *,
        primary_name: str = "",
        language_hint: str = "",
        timezone_hint: str = "",
        country_hint: str = "",
        notes: str = "",
    ) -> Contact:
        now = self._now()
        c = Contact(
            contact_id=new_id(),
            primary_name=primary_name,
            language_hint=language_hint,
            timezone_hint=timezone_hint,
            country_hint=country_hint,
            created_at=now,
            last_active_at=now,
            notes=notes,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO contacts (contact_id, primary_name, language_hint, timezone_hint, "
                "country_hint, created_at, last_active_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (c.contact_id, c.primary_name, c.language_hint, c.timezone_hint,
                 c.country_hint, c.created_at, c.last_active_at, c.notes),
            )
            self._conn.commit()
        return c

    def get_contact(self, contact_id: str) -> Optional[Contact]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contacts WHERE contact_id=?", (contact_id,)
            ).fetchone()
        return _row_to_contact(row) if row else None

    def update_contact(
        self,
        contact_id: str,
        *,
        primary_name: Optional[str] = None,
        language_hint: Optional[str] = None,
        timezone_hint: Optional[str] = None,
        country_hint: Optional[str] = None,
        last_active_at: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> bool:
        updates: List[str] = []
        params: List[Any] = []
        for col, val in [
            ("primary_name", primary_name),
            ("language_hint", language_hint),
            ("timezone_hint", timezone_hint),
            ("country_hint", country_hint),
            ("last_active_at", last_active_at),
            ("notes", notes),
        ]:
            if val is not None:
                updates.append(f"{col}=?")
                params.append(val)
        if not updates:
            return False
        params.append(contact_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE contacts SET {', '.join(updates)} WHERE contact_id=?", params
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── Contact 属性（Phase 5-4：pre-chat 留资 + 去重）────────────
    def set_contact_attribute(self, contact_id: str, key: str, value: str) -> None:
        """写入/更新一条联系人属性（如 phone/email）。空值删除该属性。"""
        cid = str(contact_id or "").strip()
        k = str(key or "").strip().lower()
        v = str(value or "").strip()
        if not cid or not k:
            return
        with self._lock:
            if not v:
                self._conn.execute(
                    "DELETE FROM contact_attributes WHERE contact_id=? AND attr_key=?",
                    (cid, k),
                )
            else:
                self._conn.execute(
                    "INSERT INTO contact_attributes (contact_id, attr_key, attr_value, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(contact_id, attr_key) DO UPDATE SET "
                    "attr_value=excluded.attr_value, updated_at=excluded.updated_at",
                    (cid, k, v, self._now()),
                )
            self._conn.commit()

    def get_contact_attributes(self, contact_id: str) -> Dict[str, str]:
        cid = str(contact_id or "").strip()
        if not cid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT attr_key, attr_value FROM contact_attributes WHERE contact_id=?",
                (cid,),
            ).fetchall()
        return {str(r["attr_key"]): str(r["attr_value"]) for r in rows}

    def find_contacts_by_attribute(
        self, key: str, value: str, *, exclude_contact_id: str = "",
    ) -> List[str]:
        """按属性值反查 contact_id（去重合并用）。返回最近更新优先的列表。"""
        k = str(key or "").strip().lower()
        v = str(value or "").strip()
        if not k or not v:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT contact_id FROM contact_attributes "
                "WHERE attr_key=? AND attr_value=? ORDER BY updated_at DESC",
                (k, v),
            ).fetchall()
        out: List[str] = []
        for r in rows:
            cid = str(r["contact_id"])
            if exclude_contact_id and cid == exclude_contact_id:
                continue
            if cid not in out:
                out.append(cid)
        return out

    # ── Phase 6-3/6-4：跟进任务 + 标签 ───────────────────────────
    def _recompute_follow_up(self, contact_id: str) -> int:
        """把 contacts.follow_up_at 重算为「最近未完成任务到期时间」（无则 0）。需持锁调用。"""
        row = self._conn.execute(
            "SELECT MIN(due_at) FROM follow_up_tasks "
            "WHERE contact_id=? AND done_at=0 AND due_at>0", (contact_id,),
        ).fetchone()
        nxt = int(row[0]) if row and row[0] else 0
        self._conn.execute(
            "UPDATE contacts SET follow_up_at=? WHERE contact_id=?", (nxt, contact_id)
        )
        return nxt

    def set_follow_up(self, contact_id: str, follow_up_at: int) -> bool:
        """快捷设置/清除「下次跟进」。

        基于任务表去重：>0 时更新最近未完成任务的到期（无则新建一条）；
        <=0 时完成所有未完成任务。contacts.follow_up_at 随之重算。
        """
        cid = str(contact_id or "").strip()
        if not cid:
            return False
        val = max(0, int(follow_up_at or 0))
        now = self._now()
        with self._lock:
            if val <= 0:
                self._conn.execute(
                    "UPDATE follow_up_tasks SET done_at=? WHERE contact_id=? AND done_at=0",
                    (now, cid),
                )
            else:
                row = self._conn.execute(
                    "SELECT task_id FROM follow_up_tasks WHERE contact_id=? AND done_at=0 "
                    "ORDER BY due_at LIMIT 1", (cid,),
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE follow_up_tasks SET due_at=? WHERE task_id=?",
                        (val, row["task_id"]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO follow_up_tasks "
                        "(task_id, contact_id, due_at, created_at) VALUES (?, ?, ?, ?)",
                        (new_id(), cid, val, now),
                    )
            self._recompute_follow_up(cid)
            self._conn.commit()
            return True

    def add_follow_up_task(
        self, contact_id: str, *, due_at: int, note: str = "",
        assignee: str = "", created_by: str = "",
    ) -> Optional[str]:
        """新增一条跟进任务，返回 task_id。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        tid = new_id()
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO follow_up_tasks "
                "(task_id, contact_id, due_at, note, assignee, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tid, cid, max(0, int(due_at or 0)), str(note or "")[:1000],
                 str(assignee or ""), now, str(created_by or "")),
            )
            self._recompute_follow_up(cid)
            self._conn.commit()
        return tid

    def complete_follow_up_task(self, task_id: str, *, done_by: str = "") -> bool:
        """标记某跟进任务完成，并重算所属 contact 的 follow_up_at。"""
        tid = str(task_id or "").strip()
        if not tid:
            return False
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT contact_id, done_at FROM follow_up_tasks WHERE task_id=?", (tid,)
            ).fetchone()
            if row is None or row["done_at"]:
                return False
            self._conn.execute(
                "UPDATE follow_up_tasks SET done_at=?, done_by=? WHERE task_id=?",
                (now, str(done_by or ""), tid),
            )
            self._recompute_follow_up(str(row["contact_id"]))
            self._conn.commit()
            return True

    def list_follow_up_tasks(
        self, contact_id: str, *, include_done: bool = True,
    ) -> List[Dict[str, Any]]:
        """某客户的跟进任务：未完成在前(按到期升序)，已完成在后(按完成倒序)。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        sql = "SELECT * FROM follow_up_tasks WHERE contact_id=?"
        if not include_done:
            sql += " AND done_at=0"
        sql += " ORDER BY (CASE WHEN done_at=0 THEN 0 ELSE 1 END), " \
               "(CASE WHEN done_at=0 THEN due_at ELSE -done_at END)"
        with self._lock:
            rows = self._conn.execute(sql, (cid,)).fetchall()
        return [{
            "task_id": r["task_id"], "contact_id": r["contact_id"],
            "due_at": r["due_at"] or 0, "note": r["note"] or "",
            "assignee": r["assignee"] or "", "created_at": r["created_at"] or 0,
            "created_by": r["created_by"] or "", "done_at": r["done_at"] or 0,
            "done_by": r["done_by"] or "",
        } for r in rows]

    def count_due_tasks(
        self, *, assignee: Optional[str] = None, now_ts: Optional[int] = None,
    ) -> int:
        """未完成且已到期的任务数（assignee 给定则只数该坐席）。"""
        now = int(now_ts if now_ts is not None else self._now())
        sql = "SELECT COUNT(*) FROM follow_up_tasks WHERE done_at=0 AND due_at>0 AND due_at<=?"
        params: List[Any] = [now]
        if assignee is not None:
            sql += " AND assignee=?"
            params.append(assignee)
        with self._lock:
            return self._conn.execute(sql, params).fetchone()[0]

    def list_open_tasks(
        self, *, assignee: Optional[str] = None, due_before: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """未完成跟进任务（含客户名/渠道），按到期升序。「我的待办」面板用。

        assignee 给定→只看该坐席；due_before 给定→只看到期 <= 该时间。
        """
        sql = (
            "SELECT ft.task_id, ft.contact_id, ft.due_at, ft.note, ft.assignee, "
            "ft.created_by, ft.created_at, c.primary_name AS name, "
            "(SELECT GROUP_CONCAT(DISTINCT ci.channel) FROM channel_identities ci "
            "WHERE ci.contact_id=ft.contact_id) AS channels "
            "FROM follow_up_tasks ft LEFT JOIN contacts c ON c.contact_id=ft.contact_id "
            "WHERE ft.done_at=0"
        )
        params: List[Any] = []
        if assignee is not None:
            sql += " AND ft.assignee=?"
            params.append(assignee)
        if due_before is not None:
            sql += " AND ft.due_at>0 AND ft.due_at<=?"
            params.append(int(due_before))
        sql += (" ORDER BY (CASE WHEN ft.due_at>0 THEN 0 ELSE 1 END), ft.due_at "
                "LIMIT ?")
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{
            "task_id": r["task_id"], "contact_id": r["contact_id"],
            "due_at": r["due_at"] or 0, "note": r["note"] or "",
            "assignee": r["assignee"] or "", "created_by": r["created_by"] or "",
            "created_at": r["created_at"] or 0, "name": r["name"] or "",
            "channels": [c for c in str(r["channels"] or "").split(",") if c],
        } for r in rows]

    def reassign_task(self, task_id: str, assignee: str) -> Optional[str]:
        """改派未完成任务给某坐席，成功返回 contact_id（已完成/不存在→None）。"""
        tid = str(task_id or "").strip()
        if not tid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT contact_id, done_at FROM follow_up_tasks WHERE task_id=?", (tid,)
            ).fetchone()
            if row is None or row["done_at"]:
                return None
            self._conn.execute(
                "UPDATE follow_up_tasks SET assignee=? WHERE task_id=?",
                (str(assignee or ""), tid),
            )
            self._conn.commit()
            return str(row["contact_id"])

    def snooze_task(
        self, task_id: str, *, days: int = 0, due_at: int = 0,
    ) -> Optional[str]:
        """延期未完成任务：给 due_at 直接设；否则按 days 从 max(now, 当前到期) 顺延。

        成功返回 contact_id 并重算缓存（已完成/不存在→None）。
        """
        tid = str(task_id or "").strip()
        if not tid:
            return None
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT contact_id, due_at, done_at FROM follow_up_tasks WHERE task_id=?",
                (tid,),
            ).fetchone()
            if row is None or row["done_at"]:
                return None
            if due_at and int(due_at) > 0:
                new_due = int(due_at)
            else:
                base = max(now, int(row["due_at"] or 0))
                new_due = base + max(0, int(days or 0)) * 86400
            self._conn.execute(
                "UPDATE follow_up_tasks SET due_at=? WHERE task_id=?", (new_due, tid)
            )
            self._recompute_follow_up(str(row["contact_id"]))
            self._conn.commit()
            return str(row["contact_id"])

    # ── Phase 6-6：会话↔CRM 打通 + 仪表盘 ────────────────────
    def overdue_contact_ids(self, now_ts: Optional[int] = None) -> set:
        """有逾期未完成跟进的 contact_id 集合（会话列表红点用，单查缓存列）。"""
        now = int(now_ts if now_ts is not None else self._now())
        with self._lock:
            rows = self._conn.execute(
                "SELECT contact_id FROM contacts WHERE follow_up_at>0 AND follow_up_at<=?",
                (now,),
            ).fetchall()
        return {str(r["contact_id"]) for r in rows}

    def resolve_contacts_by_external(
        self, pairs: List[Tuple[str, str]],
    ) -> Dict[Tuple[str, str], str]:
        """批量把 (channel, external_id) 解析为 contact_id（会话列表关联，避免 N+1）。"""
        exts = list({e for (_c, e) in pairs if e})
        if not exts:
            return {}
        ph = ",".join("?" * len(exts))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT channel, external_id, contact_id FROM channel_identities "
                f"WHERE external_id IN ({ph})", tuple(exts),
            ).fetchall()
        m: Dict[Tuple[str, str], str] = {}
        for r in rows:
            m[(str(r["channel"]), str(r["external_id"]))] = str(r["contact_id"])
        return {p: m[p] for p in pairs if p in m}

    def count_contacts_created_since(self, since_ts: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE created_at>=?", (int(since_ts),)
            ).fetchone()[0]

    def count_events_since(self, event_type: str, since_ts: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM journey_events WHERE event_type=? AND ts>=?",
                (str(event_type), int(since_ts)),
            ).fetchone()[0]

    def count_events_since_multi(
        self, event_types: List[str], since_ts: int,
    ) -> Dict[str, int]:
        """一次查多种事件自某时间起的计数（仪表盘减少往返）。"""
        types = [str(t) for t in (event_types or []) if t]
        if not types:
            return {}
        ph = ",".join("?" * len(types))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT event_type, COUNT(*) AS n FROM journey_events "
                f"WHERE event_type IN ({ph}) AND ts>=? GROUP BY event_type",
                (*types, int(since_ts)),
            ).fetchall()
        return {str(r["event_type"]): int(r["n"]) for r in rows}

    def count_contacts_by_day(self, since_ts: int) -> Dict[str, int]:
        """按本地日期聚合新建客户数（趋势折线）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS n FROM contacts WHERE created_at>=? GROUP BY d",
                (int(since_ts),),
            ).fetchall()
        return {str(r["d"]): int(r["n"]) for r in rows}

    def resolution_stats(
        self, since_ts: int, resolve_event: str = "handoff_sent",
    ) -> List[Dict[str, Any]]:
        """每 journey 解决时长原始数据：首条 msg_in → 其后首个 resolve 事件。

        默认 resolve_event=handoff_sent（本仓库漏斗目标=引流已发）。
        仅含首条 msg_in 在 since_ts 之后的 journey。resolved_ts=None ⇒ 尚未解决。
        聚合（均值/趋势）交由调用方完成，保持单一职责。
        """
        sql = (
            "WITH firstin AS ("
            "  SELECT journey_id, MIN(ts) AS t_in FROM journey_events "
            "  WHERE event_type='msg_in' GROUP BY journey_id"
            ") "
            "SELECT f.journey_id AS jid, f.t_in AS t_in, "
            "  (SELECT MIN(e.ts) FROM journey_events e WHERE e.journey_id=f.journey_id "
            "   AND e.event_type=? AND e.ts>=f.t_in) AS resolved_ts "
            "FROM firstin f WHERE f.t_in >= ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (str(resolve_event), int(since_ts))).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            res = r["resolved_ts"]
            out.append({
                "journey_id": str(r["jid"]),
                "t_in": int(r["t_in"] or 0),
                "resolved_ts": int(res) if res is not None else None,
            })
        return out

    def count_tasks_done_by_day(
        self, done_by: str, since_ts: int,
    ) -> Dict[str, int]:
        """某坐席按本地日期完成的跟进任务数（个人日报：任务产出）。"""
        who = str(done_by or "").strip()
        if not who:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', done_at, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS n FROM follow_up_tasks "
                "WHERE done_by=? AND done_at>=? GROUP BY d",
                (who, int(since_ts)),
            ).fetchall()
        return {str(r["d"]): int(r["n"]) for r in rows if r["d"]}

    def count_events_by_day(self, event_type: str, since_ts: int) -> Dict[str, int]:
        """按本地日期聚合某事件数（趋势折线）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS n FROM journey_events WHERE event_type=? AND ts>=? GROUP BY d",
                (str(event_type), int(since_ts)),
            ).fetchall()
        return {str(r["d"]): int(r["n"]) for r in rows}

    def agent_task_load(self) -> List[Dict[str, Any]]:
        """每个坐席未完成任务数（仪表盘坐席负载）。"""
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT assignee, COUNT(*) AS n, "
                "SUM(CASE WHEN due_at>0 AND due_at<=? THEN 1 ELSE 0 END) AS overdue "
                "FROM follow_up_tasks WHERE done_at=0 GROUP BY assignee ORDER BY n DESC",
                (now,),
            ).fetchall()
        return [{"assignee": str(r["assignee"] or ""), "open": int(r["n"]),
                 "overdue": int(r["overdue"] or 0)} for r in rows]

    # ── Phase 6-4：预设标签库 ────────────────────────────────
    def upsert_tag_library(self, tag: str, *, color: str = "", sort_order: int = 0) -> bool:
        t = str(tag or "").strip()[:40]
        if not t:
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO tag_library (tag, color, sort_order, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tag) DO UPDATE SET "
                "color=excluded.color, sort_order=excluded.sort_order",
                (t, str(color or "")[:16], int(sort_order or 0), self._now()),
            )
            self._conn.commit()
            return True

    def delete_tag_library(self, tag: str) -> bool:
        t = str(tag or "").strip()
        if not t:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM tag_library WHERE tag=?", (t,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_tag_library(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag, color, sort_order FROM tag_library ORDER BY sort_order, tag"
            ).fetchall()
        return [{"tag": str(r["tag"]), "color": str(r["color"] or ""),
                 "sort_order": int(r["sort_order"] or 0)} for r in rows]

    def set_contact_tags(self, contact_id: str, tags: List[str]) -> List[str]:
        """全量替换某客户的标签集合。返回规整去重后的标签列表。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        norm: List[str] = []
        for t in tags or []:
            tt = str(t or "").strip()[:40]
            if tt and tt not in norm:
                norm.append(tt)
        now = self._now()
        with self._lock:
            self._conn.execute("DELETE FROM contact_tags WHERE contact_id=?", (cid,))
            for t in norm:
                self._conn.execute(
                    "INSERT OR IGNORE INTO contact_tags (contact_id, tag, created_at) "
                    "VALUES (?, ?, ?)", (cid, t, now),
                )
            self._conn.commit()
        return norm

    def get_contact_tags(self, contact_id: str) -> List[str]:
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag FROM contact_tags WHERE contact_id=? ORDER BY created_at", (cid,)
            ).fetchall()
        return [str(r["tag"]) for r in rows]

    def get_tags_for_contacts(self, contact_ids: List[str]) -> Dict[str, List[str]]:
        """批量取标签（CRM 列表用，避免 N+1）。"""
        if not contact_ids:
            return {}
        ph = ",".join("?" * len(contact_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT contact_id, tag FROM contact_tags WHERE contact_id IN ({ph}) "
                "ORDER BY created_at", tuple(contact_ids),
            ).fetchall()
        out: Dict[str, List[str]] = {}
        for r in rows:
            out.setdefault(str(r["contact_id"]), []).append(str(r["tag"]))
        return out

    def list_all_tags(self, limit: int = 100) -> List[Dict[str, Any]]:
        """聚合全部使用中的标签 + 计数 + 预设库颜色（标签自动补全/快筛/上色）。

        合并「已使用标签」与「预设库中尚未使用的标签」(count=0)，便于补全选用。
        """
        with self._lock:
            used = self._conn.execute(
                "SELECT ct.tag AS tag, COUNT(*) AS n, "
                "COALESCE(tl.color, '') AS color "
                "FROM contact_tags ct LEFT JOIN tag_library tl ON tl.tag=ct.tag "
                "GROUP BY ct.tag ORDER BY n DESC, ct.tag LIMIT ?", (int(limit),),
            ).fetchall()
            lib = self._conn.execute(
                "SELECT tag, color FROM tag_library "
                "WHERE tag NOT IN (SELECT DISTINCT tag FROM contact_tags) "
                "ORDER BY sort_order, tag"
            ).fetchall()
        out = [{"tag": str(r["tag"]), "count": int(r["n"]),
                "color": str(r["color"] or "")} for r in used]
        out += [{"tag": str(r["tag"]), "count": 0, "color": str(r["color"] or "")}
                for r in lib]
        return out

    def count_due_follow_ups(self, now_ts: Optional[int] = None) -> int:
        now = int(now_ts if now_ts is not None else self._now())
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE follow_up_at > 0 AND follow_up_at <= ?",
                (now,),
            ).fetchone()[0]

    def list_contacts(self, limit: int = 50, offset: int = 0) -> List[Contact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts ORDER BY last_active_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_contact(r) for r in rows]

    def search_contacts(
        self, q: str, limit: int = 50, offset: int = 0
    ):
        """按 contact_id / primary_name / channel_identity.external_id 模糊搜索。

        返回 (contacts_list, total_matched_count)。
        q 为空时退化为 list_contacts。
        """
        if not q:
            rows = self.list_contacts(limit=limit, offset=offset)
            total = self.count_contacts()
            return rows, total
        like = f"%{q}%"
        sql_rows = (
            "SELECT DISTINCT c.* FROM contacts c"
            " LEFT JOIN channel_identities ci ON c.contact_id = ci.contact_id"
            " WHERE c.contact_id = :exact OR c.primary_name LIKE :like OR ci.external_id LIKE :like"
            " ORDER BY c.last_active_at DESC LIMIT :lim OFFSET :off"
        )
        sql_count = (
            "SELECT COUNT(DISTINCT c.contact_id) FROM contacts c"
            " LEFT JOIN channel_identities ci ON c.contact_id = ci.contact_id"
            " WHERE c.contact_id = :exact OR c.primary_name LIKE :like OR ci.external_id LIKE :like"
        )
        params = {"exact": q, "like": like, "lim": limit, "off": offset}
        with self._lock:
            rows = self._conn.execute(sql_rows, params).fetchall()
            total = self._conn.execute(sql_count, {"exact": q, "like": like}).fetchone()[0]
        return [_row_to_contact(r) for r in rows], int(total)

    def count_contacts(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    def list_contacts_overview(
        self,
        *,
        q: str = "",
        stage: str = "",
        has_lead: Optional[bool] = None,
        tag: str = "",
        follow_up: str = "",
        now_ts: Optional[int] = None,
        limit: int = 30,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """CRM 客户列表：单次 JOIN 返回 contact + journey + 渠道 + 留资 + 跟进（避免 N+1）。

        返回 (rows, total)。rows 每项含 contact_id/primary_name/last_active_at/
        funnel_stage/intimacy_score/channels(list)/has_lead(bool)/follow_up_at/tags(list)。
        过滤：q（名称/ID/渠道 external_id）、stage、has_lead、tag（精确）、
        follow_up（"due"=已到期 / "any"=有跟进）。
        """
        now = int(now_ts if now_ts is not None else self._now())
        where: List[str] = []
        params: Dict[str, Any] = {}
        if q:
            where.append(
                "(c.primary_name LIKE :like OR c.contact_id = :exact OR EXISTS("
                "SELECT 1 FROM channel_identities ci2 WHERE ci2.contact_id=c.contact_id "
                "AND ci2.external_id LIKE :like))"
            )
            params["like"] = f"%{q}%"
            params["exact"] = q
        if stage:
            where.append("j.funnel_stage = :stage")
            params["stage"] = stage
        if has_lead is True:
            where.append("EXISTS(SELECT 1 FROM contact_attributes a WHERE a.contact_id=c.contact_id)")
        elif has_lead is False:
            where.append("NOT EXISTS(SELECT 1 FROM contact_attributes a WHERE a.contact_id=c.contact_id)")
        if tag:
            where.append("EXISTS(SELECT 1 FROM contact_tags t WHERE t.contact_id=c.contact_id AND t.tag=:tag)")
            params["tag"] = tag
        if follow_up == "due":
            where.append("c.follow_up_at > 0 AND c.follow_up_at <= :now")
            params["now"] = now
        elif follow_up == "any":
            where.append("c.follow_up_at > 0")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        base = (
            "FROM contacts c LEFT JOIN journeys j ON j.contact_id = c.contact_id" + where_sql
        )
        # 到期跟进优先排前，其次最近活跃
        rows_sql = (
            "SELECT c.contact_id, c.primary_name, c.last_active_at, c.follow_up_at, "
            "j.funnel_stage AS funnel_stage, j.intimacy_score AS intimacy_score, "
            "(SELECT GROUP_CONCAT(DISTINCT ci.channel) FROM channel_identities ci "
            "WHERE ci.contact_id=c.contact_id) AS channels, "
            "EXISTS(SELECT 1 FROM contact_attributes a WHERE a.contact_id=c.contact_id) AS has_lead "
            + base
            + " ORDER BY (CASE WHEN c.follow_up_at>0 AND c.follow_up_at<=:now THEN 0 ELSE 1 END), "
            "c.last_active_at DESC LIMIT :lim OFFSET :off"
        )
        count_sql = "SELECT COUNT(*) " + base
        row_params = dict(params, lim=int(limit), off=int(offset), now=now)
        with self._lock:
            rows = self._conn.execute(rows_sql, row_params).fetchall()
            total = self._conn.execute(count_sql, params).fetchone()[0]
        ids = [r["contact_id"] for r in rows]
        tags_map = self.get_tags_for_contacts(ids)
        out: List[Dict[str, Any]] = []
        for r in rows:
            chans = [c for c in str(r["channels"] or "").split(",") if c]
            out.append({
                "contact_id": r["contact_id"],
                "primary_name": r["primary_name"] or "",
                "last_active_at": r["last_active_at"] or 0,
                "funnel_stage": r["funnel_stage"] or "",
                "intimacy_score": r["intimacy_score"],
                "channels": chans,
                "has_lead": bool(r["has_lead"]),
                "follow_up_at": r["follow_up_at"] or 0,
                "tags": tags_map.get(r["contact_id"], []),
            })
        return out, int(total)

    def count_journeys_by_stage(self, channel: Optional[str] = None) -> Dict[str, int]:
        """按 funnel_stage 聚合 Journey 数量。Funnel Dashboard 基础数据。

        Args:
            channel: 可选，按 channel 过滤（messenger/line/telegram/mobile）。
                None → 所有 Journey；非 None → 仅统计在该 channel 上有
                identity 的 Journey（**COUNT(DISTINCT)**，避免同一 journey
                在 N 个 account 各算一次）。

        Returns: ``{stage: count}``，例如 ``{"INITIAL": 12, "ENGAGED": 8}``。
        """
        if channel is None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT funnel_stage, COUNT(*) AS n FROM journeys GROUP BY funnel_stage"
                ).fetchall()
            return {r["funnel_stage"]: r["n"] for r in rows}
        # ── 带 channel 过滤：JOIN + DISTINCT 防重复计数 ────────────────
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.funnel_stage, COUNT(DISTINCT j.journey_id) AS n "
                "FROM journeys j "
                "JOIN channel_identities ci ON j.contact_id = ci.contact_id "
                "WHERE ci.channel = ? "
                "GROUP BY j.funnel_stage",
                (channel,),
            ).fetchall()
        return {r["funnel_stage"]: r["n"] for r in rows}

    def count_stage_transitions_by_day(
        self,
        *,
        days: int = 30,
        channel: Optional[str] = None,
        now_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """B1：从 journey_events 重放每天每个 stage 的进入数（流量时序）。

        数据源是 ``journey_events.event_type='stage_change'``（含
        ``journey_fsm.transit`` + 各处主动写入）。``silence_decay`` 是后
        台衰减不算业务事件，**刻意排除**。

        Args:
            days: 取过去多少天的窗口（不含未来）。默认 30。
            channel: 可选，仅统计在该 channel 上有 identity 的 journey
                （与 ``count_journeys_by_stage(channel=...)`` 语义一致；
                JOIN + DISTINCT 防同 journey 多 CI 重复计数）。
            now_ts: 测试用，注入"现在"时间戳；缺省取 self._now()。

        Returns:
            按天升序的列表，每项 ``{"day": "YYYY-MM-DD", "by_stage":
            {stage: n}}``。即使某天 0 进入也保留 day 行（前端折线图不
            断点）。

        实现细节：
          - 只用 SQL 做"事件筛选 + 可选 channel JOIN + DISTINCT"，
            payload_json 的 'to' stage 字段在 Python 层 json.loads 提取
            （避免依赖 SQLite JSON1 模块）。
          - 同一 journey 一天内多次进入同一 stage 也只算 1 次
            （DISTINCT，否则 flip-flop 会膨胀流量）。
        """
        import json as _json
        from datetime import datetime, timedelta, timezone

        days = max(1, int(days))
        now = int(now_ts) if now_ts is not None else self._now()
        # 截掉 now 当天的"未到达"那部分：用 UTC 当天 23:59:59
        today_utc = datetime.fromtimestamp(now, tz=timezone.utc).date()
        cutoff_date = today_utc - timedelta(days=days - 1)
        cutoff_ts = int(
            datetime(
                cutoff_date.year, cutoff_date.month, cutoff_date.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )

        # SQL 拉所有候选事件 —— 数据量是 N 天 × 每天 stage 变化次数，
        # 即使一天 10000 次也才 30 万行，足够在内存里处理
        if channel is None:
            sql = (
                "SELECT je.journey_id, je.payload_json, je.ts "
                "FROM journey_events je "
                "WHERE je.event_type='stage_change' AND je.ts >= ?"
            )
            params: tuple = (cutoff_ts,)
        else:
            # JOIN channel_identities 过滤 + DISTINCT 防重复
            sql = (
                "SELECT DISTINCT je.journey_id, je.payload_json, je.ts "
                "FROM journey_events je "
                "JOIN journeys j ON j.journey_id = je.journey_id "
                "JOIN channel_identities ci ON ci.contact_id = j.contact_id "
                "WHERE je.event_type='stage_change' AND je.ts >= ? "
                "AND ci.channel = ?"
            )
            params = (cutoff_ts, channel)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # Python 层：按 (day, to_stage) 聚合，同 (day, journey, stage) 只算 1
        # 用 set 去重
        seen_day_journey_stage: set = set()
        bucket: Dict[str, Dict[str, int]] = {}
        for r in rows:
            try:
                payload = _json.loads(r["payload_json"] or "{}")
                to_stage = str(payload.get("to") or "").strip()
            except Exception:
                continue
            if not to_stage:
                continue
            day = datetime.fromtimestamp(
                int(r["ts"]), tz=timezone.utc,
            ).strftime("%Y-%m-%d")
            key = (day, r["journey_id"], to_stage)
            if key in seen_day_journey_stage:
                continue
            seen_day_journey_stage.add(key)
            bucket.setdefault(day, {})
            bucket[day][to_stage] = bucket[day].get(to_stage, 0) + 1

        # 输出按 day 升序填充（缺失天补空 dict，让前端折线不断）
        out: List[Dict[str, Any]] = []
        for i in range(days):
            d = (cutoff_date + timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"day": d, "by_stage": bucket.get(d, {})})
        return out

    # ── B2: KPI 漏斗告警 ─────────────────────────────────
    def insert_kpi_alert(
        self, *, kind: str, severity: str = "warn",
        message: str = "", detail: Optional[Dict] = None,
        dedup_window_sec: float = 14400.0,
    ) -> Optional[int]:
        """插入 KPI 告警，含去重（同 kind 在窗口内已存在则跳过）。

        Args:
            kind: 告警类型，如 ``kpi_drop_engaged_rate``。
            severity: ``warn`` / ``critical``。
            message: 人可读摘要。
            detail: 附加 JSON 数据（rate_key / today_val / avg_7d 等）。
            dedup_window_sec: 去重窗口，默认 4 小时（3600×4）。

        Returns:
            新插入记录的 id；重复时返回 None。
        """
        since = time.time() - dedup_window_sec
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM kpi_alerts WHERE kind=? AND ts>=? LIMIT 1",
                (kind, since),
            ).fetchone()
            if dup:
                return None
            cur = self._conn.execute(
                "INSERT INTO kpi_alerts (ts, kind, severity, message, detail_json) "
                "VALUES (?,?,?,?,?)",
                (time.time(), kind, severity, message,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_kpi_alerts(
        self,
        limit: int = 50,
        *,
        unacked_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """列出 KPI 告警（降序，最新在前）。"""
        sql = "SELECT * FROM kpi_alerts"
        params: tuple
        if unacked_only:
            sql += " WHERE acked=0"
            params = (limit,)
        else:
            params = (limit,)
        sql += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "severity": r["severity"],
                "message": r["message"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "acked": bool(r["acked"]),
                "acked_at": r["acked_at"],
                "acked_by": r["acked_by"] or "",
            })
        return out

    def ack_kpi_alert(self, alert_id: int, *, acked_by: str = "") -> bool:
        """确认单条 KPI 告警。返回 True 表示成功更新（未确认 → 已确认）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kpi_alerts SET acked=1, acked_at=?, acked_by=? "
                "WHERE id=? AND acked=0",
                (time.time(), acked_by, alert_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def ack_all_kpi_alerts(self, *, acked_by: str = "") -> int:
        """批量确认所有未读 KPI 告警。返回实际更新条数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE kpi_alerts SET acked=1, acked_at=?, acked_by=? WHERE acked=0",
                (time.time(), acked_by),
            )
            self._conn.commit()
        return cur.rowcount

    def count_unacked_kpi_alerts(self) -> int:
        """未确认 KPI 告警数（用于 UI 红点徽章）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM kpi_alerts WHERE acked=0"
            ).fetchone()
        return int(row[0]) if row else 0

    def count_channel_identities_by_channel(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, COUNT(*) AS n FROM channel_identities GROUP BY channel"
            ).fetchall()
        return {r["channel"]: r["n"] for r in rows}

    def count_multi_platform_contacts(self) -> Dict[str, Any]:
        """W3-3L.2：统计跨平台联系人（拥有 2+ 不同 channel 的 contact）。

        返回::

            {
                "multi_platform_contacts": N,
                "by_channel_combo": {"line+messenger": 12, "messenger+telegram": 3, ...},
            }

        设计：
          - 在 Python 层聚合（而非复杂 SQL）：数量级 ≤ 万行时足够快，
            且 SQLite 不支持 ``GROUP_CONCAT`` 内排序，Python 更清晰。
          - 只统计 distinct channel（不同账号的同 channel 算一个）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT contact_id, channel FROM channel_identities"
                " ORDER BY contact_id, channel"
            ).fetchall()

        from collections import defaultdict
        channels_by_contact: Dict[str, set] = defaultdict(set)
        for r in rows:
            channels_by_contact[r["contact_id"]].add(r["channel"])

        combo_count: Dict[str, int] = {}
        multi_n = 0
        for channels in channels_by_contact.values():
            if len(channels) >= 2:
                multi_n += 1
                combo = "+".join(sorted(channels))
                combo_count[combo] = combo_count.get(combo, 0) + 1

        return {
            "multi_platform_contacts": multi_n,
            "by_channel_combo": combo_count,
        }

    # ── 每账号每日 handoff 计数 ───────────────────────────
    def incr_account_handoff_counter(self, account_id: str, day: str) -> int:
        """原子 +1 并返回当日计数值。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO account_handoff_counters (account_id, day, count) VALUES (?, ?, 0) "
                "ON CONFLICT(account_id, day) DO NOTHING",
                (account_id, day),
            )
            self._conn.execute(
                "UPDATE account_handoff_counters SET count = count + 1 "
                "WHERE account_id=? AND day=?",
                (account_id, day),
            )
            row = self._conn.execute(
                "SELECT count FROM account_handoff_counters WHERE account_id=? AND day=?",
                (account_id, day),
            ).fetchone()
            self._conn.commit()
            return int(row["count"]) if row else 0

    def get_account_handoff_counter(self, account_id: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM account_handoff_counters WHERE account_id=? AND day=?",
                (account_id, day),
            ).fetchone()
        return int(row["count"]) if row else 0

    def sum_account_handoff_counters(self, day: str) -> int:
        """全域当日发送总数（供 global cap 判定）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS n FROM account_handoff_counters WHERE day=?",
                (day,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def reset_account_handoff_counter(self, account_id: str, day: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE account_handoff_counters SET count=0 WHERE account_id=? AND day=?",
                (account_id, day),
            )
            self._conn.commit()

    def decr_account_handoff_counter(self, account_id: str, day: str) -> int:
        """原子 -1（不低于 0）并返回新值。"""
        with self._lock:
            self._conn.execute(
                "UPDATE account_handoff_counters SET count = MAX(0, count - 1) "
                "WHERE account_id=? AND day=?",
                (account_id, day),
            )
            row = self._conn.execute(
                "SELECT count FROM account_handoff_counters WHERE account_id=? AND day=?",
                (account_id, day),
            ).fetchone()
            self._conn.commit()
            return int(row["count"]) if row else 0

    # ── ChannelIdentity ────────────────────────────────────
    def ensure_channel_identity(
        self,
        *,
        channel: str,
        account_id: str,
        external_id: str,
        display_name: str = "",
        language_hint: str = "",
        timezone_hint: str = "",
    ) -> Tuple[Contact, ChannelIdentity, bool]:
        """获取或创建 ChannelIdentity，自动建 Contact + Journey。

        返回 (contact, channel_identity, created_new)
        """
        if channel not in VALID_CHANNELS:
            raise ValueError(f"unknown channel: {channel}")

        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channel_identities WHERE channel=? AND account_id=? AND external_id=?",
                (channel, account_id, external_id),
            ).fetchone()
            if row:
                ci = _row_to_ci(row)
                crow = self._conn.execute(
                    "SELECT * FROM contacts WHERE contact_id=?", (ci.contact_id,)
                ).fetchone()
                # 顺便刷新 last_active
                self._conn.execute(
                    "UPDATE contacts SET last_active_at=? WHERE contact_id=?",
                    (now, ci.contact_id),
                )
                self._conn.commit()
                return _row_to_contact(crow), ci, False

            # 新建 Contact + Journey + ChannelIdentity（同一事务）
            contact_id = new_id()
            journey_id = new_id()
            ci_id = new_id()

            self._conn.execute(
                "INSERT INTO contacts (contact_id, primary_name, language_hint, timezone_hint, "
                "country_hint, created_at, last_active_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (contact_id, display_name, language_hint, timezone_hint, "", now, now, ""),
            )
            self._conn.execute(
                "INSERT INTO channel_identities (channel_identity_id, contact_id, channel, "
                "account_id, external_id, direction, linked_at, linked_via, "
                "attribution_confidence, display_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ci_id, contact_id, channel, account_id, external_id, "first_seen",
                 now, "", 1.0, display_name),
            )
            self._conn.execute(
                "INSERT INTO journeys (journey_id, contact_id, persona_id, funnel_stage, "
                "intimacy_score, engagement_score, readiness_score, intimacy_updated_at, "
                "context_snapshot_json, snapshot_refreshed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (journey_id, contact_id, "", STAGE_INITIAL, 0.0, 0.0, 0.0, 0, "", 0, now, now),
            )
            # 落事件
            self._insert_event_nolock(
                journey_id=journey_id,
                event_type="contact_created",
                payload={"channel": channel, "account_id": account_id, "external_id": external_id},
                trace_id="",
                ts=now,
            )
            self._conn.commit()

            contact = Contact(
                contact_id=contact_id,
                primary_name=display_name,
                language_hint=language_hint,
                timezone_hint=timezone_hint,
                created_at=now,
                last_active_at=now,
            )
            ci = ChannelIdentity(
                channel_identity_id=ci_id,
                contact_id=contact_id,
                channel=channel,
                account_id=account_id,
                external_id=external_id,
                direction="first_seen",
                linked_at=now,
                attribution_confidence=1.0,
                display_name=display_name,
            )
            return contact, ci, True

    def get_channel_identity(self, ci_id: str) -> Optional[ChannelIdentity]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channel_identities WHERE channel_identity_id=?", (ci_id,)
            ).fetchone()
        return _row_to_ci(row) if row else None

    def get_ci_by_external(self, channel: str, account_id: str, external_id: str) -> Optional[ChannelIdentity]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channel_identities WHERE channel=? AND account_id=? AND external_id=?",
                (channel, account_id, external_id),
            ).fetchone()
        return _row_to_ci(row) if row else None

    def list_channel_identities_for_contacts(
        self, contact_ids: List[str],
    ) -> Dict[str, List[ChannelIdentity]]:
        """W3-3L.4：批量拉取多个 contact 的所有 ChannelIdentity（单次 SQL）。

        避免 N+1 问题；返回 ``{contact_id: [ChannelIdentity, ...]}``.
        对不存在的 contact_id 返回空列表（不报错）。
        """
        if not contact_ids:
            return {}
        placeholders = ",".join("?" * len(contact_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM channel_identities WHERE contact_id IN ({placeholders})"
                " ORDER BY contact_id, linked_at",
                contact_ids,
            ).fetchall()
        result: Dict[str, List[ChannelIdentity]] = {cid: [] for cid in contact_ids}
        for r in rows:
            ci = _row_to_ci(r)
            result.setdefault(ci.contact_id, []).append(ci)
        return result

    def list_channel_identities_of(self, contact_id: str) -> List[ChannelIdentity]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM channel_identities WHERE contact_id=? ORDER BY linked_at",
                (contact_id,),
            ).fetchall()
        return [_row_to_ci(r) for r in rows]

    def relink_channel_identity(
        self,
        *,
        ci_id: str,
        new_contact_id: str,
        linked_via: str,
        attribution_confidence: float,
        trace_id: str = "",
    ) -> bool:
        """把一个 ChannelIdentity 从原 Contact 迁到目标 Contact（合并核心原语）。

        原子操作：
          1. 读 CI 当前 contact_id (old_contact_id)
          2. 若 old_contact_id 上没别的 CI，把老 Contact 和老 Journey 删除（孤岛回收）
          3. 更新 CI 的 contact_id 为 new_contact_id
          4. 在新 Contact 的 Journey 上追加 merged 事件
        """
        now = self._now()
        with self._lock:
            ci_row = self._conn.execute(
                "SELECT * FROM channel_identities WHERE channel_identity_id=?", (ci_id,)
            ).fetchone()
            if not ci_row:
                return False
            old_contact_id = ci_row["contact_id"]
            if old_contact_id == new_contact_id:
                return False  # noop

            new_journey_row = self._conn.execute(
                "SELECT journey_id FROM journeys WHERE contact_id=?", (new_contact_id,)
            ).fetchone()
            if not new_journey_row:
                raise ValueError(f"target contact has no journey: {new_contact_id}")

            # 统计 old_contact 上还有多少其他 CI
            other_ci_count = self._conn.execute(
                "SELECT COUNT(*) FROM channel_identities WHERE contact_id=? AND channel_identity_id<>?",
                (old_contact_id, ci_id),
            ).fetchone()[0]

            # 迁移 CI
            self._conn.execute(
                "UPDATE channel_identities SET contact_id=?, linked_via=?, "
                "attribution_confidence=?, linked_at=?, direction='linked_from' "
                "WHERE channel_identity_id=?",
                (new_contact_id, linked_via, attribution_confidence, now, ci_id),
            )

            # 老 Contact 已空 → 删除老 Contact + 老 Journey + 老 Journey 上的事件
            if other_ci_count == 0:
                old_journey_row = self._conn.execute(
                    "SELECT * FROM journeys WHERE contact_id=?", (old_contact_id,)
                ).fetchone()
                if old_journey_row:
                    old_journey_id = old_journey_row["journey_id"]
                    # 先把老 journey 的 scores 留痕（被丢弃前的快照），便于事后审计/回滚
                    self._insert_event_nolock(
                        journey_id=new_journey_row["journey_id"],
                        event_type="journey_states_discarded",
                        payload={
                            "from_journey_id": old_journey_id,
                            "stage": old_journey_row["funnel_stage"],
                            "intimacy_score": old_journey_row["intimacy_score"],
                            "engagement_score": old_journey_row["engagement_score"],
                            "readiness_score": old_journey_row["readiness_score"],
                        },
                        trace_id=trace_id,
                        ts=now,
                    )
                    # 老 journey 上的事件搬到新 journey（保留历史）
                    self._conn.execute(
                        "UPDATE journey_events SET journey_id=? WHERE journey_id=?",
                        (new_journey_row["journey_id"], old_journey_id),
                    )
                    self._conn.execute("DELETE FROM journeys WHERE journey_id=?", (old_journey_id,))
                self._conn.execute("DELETE FROM contacts WHERE contact_id=?", (old_contact_id,))

            # 新 Journey 上追加 merge 事件
            self._insert_event_nolock(
                journey_id=new_journey_row["journey_id"],
                event_type="channel_identity_merged",
                payload={
                    "channel_identity_id": ci_id,
                    "from_contact_id": old_contact_id,
                    "linked_via": linked_via,
                    "confidence": attribution_confidence,
                },
                trace_id=trace_id,
                ts=now,
            )
            self._conn.execute(
                "UPDATE journeys SET updated_at=? WHERE journey_id=?",
                (now, new_journey_row["journey_id"]),
            )
            self._conn.execute(
                "UPDATE contacts SET last_active_at=? WHERE contact_id=?",
                (now, new_contact_id),
            )
            self._conn.commit()
            return True

    def split_channel_identity(
        self, *, ci_id: str, display_name: str = "", trace_id: str = "",
    ) -> Optional[str]:
        """把一个 ChannelIdentity 从共享 Contact 拆出，独立成新 Contact + Journey（误并回滚）。

        返回新建的 contact_id；若该 ci 不存在或本就是该 Contact 上的唯一身份
        （拆了没意义），返回 None。

        说明：historical journey_events 按 journey 记录、未按 ci 区分，拆分**不回搬**历史
        事件（无法可靠归属），仅修正身份归属与未来归因；两侧各落一条 split 审计事件。
        """
        now = self._now()
        with self._lock:
            ci_row = self._conn.execute(
                "SELECT * FROM channel_identities WHERE channel_identity_id=?", (ci_id,)
            ).fetchone()
            if not ci_row:
                return None
            old_contact_id = ci_row["contact_id"]
            sibling = self._conn.execute(
                "SELECT COUNT(*) FROM channel_identities "
                "WHERE contact_id=? AND channel_identity_id<>?",
                (old_contact_id, ci_id),
            ).fetchone()[0]
            if sibling == 0:
                return None  # 已是孤岛，无需拆

            new_contact_id = new_id()
            new_journey_id = new_id()
            name = display_name or ci_row["display_name"] or ""
            self._conn.execute(
                "INSERT INTO contacts (contact_id, primary_name, language_hint, timezone_hint, "
                "country_hint, created_at, last_active_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_contact_id, name, "", "", "", now, now, ""),
            )
            self._conn.execute(
                "INSERT INTO journeys (journey_id, contact_id, persona_id, funnel_stage, "
                "intimacy_score, engagement_score, readiness_score, intimacy_updated_at, "
                "context_snapshot_json, snapshot_refreshed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_journey_id, new_contact_id, "", STAGE_INITIAL, 0.0, 0.0, 0.0, 0, "", 0, now, now),
            )
            self._conn.execute(
                "UPDATE channel_identities SET contact_id=?, direction='first_seen', "
                "linked_via='manual_split', attribution_confidence=1.0, linked_at=? "
                "WHERE channel_identity_id=?",
                (new_contact_id, now, ci_id),
            )
            self._insert_event_nolock(
                journey_id=new_journey_id,
                event_type="channel_identity_split",
                payload={"channel_identity_id": ci_id, "from_contact_id": old_contact_id},
                trace_id=trace_id,
                ts=now,
            )
            old_journey_row = self._conn.execute(
                "SELECT journey_id FROM journeys WHERE contact_id=?", (old_contact_id,)
            ).fetchone()
            if old_journey_row:
                self._insert_event_nolock(
                    journey_id=old_journey_row["journey_id"],
                    event_type="channel_identity_split_out",
                    payload={"channel_identity_id": ci_id, "to_contact_id": new_contact_id},
                    trace_id=trace_id,
                    ts=now,
                )
            self._conn.commit()
            return new_contact_id

    # ── Journey ────────────────────────────────────────────
    def get_journey_by_contact(self, contact_id: str) -> Optional[Journey]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM journeys WHERE contact_id=?", (contact_id,)
            ).fetchone()
        return _row_to_journey(row) if row else None

    def get_journey(self, journey_id: str) -> Optional[Journey]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM journeys WHERE journey_id=?", (journey_id,)
            ).fetchone()
        return _row_to_journey(row) if row else None

    def update_journey(self, journey_id: str, *, _touch: bool = True, **fields: Any) -> bool:
        """更新 journey 字段。

        ★ W3-D2.5 加 ``_touch`` 参数：
        - True（默认）：同时更新 ``updated_at = now()``，表示 journey 又活跃了
        - False：**只改给定字段，不动 updated_at**
          关键场景：intimacy refresh / readiness 重算这种"系统计算而非用户交互"
          不应该把 silent_days 重置为 0（否则 reactivation_scheduler 找不到候选）
        """
        allowed = {
            "persona_id", "funnel_stage",
            "intimacy_score", "engagement_score", "readiness_score",
            "intimacy_updated_at", "context_snapshot_json", "snapshot_refreshed_at",
        }
        updates: List[str] = []
        params: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"not updatable: {k}")
            updates.append(f"{k}=?")
            params.append(v)
        if not updates:
            return False
        if _touch:
            updates.append("updated_at=?")
            params.append(self._now())
        params.append(journey_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE journeys SET {', '.join(updates)} WHERE journey_id=?", params
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── Journey Events ─────────────────────────────────────
    def append_event(
        self,
        *,
        journey_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        trace_id: str = "",
    ) -> str:
        ts = self._now()
        with self._lock:
            eid = self._insert_event_nolock(
                journey_id=journey_id,
                event_type=event_type,
                payload=payload or {},
                trace_id=trace_id,
                ts=ts,
            )
            self._conn.commit()
        return eid

    def _insert_event_nolock(
        self,
        *,
        journey_id: str,
        event_type: str,
        payload: Dict[str, Any],
        trace_id: str,
        ts: int,
    ) -> str:
        """⚠️ 调用方必须持有 self._lock 且自己 commit。"""
        eid = new_id()
        self._conn.execute(
            "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, journey_id, trace_id, event_type, json.dumps(payload, ensure_ascii=False), ts),
        )
        return eid

    def list_events(self, journey_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journey_events WHERE journey_id=? ORDER BY ts DESC LIMIT ?",
                (journey_id, limit),
            ).fetchall()
        return self._rows_to_events(rows)

    def list_events_for_journeys(
        self, journey_ids: List[str], limit_per_journey: int = 500,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """W3-3D.2：批量加载多个 journey 的事件，避免 N+1。

        实现：
          - 1 次 SQL 用 ``WHERE journey_id IN (...)`` 拿全量
          - 在内存里按 ``journey_id`` 分组，每组取最新 ``limit_per_journey`` 条
          - 返回 ``{journey_id: events}``，**保证每个传入 jid 都有 key**（即使为 []）
            以便调用方可直接 ``result[jid]`` 不需 ``.get(jid, [])``

        SQLite IN-list 上限通常是 999/32766（依版本）。当 ``len(journey_ids)`` 超
        阈值时分批查询。
        """
        if not journey_ids:
            return {}
        # SQLite SQLITE_MAX_VARIABLE_NUMBER 安全值
        CHUNK = 800
        out: Dict[str, List[Dict[str, Any]]] = {jid: [] for jid in journey_ids}
        with self._lock:
            for i in range(0, len(journey_ids), CHUNK):
                chunk = journey_ids[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT * FROM journey_events WHERE journey_id IN ({placeholders}) "
                    f"ORDER BY journey_id, ts DESC",
                    chunk,
                ).fetchall()
                for r in rows:
                    jid = r["journey_id"]
                    bucket = out.get(jid)
                    if bucket is None:
                        # journey_id 不在请求列表中（不应发生，防御）
                        continue
                    if len(bucket) < limit_per_journey:
                        bucket.append(self._row_to_event(r))
        return out

    @staticmethod
    def _row_to_event(row) -> Dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return {
            "event_id": row["event_id"],
            "journey_id": row["journey_id"],
            "trace_id": row["trace_id"],
            "event_type": row["event_type"],
            "payload": payload,
            "ts": row["ts"],
        }

    def _rows_to_events(self, rows) -> List[Dict[str, Any]]:
        return [self._row_to_event(r) for r in rows]

    # ── Draft Log（W3-3G：reunion 草稿生成→发送→评估闭环） ──
    def record_draft(
        self,
        *,
        journey_id: str,
        contact_id: str = "",
        draft_text: str,
        draft_lang: str = "zh",
        intimacy_score: float = 0.0,
        silent_days: int = 0,
        funnel_stage: str = "",
        prompt_variant: str = "v1",
        prompt_snapshot_hash: str = "",
    ) -> str:
        """记录一次 draft 生成。返回 draft_id。

        ``prompt_snapshot_hash``（W3-3I.5）：prompt 文本的 stable hash，
        留空则自动从 ``draft_text`` 派生（兼容旧调用方）。建议传入
        ``hash_prompt(prompt)``，因为 draft_text 是 AI 输出而非 prompt 本身。
        """
        did = new_id()
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO draft_log (draft_id, journey_id, contact_id, draft_text, "
                "draft_lang, intimacy_score, silent_days, funnel_stage, prompt_variant, "
                "prompt_snapshot_hash, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (did, journey_id, contact_id, draft_text, draft_lang,
                 float(intimacy_score), int(silent_days), funnel_stage,
                 prompt_variant, prompt_snapshot_hash, now),
            )
            self._conn.commit()
        return did

    def latest_unsent_draft_for(self, journey_id: str) -> Optional[Dict[str, Any]]:
        """取该 journey 最新一条还未 mark-sent 的 draft（用于 /mark-sent 联动）。

        同秒插入的多条用 ROWID 作 tiebreaker，保证「后写的赢」。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM draft_log WHERE journey_id=? AND sent_ts IS NULL "
                "ORDER BY generated_at DESC, ROWID DESC LIMIT 1",
                (journey_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_draft_sent(
        self, draft_id: str, *, sent_by: str = "",
    ) -> bool:
        """把 draft 置为已发。已发 / 不存在的返回 False。"""
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE draft_log SET sent_ts=?, sent_by=? "
                "WHERE draft_id=? AND sent_ts IS NULL",
                (now, sent_by, draft_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def mark_draft_unsent(self, draft_id: str) -> bool:
        """W3-3H.5：撤回 draft 的「已发」状态（运营点错时用）。

        - 仅清 sent_ts / sent_by；保留 draft_text 等
        - **拒绝撤回**已评估过的 draft（success_eval_ts 非空）：因为 success
          已写入 stats，撤回会导致历史分母不一致
        - 已评估过 / 没发过 / 不存在 → False
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE draft_log SET sent_ts=NULL, sent_by=NULL "
                "WHERE draft_id=? AND sent_ts IS NOT NULL "
                "AND success_eval_ts IS NULL",
                (draft_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def find_first_msg_in_window(
        self,
        journey_id: str,
        *,
        after_ts: int,
        before_ts: int,
    ) -> Optional[Dict[str, Any]]:
        """W3-3H.6：在 ``(after_ts, before_ts]`` 区间找该 journey 第一条 msg_in。

        给 DraftSuccessEvaluator 用，避免它直接访问 ``_lock`` / ``_conn``。

        返回 ``{event_id, ts}`` 或 ``None``。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT event_id, ts FROM journey_events "
                "WHERE journey_id=? AND event_type='msg_in' "
                "AND ts > ? AND ts <= ? "
                "ORDER BY ts ASC LIMIT 1",
                (journey_id, int(after_ts), int(before_ts)),
            ).fetchone()
        return dict(row) if row else None

    def list_drafts_pending_eval(
        self, *, window_secs: int = 86400, now_ts: Optional[int] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """已发但还没评估，且评估窗口已到期的草稿。"""
        now = int(now_ts) if now_ts is not None else self._now()
        deadline = now - int(window_secs)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM draft_log "
                "WHERE sent_ts IS NOT NULL AND success_eval_ts IS NULL "
                "AND sent_ts <= ? "
                "ORDER BY sent_ts ASC LIMIT ?",
                (deadline, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def eval_draft_success(
        self, draft_id: str, *, success: bool,
        reply_event_id: str = "", now_ts: Optional[int] = None,
    ) -> bool:
        """写评估结果。已评估过的返回 False（幂等）。"""
        now = int(now_ts) if now_ts is not None else self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE draft_log SET success_eval_ts=?, success=?, reply_event_id=? "
                "WHERE draft_id=? AND success_eval_ts IS NULL",
                (now, 1 if success else 0, reply_event_id, draft_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _wilson_lower(success: int, total: int, *, z: float = 1.96) -> Optional[float]:
        """Wilson score 区间下界（95% CI by default）。

        比朴素 success/total 更稳健 —— 3/3=100% 给出虚高，5/10=50% 才有意义。
        Reference: https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
        """
        if total <= 0:
            return None
        p = success / total
        denom = 1.0 + z * z / total
        center = (p + z * z / (2 * total)) / denom
        margin = (
            z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
        ) / denom
        return max(0.0, round(center - margin, 3))

    @staticmethod
    def _wilson_upper(success: int, total: int, *, z: float = 1.96) -> Optional[float]:
        """Wilson score 区间上界（W3-3I.1：判断 A/B 显著性时和 _wilson_lower 配对用）。"""
        if total <= 0:
            return None
        p = success / total
        denom = 1.0 + z * z / total
        center = (p + z * z / (2 * total)) / denom
        margin = (
            z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
        ) / denom
        return min(1.0, round(center + margin, 3))

    @classmethod
    def pick_winning_variant(
        cls, by_variant: Dict[str, Dict[str, Any]],
        *, min_evaluated: int = 10,
    ) -> Optional[Dict[str, Any]]:
        """W3-3I.1：从 ``by_variant`` 桶里挑显著优胜者。

        判定：
          - 至少有 2 个 variant 各自评估数 ≥ ``min_evaluated``
          - 「最强」variant 的 95% CI 下界 > 「最弱」variant 的 95% CI 上界
            （即两区间不重叠 → 差异显著）
          - 返回 ``{winner, winner_rate, runner_up, runner_up_rate, gap}``，
            否则返回 ``None``（数据不足或差异未显著）

        阈值 ``min_evaluated=10`` 是工程权衡：太低（< 5）噪声大，太高
        （> 30）等太久；10 在 50/50 路由下大约对应每 variant 累计 30+
        样本就能决断。
        """
        eligible = {
            k: v for k, v in by_variant.items()
            if isinstance(v, dict) and int(v.get("evaluated", 0)) >= min_evaluated
            and v.get("success_rate") is not None
            and v.get("success_rate_lower") is not None
        }
        if len(eligible) < 2:
            return None
        # 按点估计排：最强 vs 最弱
        ranked = sorted(
            eligible.items(),
            key=lambda kv: kv[1]["success_rate"],
            reverse=True,
        )
        winner_id, winner = ranked[0]
        loser_id, loser = ranked[-1]
        loser_upper = cls._wilson_upper(
            int(loser["success"]), int(loser["evaluated"]),
        )
        if loser_upper is None:
            return None
        if winner["success_rate_lower"] > loser_upper:
            return {
                "winner": winner_id,
                "winner_rate": winner["success_rate"],
                "winner_evaluated": winner["evaluated"],
                "runner_up": loser_id,
                "runner_up_rate": loser["success_rate"],
                "runner_up_evaluated": loser["evaluated"],
                "gap_pct": round(
                    (winner["success_rate"] - loser["success_rate"]) * 100, 1,
                ),
            }
        return None

    @staticmethod
    def _silent_band(days: int) -> str:
        """W3-3H.4：把 silent_days 分到 4 桶（运营粒度）。"""
        d = int(days or 0)
        if d < 7:
            return "0-6d"
        if d < 14:
            return "7-13d"
        if d < 30:
            return "14-29d"
        if d < 60:
            return "30-59d"
        return "60d+"

    def draft_quality_stats(
        self, *, days: int = 7, now_ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """汇总最近 N 天的 draft 质量。

        返回 {window_days, generated, sent, evaluated, success, success_rate,
              success_rate_lower (Wilson 95% CI 下界，更稳健),
              by_lang: {zh: {sent, evaluated, success, success_rate, success_rate_lower}, ...},
              by_variant: {...}, by_silent_band: {...}}
        """
        now = int(now_ts) if now_ts is not None else self._now()
        cutoff = now - int(days) * 86400
        with self._lock:
            # 一次性把窗口内所有 draft 拿出来，内存里分组（数据量小）
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM draft_log WHERE generated_at >= ?", (cutoff,),
            ).fetchall()]
        generated = len(rows)
        sent_rows = [r for r in rows if r.get("sent_ts")]
        eval_rows = [r for r in rows if r.get("success_eval_ts")]
        success_rows = [r for r in eval_rows if r.get("success")]

        def _group(key_fn):
            """key_fn: row → bucket key（str 或 None）。"""
            buckets: Dict[str, Dict[str, Any]] = {}
            for r in sent_rows:
                k = key_fn(r) or "_unknown"
                b = buckets.setdefault(
                    k, {"sent": 0, "evaluated": 0, "success": 0},
                )
                b["sent"] += 1
                if r.get("success_eval_ts"):
                    b["evaluated"] += 1
                    if r.get("success"):
                        b["success"] += 1
            for k, b in buckets.items():
                b["success_rate"] = (
                    round(b["success"] / b["evaluated"], 3)
                    if b["evaluated"] else None
                )
                b["success_rate_lower"] = self._wilson_lower(
                    b["success"], b["evaluated"],
                )
            return buckets

        return {
            "window_days": days,
            "generated": generated,
            "sent": len(sent_rows),
            "evaluated": len(eval_rows),
            "success": len(success_rows),
            "success_rate": (
                round(len(success_rows) / len(eval_rows), 3)
                if eval_rows else None
            ),
            "success_rate_lower": self._wilson_lower(
                len(success_rows), len(eval_rows),
            ),
            "by_lang": _group(lambda r: r.get("draft_lang")),
            "by_variant": _group(lambda r: r.get("prompt_variant")),
            "by_silent_band": _group(
                lambda r: self._silent_band(r.get("silent_days", 0)),
            ),
        }

    def draft_quality_by_hash(
        self, *, days: int = 7, now_ts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """W3-3J.4：按 prompt_snapshot_hash 聚合 draft 质量。

        返回 ``{hash16: {sent, evaluated, success, success_rate, success_rate_lower}}``。
        hash == "" 的行（旧数据，无 hash）归入 "_legacy" 桶。
        """
        now = int(now_ts) if now_ts is not None else self._now()
        cutoff = now - int(days) * 86400
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM draft_log WHERE generated_at >= ? AND sent_ts IS NOT NULL",
                (cutoff,),
            ).fetchall()]
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            k = (r.get("prompt_snapshot_hash") or "").strip() or "_legacy"
            b = buckets.setdefault(k, {"sent": 0, "evaluated": 0, "success": 0})
            b["sent"] += 1
            if r.get("success_eval_ts"):
                b["evaluated"] += 1
                if r.get("success"):
                    b["success"] += 1
        for b in buckets.values():
            b["success_rate"] = (
                round(b["success"] / b["evaluated"], 3)
                if b["evaluated"] else None
            )
            b["success_rate_lower"] = self._wilson_lower(b["success"], b["evaluated"])
        return buckets

    def compute_reactivation_response_stats(
        self, *, window_sec: int = 86400, response_window_sec: int = 86400,
    ) -> Dict[str, Any]:
        """W2-D6.1 + D7.2：算"最近 window_sec 内主动唤醒后是否被回复"。

        - 取最近 window_sec 内每个 journey 最新的 reactivation_sent 时间
        - 若该 journey 在 reactivation_sent 之后 response_window_sec 内有 msg_in → 计为已回复
        - ★ D7.2：同时按 intimacy_score 分桶（high>=70 / mid 40-70 / low<40）
        - 返回 {sent, responded, response_rate_pct, by_intimacy: {high/mid/low}, computed_at}
        """
        import time as _t
        now = int(_t.time())
        cutoff = now - int(max(60, window_sec))

        def _bucket_of(score: float) -> str:
            if score >= 70:
                return "high"
            if score >= 40:
                return "mid"
            return "low"

        with self._lock:
            # 一次 join 拿到 reactivation_sent 时间 + 当前 intimacy_score
            sent_rows = self._conn.execute(
                """SELECT je.journey_id AS journey_id,
                          MAX(je.ts) AS react_ts,
                          j.intimacy_score AS intimacy_score
                   FROM journey_events je
                   LEFT JOIN journeys j ON j.journey_id = je.journey_id
                   WHERE je.event_type='reactivation_sent' AND je.ts >= ?
                   GROUP BY je.journey_id""",
                (cutoff,),
            ).fetchall()
            sent = len(sent_rows)
            responded = 0
            by_intimacy = {
                "high": {"sent": 0, "responded": 0},
                "mid": {"sent": 0, "responded": 0},
                "low": {"sent": 0, "responded": 0},
            }
            for row in sent_rows:
                jid = row["journey_id"]
                react_ts = int(row["react_ts"] or 0)
                if react_ts <= 0:
                    continue
                bucket = _bucket_of(float(row["intimacy_score"] or 0))
                by_intimacy[bucket]["sent"] += 1
                got = self._conn.execute(
                    """SELECT 1 FROM journey_events
                       WHERE journey_id=? AND event_type='msg_in'
                         AND ts > ? AND ts <= ?
                       LIMIT 1""",
                    (jid, react_ts, react_ts + int(response_window_sec)),
                ).fetchone()
                if got is not None:
                    responded += 1
                    by_intimacy[bucket]["responded"] += 1
        # 算每桶的 rate
        for k, v in by_intimacy.items():
            v["rate_pct"] = round(
                (v["responded"] / v["sent"] * 100.0) if v["sent"] else 0.0, 1,
            )
        rate = (responded / sent * 100.0) if sent > 0 else 0.0
        return {
            "sent": sent,
            "responded": responded,
            "response_rate_pct": round(rate, 1),
            "by_intimacy": by_intimacy,
            "window_hours": round(window_sec / 3600.0, 1),
            "response_window_hours": round(response_window_sec / 3600.0, 1),
            "computed_at": now,
        }

    def has_event_of_type(self, journey_id: str, event_type: str) -> bool:
        """O(1) 检查：某 journey 是否已经落过特定类型的事件。

        优于 list_events + 遍历——事件多时防重放判定不会丢信号。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM journey_events WHERE journey_id=? AND event_type=? LIMIT 1",
                (journey_id, event_type),
            ).fetchone()
        return row is not None

    # ── HandoffToken 原语（Service 层再封一层） ─────────────
    def insert_token(self, tok: HandoffToken) -> bool:
        """尝试插入 token，成功返回 True，PK 冲突返回 False（上层应重试）。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO handoff_tokens (token, issued_from_ci_id, issued_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tok.token, tok.issued_from_ci_id, tok.issued_at, tok.expires_at),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def get_token(self, token: str) -> Optional[HandoffToken]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM handoff_tokens WHERE token=?", (token,)
            ).fetchone()
        return _row_to_token(row) if row else None

    def consume_token(self, token: str, *, consumed_by_ci_id: str) -> Optional[HandoffToken]:
        """原子消费：仅当未消费、未撤销、未过期才 UPDATE 成功。

        成功返回更新后的 token；已消费/过期/不存在返回 None。
        """
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM handoff_tokens WHERE token=?", (token,)
            ).fetchone()
            if not row:
                return None
            if row["consumed_by_ci_id"] or row["revoked_reason"]:
                return None
            if now >= row["expires_at"]:
                return None
            self._conn.execute(
                "UPDATE handoff_tokens SET consumed_by_ci_id=?, consumed_at=? WHERE token=? "
                "AND consumed_by_ci_id='' AND revoked_reason='' AND expires_at>?",
                (consumed_by_ci_id, now, token, now),
            )
            self._conn.commit()
            # 取最新
            new_row = self._conn.execute(
                "SELECT * FROM handoff_tokens WHERE token=?", (token,)
            ).fetchone()
        return _row_to_token(new_row) if new_row else None

    def revoke_token(self, token: str, reason: str = "manual") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE handoff_tokens SET revoked_reason=? WHERE token=? AND consumed_by_ci_id=''",
                (reason or "manual", token),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_active_tokens_issued_from(self, ci_id: str) -> List[HandoffToken]:
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM handoff_tokens WHERE issued_from_ci_id=? "
                "AND consumed_by_ci_id='' AND revoked_reason='' AND expires_at>? "
                "ORDER BY issued_at DESC",
                (ci_id, now),
            ).fetchall()
        return [_row_to_token(r) for r in rows]

    def list_all_active_tokens_with_ci(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """一次 JOIN 读出所有未消费/未撤销/未过期的 token + 对应 ChannelIdentity + Contact。

        避免 MergeService 的 N+1 查询。返回字典，字段：
          - token / issued_at / expires_at
          - ci: {channel_identity_id, contact_id, channel, account_id, external_id, display_name}
          - contact: {primary_name, language_hint, timezone_hint}
        """
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.token, t.issued_at, t.expires_at, t.issued_from_ci_id, "
                "       ci.channel_identity_id, ci.contact_id, ci.channel, ci.account_id, "
                "       ci.external_id, ci.display_name, "
                "       c.primary_name, c.language_hint, c.timezone_hint "
                "FROM handoff_tokens t "
                "JOIN channel_identities ci ON ci.channel_identity_id = t.issued_from_ci_id "
                "JOIN contacts c ON c.contact_id = ci.contact_id "
                "WHERE t.consumed_by_ci_id='' AND t.revoked_reason='' AND t.expires_at>? "
                "ORDER BY t.issued_at DESC LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_review(self, review_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM merge_review_queue WHERE review_id=?", (review_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "review_id": row["review_id"],
            "candidate_ci_id": row["candidate_ci_id"],
            "target_contact_id": row["target_contact_id"],
            "confidence": row["confidence"],
            "breakdown": json.loads(row["breakdown_json"] or "{}"),
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolved_by": row["resolved_by"],
        }

    # ── Merge 人工审核队列 ─────────────────────────────────
    def enqueue_merge_review(
        self,
        *,
        candidate_ci_id: str,
        target_contact_id: str,
        confidence: float,
        breakdown: Dict[str, float],
    ) -> str:
        rid = new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO merge_review_queue (review_id, candidate_ci_id, target_contact_id, "
                "confidence, breakdown_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (rid, candidate_ci_id, target_contact_id, confidence,
                 json.dumps(breakdown, ensure_ascii=False), self._now()),
            )
            self._conn.commit()
        return rid

    def list_pending_reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM merge_review_queue WHERE status='pending' "
                "ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{
            "review_id": r["review_id"],
            "candidate_ci_id": r["candidate_ci_id"],
            "target_contact_id": r["target_contact_id"],
            "confidence": r["confidence"],
            "breakdown": json.loads(r["breakdown_json"] or "{}"),
            "created_at": r["created_at"],
        } for r in rows]

    def resolve_review(self, review_id: str, *, status: str, resolved_by: str = "") -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError(f"bad status: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE merge_review_queue SET status=?, resolved_at=?, resolved_by=? "
                "WHERE review_id=? AND status='pending'",
                (status, self._now(), resolved_by, review_id),
            )
            self._conn.commit()
            return cur.rowcount > 0


# ── row → dataclass helpers ─────────────────────────────────
def _row_to_contact(row: sqlite3.Row) -> Contact:
    return Contact(
        contact_id=row["contact_id"],
        primary_name=row["primary_name"],
        language_hint=row["language_hint"],
        timezone_hint=row["timezone_hint"],
        country_hint=row["country_hint"],
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        notes=row["notes"],
        follow_up_at=(row["follow_up_at"] if "follow_up_at" in row.keys() else 0) or 0,
    )


def _row_to_ci(row: sqlite3.Row) -> ChannelIdentity:
    return ChannelIdentity(
        channel_identity_id=row["channel_identity_id"],
        contact_id=row["contact_id"],
        channel=row["channel"],
        account_id=row["account_id"],
        external_id=row["external_id"],
        direction=row["direction"],
        linked_at=row["linked_at"],
        linked_via=row["linked_via"],
        attribution_confidence=row["attribution_confidence"],
        display_name=row["display_name"],
    )


def _row_to_journey(row: sqlite3.Row) -> Journey:
    return Journey(
        journey_id=row["journey_id"],
        contact_id=row["contact_id"],
        persona_id=row["persona_id"],
        funnel_stage=row["funnel_stage"],
        intimacy_score=row["intimacy_score"],
        engagement_score=row["engagement_score"],
        readiness_score=row["readiness_score"],
        intimacy_updated_at=row["intimacy_updated_at"],
        context_snapshot_json=row["context_snapshot_json"],
        snapshot_refreshed_at=row["snapshot_refreshed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_token(row: sqlite3.Row) -> HandoffToken:
    return HandoffToken(
        token=row["token"],
        issued_from_ci_id=row["issued_from_ci_id"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_by_ci_id=row["consumed_by_ci_id"],
        consumed_at=row["consumed_at"],
        revoked_reason=row["revoked_reason"],
    )
