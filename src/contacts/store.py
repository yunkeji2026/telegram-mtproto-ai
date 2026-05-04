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

CREATE TABLE IF NOT EXISTS account_handoff_counters (
    account_id  TEXT NOT NULL,
    day         TEXT NOT NULL,      -- YYYY-MM-DD (UTC)
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day)
);
CREATE INDEX IF NOT EXISTS idx_counters_day ON account_handoff_counters(day);
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

    def count_journeys_by_stage(self) -> Dict[str, int]:
        """按 funnel_stage 聚合 Journey 数量。Funnel Dashboard 基础数据。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT funnel_stage, COUNT(*) AS n FROM journeys GROUP BY funnel_stage"
            ).fetchall()
        return {r["funnel_stage"]: r["n"] for r in rows}

    def count_channel_identities_by_channel(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, COUNT(*) AS n FROM channel_identities GROUP BY channel"
            ).fetchall()
        return {r["channel"]: r["n"] for r in rows}

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
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "event_id": r["event_id"],
                "journey_id": r["journey_id"],
                "trace_id": r["trace_id"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "ts": r["ts"],
            })
        return out

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
