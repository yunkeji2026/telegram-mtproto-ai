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


HANDOFF_STATUSES = {
    "new",
    "assigned",
    "in_progress",
    "line_sent",
    "line_added",
    "converted",
    "lost",
    "paused",
}
LINE_HANDOFF_STATUSES = {
    "not_sent",
    "sent",
    "added",
    "accepted",
    "engaged",
    "converted",
    "lost",
}
HANDOFF_PRIORITIES = {"", "low", "mid", "high", "urgent"}


_DDL = """
CREATE TABLE IF NOT EXISTS messenger_rpa_chat_state (
    chat_key            TEXT PRIMARY KEY,
    chat_name           TEXT DEFAULT '',
    last_peer_text      TEXT DEFAULT '',
    last_peer_fp        TEXT DEFAULT '',
    last_peer_kind      TEXT DEFAULT '',
    last_reply          TEXT DEFAULT '',
    last_screen_sha256  TEXT DEFAULT '',
    last_sent_at        REAL DEFAULT 0,
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

-- P0-4：self_skip cooldown 持久化。让 runaway_guard / wrong-chat-detect /
-- self-sent-detect 触发的冷却跨进程重启生效（之前仅在内存 dict 里）。
-- norm_key 来自 _self_skip_norm_key()（CJK 取前 2 字、ASCII 取前 8 字），
-- until_ts 是 epoch（time.time()）— **不是** monotonic，加载时再转回 monotonic。
CREATE TABLE IF NOT EXISTS messenger_rpa_self_skip (
    norm_key   TEXT PRIMARY KEY,
    until_ts   REAL NOT NULL,
    reason     TEXT DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messenger_self_skip_until
    ON messenger_rpa_self_skip(until_ts);

-- P4-7：chat 级信用分（credit 0-100，起始 100；每次 reject/escalation 扣分）
CREATE TABLE IF NOT EXISTS messenger_rpa_chat_credit (
    chat_key   TEXT PRIMARY KEY,
    credit     INTEGER NOT NULL DEFAULT 100,
    updated_at REAL NOT NULL DEFAULT 0,
    last_reason TEXT DEFAULT ''
);

-- 人工客服交接状态：给运营台保存负责人、LINE 进度、备注和结果
CREATE TABLE IF NOT EXISTS messenger_rpa_handoffs (
    chat_key         TEXT PRIMARY KEY,
    account_id       TEXT DEFAULT '',
    owner            TEXT DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'new',
    line_status      TEXT NOT NULL DEFAULT 'not_sent',
    priority         TEXT DEFAULT '',
    outcome          TEXT DEFAULT '',
    notes            TEXT DEFAULT '',
    next_followup_at REAL DEFAULT 0,
    updated_by       TEXT DEFAULT '',
    updated_at       REAL NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messenger_handoffs_status
    ON messenger_rpa_handoffs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messenger_handoffs_owner
    ON messenger_rpa_handoffs(owner, updated_at DESC);

-- 自动人设策略运行服务：账号、人设、会话状态、入站消息、运行队列。
CREATE TABLE IF NOT EXISTS messenger_rpa_strategy_accounts (
    account_id               TEXT PRIMARY KEY,
    label                    TEXT DEFAULT '',
    status                   TEXT NOT NULL DEFAULT 'active',
    supported_languages_json TEXT NOT NULL DEFAULT '[]',
    supported_customer_types_json TEXT NOT NULL DEFAULT '[]',
    persona_ids_json         TEXT NOT NULL DEFAULT '[]',
    health_score             REAL NOT NULL DEFAULT 100,
    current_load             INTEGER NOT NULL DEFAULT 0,
    daily_send_count         INTEGER NOT NULL DEFAULT 0,
    max_daily_send           INTEGER NOT NULL DEFAULT 200,
    metadata_json            TEXT NOT NULL DEFAULT '{}',
    created_at               REAL NOT NULL DEFAULT 0,
    updated_at               REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messenger_rpa_personas (
    persona_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    language         TEXT DEFAULT 'auto',
    customer_type    TEXT DEFAULT '',
    facts_json       TEXT NOT NULL DEFAULT '[]',
    persona_json     TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       REAL NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messenger_rpa_conversation_states (
    customer_id          TEXT PRIMARY KEY,
    chat_key             TEXT DEFAULT '',
    account_id           TEXT DEFAULT '',
    persona_id           TEXT DEFAULT '',
    customer_language    TEXT DEFAULT '',
    customer_type        TEXT DEFAULT '',
    stage                TEXT NOT NULL DEFAULT 'new_lead',
    memory_summary       TEXT DEFAULT '',
    recent_topics_json   TEXT NOT NULL DEFAULT '[]',
    used_persona_facts_json TEXT NOT NULL DEFAULT '[]',
    metadata_json        TEXT NOT NULL DEFAULT '{}',
    last_message_at      REAL DEFAULT 0,
    created_at           REAL NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messenger_conv_chat
    ON messenger_rpa_conversation_states(chat_key);
CREATE INDEX IF NOT EXISTS idx_messenger_conv_account_stage
    ON messenger_rpa_conversation_states(account_id, stage, updated_at DESC);

CREATE TABLE IF NOT EXISTS messenger_rpa_incoming_messages (
    message_id       TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    chat_key         TEXT DEFAULT '',
    text             TEXT NOT NULL,
    language         TEXT DEFAULT '',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    received_at      REAL NOT NULL,
    created_at       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messenger_incoming_customer_ts
    ON messenger_rpa_incoming_messages(customer_id, received_at DESC);

CREATE TABLE IF NOT EXISTS messenger_rpa_auto_run_jobs (
    job_id              TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL,
    incoming_message_id TEXT NOT NULL,
    account_id          TEXT DEFAULT '',
    persona_id          TEXT DEFAULT '',
    stage               TEXT DEFAULT '',
    strategy_json       TEXT NOT NULL DEFAULT '{}',
    priority            INTEGER NOT NULL DEFAULT 50,
    status              TEXT NOT NULL DEFAULT 'pending',
    run_after           REAL NOT NULL DEFAULT 0,
    locked_by           TEXT DEFAULT '',
    locked_at           REAL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT DEFAULT '',
    created_at          REAL NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messenger_jobs_status_run_after
    ON messenger_rpa_auto_run_jobs(status, run_after, priority DESC);
CREATE INDEX IF NOT EXISTS idx_messenger_jobs_customer
    ON messenger_rpa_auto_run_jobs(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messenger_rpa_chat_runs (
    run_id          TEXT PRIMARY KEY,
    job_id          TEXT DEFAULT '',
    customer_id     TEXT NOT NULL,
    account_id      TEXT DEFAULT '',
    persona_id      TEXT DEFAULT '',
    previous_stage  TEXT DEFAULT '',
    next_stage      TEXT DEFAULT '',
    strategy_json   TEXT NOT NULL DEFAULT '{}',
    reply_text      TEXT DEFAULT '',
    status          TEXT NOT NULL,
    error           TEXT DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messenger_chat_runs_customer_ts
    ON messenger_rpa_chat_runs(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messenger_rpa_strategy_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT DEFAULT '',
    action      TEXT NOT NULL,
    target_type TEXT DEFAULT '',
    target_id   TEXT DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json  TEXT NOT NULL DEFAULT '{}',
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messenger_strategy_audit_ts
    ON messenger_rpa_strategy_audit(ts DESC);

-- B2: 运营手动指定的"chat → reply_profile" 绑定
-- 优先级高于 reply_profiles 配置的 match_names / customer_type 自动匹配
-- 也高于 conversation_states.persona_id（LLM 自动推断的）
-- 这是给运营在 Web 后台"为每个 chat 单独指定人设"+"批量为账号下所有 chat 设人设"用的
CREATE TABLE IF NOT EXISTS messenger_rpa_chat_persona_overrides (
    chat_name        TEXT NOT NULL,
    account_id       TEXT NOT NULL DEFAULT '',  -- 区分多账号下同名 chat
    reply_profile_id TEXT NOT NULL,
    bound_by         TEXT DEFAULT '',           -- 'web_admin' / account_id / system
    bound_at         REAL NOT NULL DEFAULT 0,
    notes            TEXT DEFAULT '',
    PRIMARY KEY (chat_name, account_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_persona_overrides_account
    ON messenger_rpa_chat_persona_overrides(account_id, reply_profile_id);
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
                # ★ W2-D1：陪护模式延迟发送队列。status='deferred' + deferred_until=ts
                # 表示这条 reply 被 safe_skip 后等到时间到自动发送（而非人工审批）。
                # defer_reason 记降级原因（quiet_hours / daily_cap / min_gap / credit_low / pacing）。
                "ALTER TABLE messenger_rpa_approvals "
                "ADD COLUMN deferred_until REAL DEFAULT 0",
                "ALTER TABLE messenger_rpa_approvals "
                "ADD COLUMN defer_reason TEXT DEFAULT ''",
                # ★ W2-D2：row 级 staleness — 不同类型 defer 阈值不同
                # pacing: 60s（窗口窄，超时就丢）；quiet_hours: 21600s（6h）；
                # 0 = 用 drain 默认值。
                "ALTER TABLE messenger_rpa_approvals "
                "ADD COLUMN defer_staleness_sec REAL DEFAULT 0",
                "ALTER TABLE messenger_rpa_chat_state "
                "ADD COLUMN last_sent_at REAL DEFAULT 0",
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

    def list_chat_states(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent chat state rows for operator dashboards."""
        lim = max(1, min(int(limit or 100), 1000))
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_chat_state "
                "ORDER BY updated_at DESC LIMIT ?",
                (lim,),
            ).fetchall()
        return [dict(r) for r in rows]

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
        last_sent_at: Optional[float] = None,
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
        if last_sent_at is not None:
            merged["last_sent_at"] = float(last_sent_at)
        merged["chat_key"] = chat_key
        merged["updated_at"] = now

        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_chat_state
                    (chat_key, chat_name, last_peer_text, last_peer_fp,
                     last_peer_kind, last_reply, last_screen_sha256, last_sent_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    chat_name=excluded.chat_name,
                    last_peer_text=excluded.last_peer_text,
                    last_peer_fp=excluded.last_peer_fp,
                    last_peer_kind=excluded.last_peer_kind,
                    last_reply=excluded.last_reply,
                    last_screen_sha256=excluded.last_screen_sha256,
                    last_sent_at=excluded.last_sent_at,
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
                    float(merged.get("last_sent_at", 0) or 0),
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

    # ── P0-4：self_skip 持久化（让 runaway_guard 跨重启）────────
    def set_self_skip(
        self, norm_key: str, until_ts: float, reason: str = "",
    ) -> None:
        """写入/更新某 norm_key 的冷却到期时间（epoch）。

        until_ts 必须是 time.time() 域的绝对时间（不是 monotonic）。
        调用方负责转换：time.time() + (mono_until - time.monotonic())。
        """
        if not norm_key:
            return
        try:
            ts = float(until_ts or 0)
        except (TypeError, ValueError):
            return
        if ts <= time.time():
            # 已过期 — 直接删除而不是写入
            self.clear_self_skip(norm_key)
            return
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messenger_rpa_self_skip"
                "(norm_key, until_ts, reason, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(norm_key) DO UPDATE SET "
                "  until_ts=MAX(excluded.until_ts, until_ts),"  # 取更晚的
                "  reason=excluded.reason,"
                "  updated_at=excluded.updated_at",
                (norm_key, ts, reason or "", now),
            )
            c.commit()

    def clear_self_skip(self, norm_key: str) -> None:
        if not norm_key:
            return
        with self._lock, self._conn() as c:
            c.execute(
                "DELETE FROM messenger_rpa_self_skip WHERE norm_key=?",
                (norm_key,),
            )
            c.commit()

    def load_active_self_skips(self) -> Dict[str, Tuple[float, str]]:
        """启动时一次性回填。返回 {norm_key: (until_ts_epoch, reason)}。

        顺手清理已过期的行（GC）。
        """
        out: Dict[str, Tuple[float, str]] = {}
        now = time.time()
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "DELETE FROM messenger_rpa_self_skip WHERE until_ts < ?",
                    (now,),
                )
                c.commit()
                rows = c.execute(
                    "SELECT norm_key, until_ts, reason "
                    "FROM messenger_rpa_self_skip WHERE until_ts >= ?",
                    (now,),
                ).fetchall()
            for r in rows:
                out[str(r["norm_key"])] = (
                    float(r["until_ts"]), str(r["reason"] or ""),
                )
        except Exception:
            logger.debug("load_active_self_skips 失败", exc_info=True)
        return out

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

    # ── 人工客服交接状态 ───────────────────────────
    def _default_handoff(self, chat_key: str) -> Dict[str, Any]:
        return {
            "chat_key": str(chat_key or ""),
            "account_id": self._account_id,
            "owner": "",
            "status": "new",
            "line_status": "not_sent",
            "priority": "",
            "outcome": "",
            "notes": "",
            "next_followup_at": 0.0,
            "updated_by": "",
            "updated_at": 0.0,
            "created_at": 0.0,
        }

    def get_handoff(self, chat_key: str) -> Dict[str, Any]:
        """读取人工客服交接状态；没有记录时返回默认状态。"""
        key = str(chat_key or "")
        if not key:
            return self._default_handoff("")
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_handoffs WHERE chat_key=?",
                (key,),
            ).fetchone()
        if not row:
            return self._default_handoff(key)
        out = dict(row)
        out["next_followup_at"] = float(out.get("next_followup_at") or 0)
        out["updated_at"] = float(out.get("updated_at") or 0)
        out["created_at"] = float(out.get("created_at") or 0)
        return out

    def upsert_handoff(
        self,
        chat_key: str,
        *,
        account_id: Optional[str] = None,
        owner: Optional[str] = None,
        status: Optional[str] = None,
        line_status: Optional[str] = None,
        priority: Optional[str] = None,
        outcome: Optional[str] = None,
        notes: Optional[str] = None,
        next_followup_at: Optional[float] = None,
        updated_by: str = "web",
    ) -> Dict[str, Any]:
        """保存人工客服交接状态，未传字段保留原值。"""
        key = str(chat_key or "").strip()
        if not key:
            raise ValueError("chat_key is required")
        cur = self.get_handoff(key)

        def _text(value: Any, n: int) -> str:
            return str(value or "").strip()[:n]

        next_status = _text(status, 40) if status is not None else str(cur.get("status") or "new")
        if next_status not in HANDOFF_STATUSES:
            raise ValueError(f"invalid handoff status: {next_status}")
        next_line_status = (
            _text(line_status, 40)
            if line_status is not None
            else str(cur.get("line_status") or "not_sent")
        )
        if next_line_status not in LINE_HANDOFF_STATUSES:
            raise ValueError(f"invalid line status: {next_line_status}")
        next_priority = (
            _text(priority, 20)
            if priority is not None
            else str(cur.get("priority") or "")
        )
        if next_priority not in HANDOFF_PRIORITIES:
            raise ValueError(f"invalid priority: {next_priority}")
        next_follow = float(cur.get("next_followup_at") or 0)
        if next_followup_at is not None:
            next_follow = max(0.0, float(next_followup_at or 0))

        now = time.time()
        record = {
            "chat_key": key,
            "account_id": _text(account_id, 120) if account_id is not None else str(cur.get("account_id") or self._account_id),
            "owner": _text(owner, 120) if owner is not None else str(cur.get("owner") or ""),
            "status": next_status,
            "line_status": next_line_status,
            "priority": next_priority,
            "outcome": _text(outcome, 240) if outcome is not None else str(cur.get("outcome") or ""),
            "notes": _text(notes, 10000) if notes is not None else str(cur.get("notes") or ""),
            "next_followup_at": next_follow,
            "updated_by": _text(updated_by, 120),
            "updated_at": now,
            "created_at": float(cur.get("created_at") or 0) or now,
        }
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_handoffs
                    (chat_key, account_id, owner, status, line_status, priority,
                     outcome, notes, next_followup_at, updated_by, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    account_id=excluded.account_id,
                    owner=excluded.owner,
                    status=excluded.status,
                    line_status=excluded.line_status,
                    priority=excluded.priority,
                    outcome=excluded.outcome,
                    notes=excluded.notes,
                    next_followup_at=excluded.next_followup_at,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (
                    record["chat_key"],
                    record["account_id"],
                    record["owner"],
                    record["status"],
                    record["line_status"],
                    record["priority"],
                    record["outcome"],
                    record["notes"],
                    record["next_followup_at"],
                    record["updated_by"],
                    record["updated_at"],
                    record["created_at"],
                ),
            )
            c.commit()
        return record

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

    # ── 自动人设策略运行服务 ───────────────────────
    @staticmethod
    def _json_dumps(data: Any) -> str:
        try:
            return json.dumps(data if data is not None else {}, ensure_ascii=False)
        except Exception:
            return "{}"

    @staticmethod
    def _json_loads(raw: Any, default: Any) -> Any:
        if raw in (None, ""):
            return default
        try:
            return json.loads(str(raw))
        except Exception:
            return default

    @staticmethod
    def _make_id(prefix: str, *parts: Any) -> str:
        seed = "|".join(str(p) for p in parts) + f"|{time.time_ns()}"
        digest = hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest()[:20]
        return f"{prefix}_{digest}"

    def upsert_strategy_account(
        self,
        *,
        account_id: str,
        label: str = "",
        status: str = "active",
        supported_languages: Optional[List[str]] = None,
        supported_customer_types: Optional[List[str]] = None,
        persona_ids: Optional[List[str]] = None,
        health_score: float = 100.0,
        current_load: int = 0,
        daily_send_count: int = 0,
        max_daily_send: int = 200,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        aid = str(account_id or "").strip()
        if not aid:
            raise ValueError("account_id is required")
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_strategy_accounts
                    (account_id, label, status, supported_languages_json,
                     supported_customer_types_json, persona_ids_json,
                     health_score, current_load, daily_send_count,
                     max_daily_send, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    label=excluded.label,
                    status=excluded.status,
                    supported_languages_json=excluded.supported_languages_json,
                    supported_customer_types_json=excluded.supported_customer_types_json,
                    persona_ids_json=excluded.persona_ids_json,
                    health_score=excluded.health_score,
                    current_load=excluded.current_load,
                    daily_send_count=excluded.daily_send_count,
                    max_daily_send=excluded.max_daily_send,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    aid,
                    str(label or ""),
                    str(status or "active"),
                    self._json_dumps(supported_languages or []),
                    self._json_dumps(supported_customer_types or []),
                    self._json_dumps(persona_ids or []),
                    float(health_score),
                    int(current_load or 0),
                    int(daily_send_count or 0),
                    int(max_daily_send or 200),
                    self._json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            c.commit()

    def list_strategy_accounts(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_strategy_accounts "
                "ORDER BY updated_at DESC"
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["supported_languages"] = self._json_loads(
                d.pop("supported_languages_json", "[]"), []
            )
            d["supported_customer_types"] = self._json_loads(
                d.pop("supported_customer_types_json", "[]"), []
            )
            d["persona_ids"] = self._json_loads(d.pop("persona_ids_json", "[]"), [])
            d["metadata"] = self._json_loads(d.pop("metadata_json", "{}"), {})
            out.append(d)
        return out

    def upsert_persona(
        self,
        *,
        persona_id: str,
        name: str,
        language: str = "auto",
        customer_type: str = "",
        facts: Optional[List[str]] = None,
        persona: Optional[Dict[str, Any]] = None,
        status: str = "active",
    ) -> None:
        pid = str(persona_id or "").strip()
        if not pid:
            raise ValueError("persona_id is required")
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_personas
                    (persona_id, name, language, customer_type, facts_json,
                     persona_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id) DO UPDATE SET
                    name=excluded.name,
                    language=excluded.language,
                    customer_type=excluded.customer_type,
                    facts_json=excluded.facts_json,
                    persona_json=excluded.persona_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    pid,
                    str(name or pid),
                    str(language or "auto"),
                    str(customer_type or ""),
                    self._json_dumps(facts or []),
                    self._json_dumps(persona or {}),
                    str(status or "active"),
                    now,
                    now,
                ),
            )
            c.commit()

    def list_personas(self, *, status: str = "active") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM messenger_rpa_personas"
        params: Tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY updated_at DESC"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["facts"] = self._json_loads(d.pop("facts_json", "[]"), [])
            d["persona"] = self._json_loads(d.pop("persona_json", "{}"), {})
            out.append(d)
        return out

    def get_conversation_state(self, customer_id: str) -> Dict[str, Any]:
        cid = str(customer_id or "").strip()
        if not cid:
            return {}
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_conversation_states "
                "WHERE customer_id=?",
                (cid,),
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        d["recent_topics"] = self._json_loads(d.pop("recent_topics_json", "[]"), [])
        d["used_persona_facts"] = self._json_loads(
            d.pop("used_persona_facts_json", "[]"), []
        )
        d["metadata"] = self._json_loads(d.pop("metadata_json", "{}"), {})
        return d

    def list_conversation_states(self, limit: int = 100) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 100), 1000))
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_conversation_states "
                "ORDER BY updated_at DESC LIMIT ?",
                (lim,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["recent_topics"] = self._json_loads(d.pop("recent_topics_json", "[]"), [])
            d["used_persona_facts"] = self._json_loads(
                d.pop("used_persona_facts_json", "[]"), []
            )
            d["metadata"] = self._json_loads(d.pop("metadata_json", "{}"), {})
            out.append(d)
        return out

    def update_conversation_state(
        self,
        customer_id: str,
        *,
        chat_key: str = "",
        account_id: str = "",
        persona_id: str = "",
        customer_language: str = "",
        customer_type: str = "",
        stage: str = "new_lead",
        memory_summary: str = "",
        recent_topics: Optional[List[str]] = None,
        used_persona_facts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        last_message_at: Optional[float] = None,
    ) -> None:
        cid = str(customer_id or "").strip()
        if not cid:
            raise ValueError("customer_id is required")
        prev = self.get_conversation_state(cid)
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_conversation_states
                    (customer_id, chat_key, account_id, persona_id,
                     customer_language, customer_type, stage, memory_summary,
                     recent_topics_json, used_persona_facts_json, metadata_json,
                     last_message_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_id) DO UPDATE SET
                    chat_key=excluded.chat_key,
                    account_id=excluded.account_id,
                    persona_id=excluded.persona_id,
                    customer_language=excluded.customer_language,
                    customer_type=excluded.customer_type,
                    stage=excluded.stage,
                    memory_summary=excluded.memory_summary,
                    recent_topics_json=excluded.recent_topics_json,
                    used_persona_facts_json=excluded.used_persona_facts_json,
                    metadata_json=excluded.metadata_json,
                    last_message_at=excluded.last_message_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cid,
                    chat_key or prev.get("chat_key", ""),
                    account_id or prev.get("account_id", ""),
                    persona_id or prev.get("persona_id", ""),
                    customer_language or prev.get("customer_language", ""),
                    customer_type or prev.get("customer_type", ""),
                    stage or prev.get("stage", "new_lead"),
                    memory_summary if memory_summary != "" else prev.get("memory_summary", ""),
                    self._json_dumps(
                        recent_topics if recent_topics is not None
                        else prev.get("recent_topics", [])
                    ),
                    self._json_dumps(
                        used_persona_facts if used_persona_facts is not None
                        else prev.get("used_persona_facts", [])
                    ),
                    self._json_dumps(
                        metadata if metadata is not None else prev.get("metadata", {})
                    ),
                    float(last_message_at if last_message_at is not None else prev.get("last_message_at", 0) or now),
                    now,
                    now,
                ),
            )
            c.commit()

    def enqueue_auto_run_message(
        self,
        *,
        customer_id: str,
        text: str,
        chat_key: str = "",
        language: str = "",
        raw_payload: Optional[Dict[str, Any]] = None,
        account_id: str = "",
        persona_id: str = "",
        stage: str = "",
        strategy: Optional[Dict[str, Any]] = None,
        priority: int = 50,
        run_after: Optional[float] = None,
        message_id: str = "",
    ) -> str:
        cid = str(customer_id or "").strip()
        if not cid:
            raise ValueError("customer_id is required")
        msg_id = str(message_id or "").strip() or self._make_id("msg", cid, text[:80])
        job_id = self._make_id("job", cid, msg_id)
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO messenger_rpa_incoming_messages
                    (message_id, customer_id, chat_key, text, language,
                     raw_payload_json, received_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    cid,
                    str(chat_key or ""),
                    str(text or ""),
                    str(language or ""),
                    self._json_dumps(raw_payload or {}),
                    now,
                    now,
                ),
            )
            c.execute(
                """
                INSERT INTO messenger_rpa_auto_run_jobs
                    (job_id, customer_id, incoming_message_id, account_id,
                     persona_id, stage, strategy_json, priority, status,
                     run_after, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    job_id,
                    cid,
                    msg_id,
                    str(account_id or ""),
                    str(persona_id or ""),
                    str(stage or ""),
                    self._json_dumps(strategy or {}),
                    int(priority or 50),
                    float(run_after if run_after is not None else now),
                    now,
                    now,
                ),
            )
            c.commit()
        return job_id

    def lease_auto_run_jobs(
        self,
        *,
        worker_id: str,
        now_ts: Optional[float] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        now = float(now_ts or time.time())
        worker = str(worker_id or "worker")
        lim = max(1, min(int(limit or 10), 100))
        with self._lock, self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM messenger_rpa_auto_run_jobs
                WHERE status='pending' AND run_after<=?
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (now, lim),
            ).fetchall()
            job_ids = [str(r["job_id"]) for r in rows]
            if job_ids:
                q = ",".join("?" for _ in job_ids)
                c.execute(
                    f"UPDATE messenger_rpa_auto_run_jobs "
                    f"SET status='running', locked_by=?, locked_at=?, "
                    f"attempts=attempts+1, updated_at=? "
                    f"WHERE job_id IN ({q}) AND status='pending'",
                    (worker, now, now, *job_ids),
                )
            c.commit()
        return [self._hydrate_auto_run_job(dict(r)) for r in rows]

    def _hydrate_auto_run_job(self, d: Dict[str, Any]) -> Dict[str, Any]:
        d["strategy"] = self._json_loads(d.pop("strategy_json", "{}"), {})
        return d

    def list_auto_run_jobs(
        self, *, status: str = "all", limit: int = 100
    ) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 100), 1000))
        params: List[Any] = []
        sql = "SELECT * FROM messenger_rpa_auto_run_jobs"
        if status and status != "all":
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(lim)
        with self._lock, self._conn() as c:
            rows = c.execute(sql, tuple(params)).fetchall()
        return [self._hydrate_auto_run_job(dict(r)) for r in rows]

    def get_auto_run_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_auto_run_jobs WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
        return self._hydrate_auto_run_job(dict(row)) if row else {}

    def get_incoming_message(self, message_id: str) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_incoming_messages WHERE message_id=?",
                (str(message_id),),
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        d["raw_payload"] = self._json_loads(d.pop("raw_payload_json", "{}"), {})
        return d

    def list_strategy_chat_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 100), 1000))
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_chat_runs "
                "ORDER BY created_at DESC LIMIT ?",
                (lim,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["strategy"] = self._json_loads(d.pop("strategy_json", "{}"), {})
            out.append(d)
        return out

    def mark_auto_run_job_done(self, job_id: str) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE messenger_rpa_auto_run_jobs "
                "SET status='done', updated_at=? WHERE job_id=?",
                (now, str(job_id)),
            )
            c.commit()

    def mark_auto_run_job_failed(
        self, job_id: str, error: str, *, retry_after: Optional[float] = None
    ) -> None:
        now = time.time()
        if retry_after is not None:
            status = "pending"
            run_after = float(retry_after)
        else:
            status = "failed"
            run_after = now
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE messenger_rpa_auto_run_jobs "
                "SET status=?, run_after=?, last_error=?, updated_at=? "
                "WHERE job_id=?",
                (status, run_after, str(error or "")[:500], now, str(job_id)),
            )
            c.commit()

    def retry_auto_run_job(
        self, job_id: str, *, run_after: Optional[float] = None
    ) -> bool:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE messenger_rpa_auto_run_jobs "
                "SET status='pending', run_after=?, locked_by='', locked_at=0, "
                "last_error='', updated_at=? WHERE job_id=?",
                (float(run_after if run_after is not None else now), now, str(job_id)),
            )
            c.commit()
            return cur.rowcount > 0

    def cancel_auto_run_job(self, job_id: str, reason: str = "") -> bool:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE messenger_rpa_auto_run_jobs "
                "SET status='canceled', last_error=?, locked_by='', "
                "locked_at=0, updated_at=? WHERE job_id=?",
                (str(reason or "canceled_by_operator")[:500], now, str(job_id)),
            )
            c.commit()
            return cur.rowcount > 0

    def record_strategy_chat_run(
        self,
        *,
        customer_id: str,
        status: str,
        run_id: str = "",
        job_id: str = "",
        account_id: str = "",
        persona_id: str = "",
        previous_stage: str = "",
        next_stage: str = "",
        strategy: Optional[Dict[str, Any]] = None,
        reply_text: str = "",
        error: str = "",
    ) -> str:
        rid = str(run_id or "").strip() or self._make_id("run", customer_id, job_id)
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_chat_runs
                    (run_id, job_id, customer_id, account_id, persona_id,
                     previous_stage, next_stage, strategy_json, reply_text,
                     status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    str(job_id or ""),
                    str(customer_id or ""),
                    str(account_id or ""),
                    str(persona_id or ""),
                    str(previous_stage or ""),
                    str(next_stage or ""),
                    self._json_dumps(strategy or {}),
                    str(reply_text or ""),
                    str(status or "unknown"),
                    str(error or "")[:500],
                    now,
                ),
            )
            c.commit()
        return rid

    def append_strategy_audit(
        self,
        *,
        action: str,
        target_type: str = "",
        target_id: str = "",
        actor: str = "",
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        note: str = "",
    ) -> int:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO messenger_rpa_strategy_audit
                    (ts, actor, action, target_type, target_id,
                     before_json, after_json, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    str(actor or "web"),
                    str(action or ""),
                    str(target_type or ""),
                    str(target_id or ""),
                    self._json_dumps(before or {}),
                    self._json_dumps(after or {}),
                    str(note or "")[:500],
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)

    def list_strategy_audit(self, limit: int = 80) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 80), 500))
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messenger_rpa_strategy_audit "
                "ORDER BY ts DESC LIMIT ?",
                (lim,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["before"] = self._json_loads(d.pop("before_json", "{}"), {})
            d["after"] = self._json_loads(d.pop("after_json", "{}"), {})
            out.append(d)
        return out

    def get_strategy_audit(self, audit_id: int) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM messenger_rpa_strategy_audit WHERE id=?",
                (int(audit_id),),
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        d["before"] = self._json_loads(d.pop("before_json", "{}"), {})
        d["after"] = self._json_loads(d.pop("after_json", "{}"), {})
        return d

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
        allow_empty_reply: bool = False,
    ) -> int:
        """把待审批的回复写进 approvals 表；返回 row id。

        P6-3：``ai_tier`` 用于批量审批时按 tier 过滤（premium/normal/low）。
        旧调用者可忽略（默认空字符串）。

        ``allow_empty_reply=True``：允许 reply_text 空入队（"等人工 Suggest
        More 生成" 场景）；默认 False 保留防御性校验防意外空入队。
        """
        if not allow_empty_reply and not reply_text.strip():
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

    def enqueue_deferred(
        self,
        *,
        chat_key: str,
        chat_name: str,
        peer_text: str,
        peer_kind: str,
        reply_text: str,
        defer_until: float,
        defer_reason: str = "",
        reply_lang: str = "",
        run_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        staleness_sec: float = 0,
    ) -> int:
        """W2-D1+D2：把 safe_skip / pacing 的 reply 入延迟发送队列。

        - status='deferred' + deferred_until=ts；drain loop 到点取出真发
        - 同 chat 已有 deferred 行先标 expired（"最新承诺覆盖"）
        - ``staleness_sec``：row 级过期阈值。0 用 drain 默认。
          典型：pacing=60s，quiet_hours=21600s（6h），daily_cap=86400s
        """
        if not (reply_text or "").strip():
            raise ValueError("deferred reply_text 不能为空")
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
            # 同 chat 老的 deferred 先 expired，避免双发
            c.execute(
                """UPDATE messenger_rpa_approvals
                   SET status='expired', decided_at=?, decided_by='superseded',
                       decision_note='replaced_by_new_deferred'
                   WHERE chat_key=? AND status='deferred'""",
                (now, chat_key.strip()),
            )
            cur = c.execute(
                """INSERT INTO messenger_rpa_approvals
                     (created_at, chat_key, chat_name, peer_text, peer_kind,
                      reply_text, reply_lang, status, run_id, extra_json,
                      deferred_until, defer_reason, defer_staleness_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'deferred', ?, ?, ?, ?, ?)""",
                (
                    now, chat_key.strip(), chat_name or "",
                    peer_text or "", peer_kind or "",
                    reply_text.strip(), reply_lang or "",
                    run_id or "", extra_json,
                    float(defer_until), (defer_reason or "")[:120],
                    float(max(0.0, staleness_sec)),
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)

    def update_deferred_until(
        self, approval_id: int, new_until: float, note: str = "",
    ) -> bool:
        """W2-D2.2：drain 前 gate 未通过 → 把 deferred_until 推后。

        Returns: True if updated (row exists & status=deferred)，False otherwise.
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE messenger_rpa_approvals
                   SET deferred_until=?,
                       decision_note=COALESCE(NULLIF(?, ''), decision_note)
                   WHERE id=? AND status='deferred'""",
                (float(new_until), (note or "")[:120], int(approval_id)),
            )
            c.commit()
            return bool(cur.rowcount)

    def drain_due_deferred(self, now_ts: Optional[float] = None,
                           limit: int = 20,
                           staleness_sec: float = 6 * 3600,
                           ) -> List[Dict[str, Any]]:
        """取出已到期的 deferred 行，按 deferred_until 升序。

        ★ W2-D2 v6：row 级 staleness — 每行用自己的 defer_staleness_sec
        （由 enqueue_deferred 写入）；为 0 时回落到 ``staleness_sec`` 参数。
        典型：pacing 类 60s，quiet_hours 类 21600s（6h），daily_cap 类 86400s。

        注意：调用方负责发送；发送结果由 mark_deferred_sent 或
        mark_deferred_failed 写回。这里只读不改 status（除 stale expire），
        避免读后崩溃丢消息。
        """
        ts = float(now_ts if now_ts is not None else time.time())
        default_stale = max(0.0, float(staleness_sec))
        with self._lock, self._conn() as c:
            # 先把过时的就地 expired（row 级阈值优先；为 0 用默认）
            # SQL：阈值 = COALESCE(defer_staleness_sec>0 ? defer_staleness_sec : default, default)
            # 简化：用 CASE 表达
            c.execute(
                """UPDATE messenger_rpa_approvals
                   SET status='expired', decided_at=?, decided_by='auto_expire',
                       decision_note='stale_after_drain_window'
                   WHERE status='deferred' AND deferred_until > 0
                     AND deferred_until <= ?
                     AND (
                       (defer_staleness_sec > 0 AND
                        created_at < ? - defer_staleness_sec)
                       OR
                       (defer_staleness_sec <= 0 AND ? > 0 AND
                        created_at < ? - ?)
                     )""",
                (ts, ts, ts, default_stale, ts, default_stale),
            )
            c.commit()
            rows = c.execute(
                """SELECT * FROM messenger_rpa_approvals
                   WHERE status='deferred' AND deferred_until > 0
                     AND deferred_until <= ?
                   ORDER BY deferred_until ASC
                   LIMIT ?""",
                (ts, int(max(1, limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_deferred_sent(self, approval_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """UPDATE messenger_rpa_approvals
                   SET status='sent', sent_at=?, decided_at=?,
                       decided_by='deferred_drain'
                   WHERE id=? AND status='deferred'""",
                (time.time(), time.time(), int(approval_id)),
            )
            c.commit()

    def mark_deferred_failed(self, approval_id: int, err: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """UPDATE messenger_rpa_approvals
                   SET status='failed', send_error=?, decided_at=?,
                       decided_by='deferred_drain'
                   WHERE id=? AND status='deferred'""",
                ((err or "")[:500], time.time(), int(approval_id)),
            )
            c.commit()

    def expire_deferred_for_chat(self, chat_key: str, reason: str = "new_inbound") -> int:
        """W2-D1.4：用户后续消息进来时，把同 chat 的 deferred 全部 expire。

        理由：deferred 的 reply_text 是基于旧 peer_msg 生成的，对方又说话了
        语境已变，这条 reply 不再准确。返回 expired 数量。
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                """UPDATE messenger_rpa_approvals
                   SET status='expired', decided_at=?, decided_by='auto_expire',
                       decision_note=?
                   WHERE chat_key=? AND status='deferred'""",
                (time.time(), (reason or "")[:120], chat_key.strip()),
            )
            c.commit()
            return int(cur.rowcount or 0)

    def list_approvals(
        self,
        *,
        status: Optional[str] = None,
        chat_key: Optional[str] = None,
        reply_text_empty: Optional[bool] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列审批；status=None 时返回所有。

        ``reply_text_empty``：None 不过滤；True 仅空 reply_text（escalation
        占位行）；False 仅非空（正常 auto-reply 待审）。入队时 reply_text
        已 strip，因此空判等于 ``reply_text = ''``。
        """
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if chat_key:
            clauses.append("chat_key=?")
            params.append(chat_key)
        if reply_text_empty is True:
            clauses.append("reply_text = ''")
        elif reply_text_empty is False:
            clauses.append("reply_text <> ''")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(int(limit or 50), 1))
        sql = (
            f"SELECT * FROM messenger_rpa_approvals {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        with self._lock, self._conn() as c:
            rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def count_approvals(
        self,
        *,
        status: Optional[str] = None,
        chat_key: Optional[str] = None,
        reply_text_empty: Optional[bool] = None,
    ) -> int:
        """统计审批行数；过滤语义与 ``list_approvals`` 一致。

        为 ``/status`` 观测字段 ``pending_empty_count``（escalation 占位行堆积）
        提供 O(1) 走索引的计数路径，避免 ``len(list_approvals(...))`` 全行
        物化的 O(n) 开销。
        """
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if chat_key:
            clauses.append("chat_key=?")
            params.append(chat_key)
        if reply_text_empty is True:
            clauses.append("reply_text = ''")
        elif reply_text_empty is False:
            clauses.append("reply_text <> ''")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT COUNT(*) AS n FROM messenger_rpa_approvals {where}"
        with self._lock, self._conn() as c:
            row = c.execute(sql, tuple(params)).fetchone()
        return int(row["n"]) if row else 0

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

    # ── B2: per-chat persona overrides（运营手动指定人设）──
    def upsert_chat_persona_override(
        self,
        *,
        chat_name: str,
        reply_profile_id: str,
        account_id: str = "",
        bound_by: str = "web_admin",
        notes: str = "",
    ) -> bool:
        """运营手动绑定 chat → reply_profile_id。覆盖原 match_names / default。"""
        chat_name = (chat_name or "").strip()
        reply_profile_id = (reply_profile_id or "").strip()
        if not chat_name or not reply_profile_id:
            return False
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO messenger_rpa_chat_persona_overrides
                    (chat_name, account_id, reply_profile_id, bound_by, bound_at, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_name, account_id) DO UPDATE SET
                    reply_profile_id = excluded.reply_profile_id,
                    bound_by = excluded.bound_by,
                    bound_at = excluded.bound_at,
                    notes = excluded.notes
                """,
                (chat_name, account_id or "", reply_profile_id,
                 bound_by, time.time(), notes),
            )
            c.commit()
            return True

    def get_chat_persona_override(
        self, chat_name: str, account_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """读 chat 的运营指定 persona。优先匹配同 account_id，没有则匹配空 account_id。
        返回 dict {chat_name, account_id, reply_profile_id, bound_by, bound_at, notes}。
        """
        chat_name = (chat_name or "").strip()
        if not chat_name:
            return None
        with self._lock, self._conn() as c:
            # 1. 优先精确 account_id 匹配
            row = c.execute(
                """SELECT chat_name, account_id, reply_profile_id, bound_by, bound_at, notes
                   FROM messenger_rpa_chat_persona_overrides
                   WHERE chat_name=? AND account_id=?""",
                (chat_name, account_id or ""),
            ).fetchone()
            if not row and account_id:
                # 2. fallback：account_id='' 的全局绑定
                row = c.execute(
                    """SELECT chat_name, account_id, reply_profile_id, bound_by, bound_at, notes
                       FROM messenger_rpa_chat_persona_overrides
                       WHERE chat_name=? AND account_id=''""",
                    (chat_name,),
                ).fetchone()
            if not row:
                return None
            return {
                "chat_name": row[0],
                "account_id": row[1],
                "reply_profile_id": row[2],
                "bound_by": row[3],
                "bound_at": row[4],
                "notes": row[5] or "",
            }

    def remove_chat_persona_override(
        self, chat_name: str, account_id: str = "",
    ) -> bool:
        chat_name = (chat_name or "").strip()
        if not chat_name:
            return False
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM messenger_rpa_chat_persona_overrides "
                "WHERE chat_name=? AND account_id=?",
                (chat_name, account_id or ""),
            )
            c.commit()
            return cur.rowcount > 0

    def list_chat_persona_overrides(
        self, account_id: str = "",
    ) -> List[Dict[str, Any]]:
        """列出当前所有绑定。account_id='' 时列所有；否则只列该账号。"""
        with self._lock, self._conn() as c:
            if account_id:
                rows = c.execute(
                    """SELECT chat_name, account_id, reply_profile_id, bound_by, bound_at, notes
                       FROM messenger_rpa_chat_persona_overrides
                       WHERE account_id=? OR account_id=''
                       ORDER BY bound_at DESC""",
                    (account_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT chat_name, account_id, reply_profile_id, bound_by, bound_at, notes
                       FROM messenger_rpa_chat_persona_overrides
                       ORDER BY bound_at DESC""",
                ).fetchall()
            return [
                {
                    "chat_name": r[0],
                    "account_id": r[1],
                    "reply_profile_id": r[2],
                    "bound_by": r[3],
                    "bound_at": r[4],
                    "notes": r[5] or "",
                }
                for r in rows
            ]

    def batch_upsert_chat_persona_overrides(
        self,
        bindings: List[Dict[str, Any]],
        *,
        bound_by: str = "web_admin",
    ) -> int:
        """批量绑定。bindings = [{chat_name, reply_profile_id, account_id?, notes?}, ...]
        返回成功 upsert 的条数。"""
        n = 0
        for b in bindings or []:
            try:
                if self.upsert_chat_persona_override(
                    chat_name=str(b.get("chat_name") or ""),
                    reply_profile_id=str(b.get("reply_profile_id") or ""),
                    account_id=str(b.get("account_id") or ""),
                    bound_by=bound_by,
                    notes=str(b.get("notes") or ""),
                ):
                    n += 1
            except Exception:
                continue
        return n

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
