"""InboxStore — 统一收件箱 SQLite 持久层。

设计参考 src/contacts/store.py：
- 单进程单 connection + threading.Lock
- WAL + busy_timeout + row_factory=Row
- 多表 DDL 一次 executescript
- 幂等 migration（PRAGMA table_info + ALTER TABLE ADD COLUMN）

四张表：
- conversations        跨平台会话事实源（ingest 写）
- messages             统一消息（去重靠确定性 message_id 主键）
- message_analysis     意图/情绪/风险（Phase C 写）
- conversation_settings 运营态配置（automation_mode）——与 ingest 解耦，
                        ingest 永不触碰，修掉「automation_mode 进程内 dict 重启即丢」

关键不变量：
1. ingest 只写 conversations 的事实列，绝不动 conversation_settings。
2. messages 主键确定性生成（有 platform_msg_id 用之，否则 hash(text|ts)），
   INSERT OR IGNORE 天然幂等，重复轮询不重复入库。
3. conversations.last_ts 单调不回退：旧的 fetch 不覆盖更新的 last_text/last_ts。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import InboxConversation, InboxMessage, MessageAnalysis

logger = logging.getLogger(__name__)

AUTOMATION_MODES = {"manual", "review", "multi_choice", "auto_ai"}
_DEFAULT_AUTOMATION_MODE = "review"


_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id   TEXT PRIMARY KEY,
    platform          TEXT NOT NULL,
    account_id        TEXT NOT NULL DEFAULT 'default',
    chat_key          TEXT NOT NULL DEFAULT '',
    contact_id        TEXT NOT NULL DEFAULT '',
    display_name      TEXT NOT NULL DEFAULT '',
    language          TEXT NOT NULL DEFAULT 'unknown',
    last_text         TEXT NOT NULL DEFAULT '',
    last_ts           REAL NOT NULL DEFAULT 0,
    unread            INTEGER NOT NULL DEFAULT 0,
    risk_level        TEXT NOT NULL DEFAULT 'unknown',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_updated  ON conversations(last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_conv_platform ON conversations(platform, account_id);
CREATE INDEX IF NOT EXISTS idx_conv_contact  ON conversations(contact_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id        TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL,
    platform_msg_id   TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL DEFAULT 'in',
    text              TEXT NOT NULL DEFAULT '',
    original_text     TEXT NOT NULL DEFAULT '',
    translated_text   TEXT NOT NULL DEFAULT '',
    source_lang       TEXT NOT NULL DEFAULT 'unknown',
    target_lang       TEXT NOT NULL DEFAULT '',
    media_type        TEXT NOT NULL DEFAULT '',
    media_ref         TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL DEFAULT 0,
    ingested_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON messages(conversation_id, ts DESC);

CREATE TABLE IF NOT EXISTS message_analysis (
    analysis_id        TEXT PRIMARY KEY,
    message_id         TEXT NOT NULL,
    conversation_id    TEXT NOT NULL,
    intent             TEXT NOT NULL DEFAULT '',
    emotion            TEXT NOT NULL DEFAULT '',
    risk_level         TEXT NOT NULL DEFAULT 'low',
    risk_reasons_json  TEXT NOT NULL DEFAULT '[]',
    relationship_stage TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    order_no           TEXT NOT NULL DEFAULT '',
    confidence         REAL NOT NULL DEFAULT 0,
    analyzer           TEXT NOT NULL DEFAULT 'rule',
    ts                 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ana_msg  ON message_analysis(message_id);
CREATE INDEX IF NOT EXISTS idx_ana_conv ON message_analysis(conversation_id, ts DESC);

CREATE TABLE IF NOT EXISTS conversation_settings (
    conversation_id   TEXT PRIMARY KEY,
    automation_mode   TEXT NOT NULL DEFAULT 'review',
    updated_at        REAL NOT NULL
);

-- Phase B：统一草稿层。
-- 注意：平台来源的草稿事实源仍在各 RPA 表（line_rpa_pending / wa_rpa_pending /
-- messenger_rpa_approvals），读路径走 read-through 直读聚合，不在此镜像。
-- 本表只存：(a) inbox 自发草稿（source_kind='inbox'，无平台表）；
--           (b) 风险/autopilot 元数据 overlay（按 source_kind+source_id 键，Phase C 写）。
CREATE TABLE IF NOT EXISTS reply_drafts (
    draft_id           TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL DEFAULT '',
    platform           TEXT NOT NULL DEFAULT '',
    account_id         TEXT NOT NULL DEFAULT 'default',
    chat_key           TEXT NOT NULL DEFAULT '',
    source_kind        TEXT NOT NULL,              -- inbox | line_pending | wa_pending | messenger_approval | reunion
    source_id          TEXT NOT NULL DEFAULT '',
    peer_text          TEXT NOT NULL DEFAULT '',
    draft_text         TEXT NOT NULL DEFAULT '',
    final_text         TEXT NOT NULL DEFAULT '',
    draft_lang         TEXT NOT NULL DEFAULT '',
    translated_preview TEXT NOT NULL DEFAULT '',
    risk_level         TEXT NOT NULL DEFAULT 'low',
    risk_reasons_json  TEXT NOT NULL DEFAULT '[]',
    autopilot_level    TEXT NOT NULL DEFAULT 'L1',
    status             TEXT NOT NULL DEFAULT 'pending',
    decided_by         TEXT NOT NULL DEFAULT '',
    decided_at         REAL NOT NULL DEFAULT 0,
    sent_at            REAL NOT NULL DEFAULT 0,
    error              TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON reply_drafts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_conv   ON reply_drafts(conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_drafts_source ON reply_drafts(source_kind, source_id);
"""


def _message_pk(conversation_id: str, platform_msg_id: str, text: str, ts: Any) -> str:
    """确定性消息主键：有平台 id 用平台 id，否则用 hash(text|ts) 兜底。

    这样无 platform_msg_id 的 RPA 消息也能稳定去重（避免 (conv, '') 唯一约束
    把同会话所有无 id 消息折叠成一条）。
    """
    pid = str(platform_msg_id or "").strip()
    if pid:
        return f"{conversation_id}:{pid}"
    digest = hashlib.sha256(f"{text}|{ts}".encode("utf-8")).hexdigest()[:16]
    return f"{conversation_id}:h:{digest}"


class InboxStore:
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

    # ── 写入（ingest 调用，幂等）──────────────────────────────

    def upsert_conversation(self, conv: InboxConversation) -> None:
        if not conv.conversation_id or not conv.platform:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations
                    (conversation_id, platform, account_id, chat_key, contact_id,
                     display_name, language, last_text, last_ts, unread,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    language = CASE WHEN excluded.language != 'unknown'
                                    THEN excluded.language ELSE conversations.language END,
                    last_text = CASE WHEN excluded.last_ts >= conversations.last_ts
                                     THEN excluded.last_text ELSE conversations.last_text END,
                    last_ts = MAX(excluded.last_ts, conversations.last_ts),
                    unread = excluded.unread,
                    contact_id = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversations.contact_id END,
                    updated_at = excluded.updated_at
                """,
                (
                    conv.conversation_id, conv.platform, conv.account_id, conv.chat_key,
                    conv.contact_id, conv.display_name, conv.language, conv.last_text,
                    float(conv.last_ts or 0), int(conv.unread or 0), now, now,
                ),
            )
            self._conn.commit()

    def ingest_message(self, msg: InboxMessage) -> bool:
        """INSERT OR IGNORE，返回是否新插入。"""
        if not msg.conversation_id:
            return False
        mid = _message_pk(msg.conversation_id, msg.platform_msg_id, msg.text, msg.ts)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, conversation_id, platform_msg_id, direction, text,
                     original_text, translated_text, source_lang, target_lang,
                     media_type, media_ref, ts, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid, msg.conversation_id, str(msg.platform_msg_id or ""), msg.direction,
                    msg.text, msg.original_text or msg.text, msg.translated_text,
                    msg.source_lang, msg.target_lang, msg.media_type, msg.media_ref,
                    float(msg.ts or 0), self._now(),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def ingest_batch(self, conv: InboxConversation, msgs: List[InboxMessage]) -> int:
        """一个事务内 upsert 会话 + 批量 ingest 消息；返回新插入消息条数。"""
        if not conv.conversation_id or not conv.platform:
            return 0
        now = self._now()
        inserted = 0
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations
                    (conversation_id, platform, account_id, chat_key, contact_id,
                     display_name, language, last_text, last_ts, unread,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    language = CASE WHEN excluded.language != 'unknown'
                                    THEN excluded.language ELSE conversations.language END,
                    last_text = CASE WHEN excluded.last_ts >= conversations.last_ts
                                     THEN excluded.last_text ELSE conversations.last_text END,
                    last_ts = MAX(excluded.last_ts, conversations.last_ts),
                    unread = excluded.unread,
                    contact_id = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversations.contact_id END,
                    updated_at = excluded.updated_at
                """,
                (
                    conv.conversation_id, conv.platform, conv.account_id, conv.chat_key,
                    conv.contact_id, conv.display_name, conv.language, conv.last_text,
                    float(conv.last_ts or 0), int(conv.unread or 0), now, now,
                ),
            )
            for msg in msgs or []:
                if not msg.conversation_id:
                    continue
                mid = _message_pk(msg.conversation_id, msg.platform_msg_id, msg.text, msg.ts)
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (message_id, conversation_id, platform_msg_id, direction, text,
                         original_text, translated_text, source_lang, target_lang,
                         media_type, media_ref, ts, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid, msg.conversation_id, str(msg.platform_msg_id or ""), msg.direction,
                        msg.text, msg.original_text or msg.text, msg.translated_text,
                        msg.source_lang, msg.target_lang, msg.media_type, msg.media_ref,
                        float(msg.ts or 0), now,
                    ),
                )
                inserted += 1 if cur.rowcount > 0 else 0
            self._conn.commit()
        return inserted

    # ── 读取（unified_inbox 路由调用）──────────────────────────

    def list_conversations(
        self, *, limit: int = 50, platform: str = ""
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        sql = "SELECT * FROM conversations"
        params: List[Any] = []
        if platform:
            sql += " WHERE platform = ?"
            params.append(platform)
        sql += " ORDER BY last_ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_messages(self, conversation_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ts ASC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_messages(self, conversation_id: str = "") -> int:
        with self._lock:
            if conversation_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0]) if row else 0

    # ── automation_mode 持久化（替换进程内 dict）────────────────

    def get_automation_mode(self, conversation_id: str) -> str:
        if not conversation_id:
            return _DEFAULT_AUTOMATION_MODE
        with self._lock:
            row = self._conn.execute(
                "SELECT automation_mode FROM conversation_settings WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return _DEFAULT_AUTOMATION_MODE
        mode = str(row["automation_mode"] or _DEFAULT_AUTOMATION_MODE)
        return mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE

    def set_automation_mode(self, conversation_id: str, mode: str) -> None:
        if not conversation_id:
            return
        mode = mode if mode in AUTOMATION_MODES else _DEFAULT_AUTOMATION_MODE
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_settings (conversation_id, automation_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    automation_mode = excluded.automation_mode,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, mode, self._now()),
            )
            self._conn.commit()

    def all_automation_modes(self) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, automation_mode FROM conversation_settings"
            ).fetchall()
        return {str(r["conversation_id"]): str(r["automation_mode"]) for r in rows}

    # ── 分析落库（Phase C 用，A 先建口）────────────────────────

    def save_analysis(self, analysis: MessageAnalysis) -> str:
        analysis_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO message_analysis
                    (analysis_id, message_id, conversation_id, intent, emotion, risk_level,
                     risk_reasons_json, relationship_stage, summary, order_no, confidence,
                     analyzer, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id, analysis.message_id, analysis.conversation_id,
                    analysis.intent, analysis.emotion, analysis.risk_level,
                    json.dumps(list(analysis.risk_reasons), ensure_ascii=False),
                    analysis.relationship_stage, analysis.summary, analysis.order_no,
                    float(analysis.confidence or 0), analysis.analyzer, self._now(),
                ),
            )
            self._conn.commit()
        return analysis_id

    def latest_analysis(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM message_analysis WHERE conversation_id = ? ORDER BY ts DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["risk_reasons"] = json.loads(out.pop("risk_reasons_json", "[]") or "[]")
        except Exception:
            out["risk_reasons"] = []
        return out

    # ── reply_drafts（Phase B：inbox 自发草稿 + 风险 overlay）─────

    def upsert_draft(self, draft: Dict[str, Any]) -> str:
        """写入/更新一条草稿。

        - inbox 自发：传 source_kind='inbox' + 自带 draft_id（或自动生成）。
        - overlay：传 source_kind+source_id（平台来源），靠 uq_drafts_source 幂等，
          用于给平台草稿挂风险/autopilot 元数据。
        """
        source_kind = str(draft.get("source_kind") or "inbox")
        source_id = str(draft.get("source_id") or "")
        now = self._now()
        draft_id = str(draft.get("draft_id") or "")
        if not draft_id:
            draft_id = (
                f"{source_kind}:{source_id}" if source_id else f"inbox:{uuid.uuid4().hex}"
            )
        risk_reasons = draft.get("risk_reasons") or []
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reply_drafts
                    (draft_id, conversation_id, platform, account_id, chat_key,
                     source_kind, source_id, peer_text, draft_text, final_text,
                     draft_lang, translated_preview, risk_level, risk_reasons_json,
                     autopilot_level, status, decided_by, decided_at, sent_at, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_kind, source_id) DO UPDATE SET
                    risk_level = excluded.risk_level,
                    risk_reasons_json = excluded.risk_reasons_json,
                    autopilot_level = excluded.autopilot_level,
                    translated_preview = CASE WHEN excluded.translated_preview != ''
                        THEN excluded.translated_preview ELSE reply_drafts.translated_preview END,
                    status = excluded.status,
                    final_text = CASE WHEN excluded.final_text != ''
                        THEN excluded.final_text ELSE reply_drafts.final_text END,
                    decided_by = excluded.decided_by,
                    decided_at = excluded.decided_at,
                    sent_at = excluded.sent_at,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    draft_id, str(draft.get("conversation_id") or ""),
                    str(draft.get("platform") or ""), str(draft.get("account_id") or "default"),
                    str(draft.get("chat_key") or ""), source_kind, source_id,
                    str(draft.get("peer_text") or ""), str(draft.get("draft_text") or ""),
                    str(draft.get("final_text") or ""), str(draft.get("draft_lang") or ""),
                    str(draft.get("translated_preview") or ""),
                    str(draft.get("risk_level") or "low"),
                    json.dumps(list(risk_reasons), ensure_ascii=False),
                    str(draft.get("autopilot_level") or "L1"),
                    str(draft.get("status") or "pending"),
                    str(draft.get("decided_by") or ""), float(draft.get("decided_at") or 0),
                    float(draft.get("sent_at") or 0), str(draft.get("error") or ""),
                    now, now,
                ),
            )
            self._conn.commit()
        return draft_id

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reply_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def get_overlay(self, source_kind: str, source_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reply_drafts WHERE source_kind = ? AND source_id = ?",
                (source_kind, str(source_id)),
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def list_drafts(
        self, *, source_kind: str = "", status: str = "", limit: int = 50
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        sql = "SELECT * FROM reply_drafts"
        clauses: List[str] = []
        params: List[Any] = []
        if source_kind:
            clauses.append("source_kind = ?")
            params.append(source_kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_draft(r) for r in rows]

    @staticmethod
    def _row_to_draft(row) -> Dict[str, Any]:
        out = dict(row)
        try:
            out["risk_reasons"] = json.loads(out.pop("risk_reasons_json", "[]") or "[]")
        except Exception:
            out["risk_reasons"] = []
        return out
