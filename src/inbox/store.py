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

-- Phase 5：坐席在线状态 + 会话租约锁（多坐席防重复回复）
CREATE TABLE IF NOT EXISTS agent_presence (
    agent_id          TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'offline',
    last_seen_at      REAL NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_claims (
    conversation_id   TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,
    agent_name        TEXT NOT NULL DEFAULT '',
    claimed_at        REAL NOT NULL DEFAULT 0,
    expires_at        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_agent   ON conversation_claims(agent_id);
CREATE INDEX IF NOT EXISTS idx_claims_expires ON conversation_claims(expires_at);

CREATE TABLE IF NOT EXISTS agent_sends (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL,
    agent_id          TEXT NOT NULL DEFAULT '',
    agent_name        TEXT NOT NULL DEFAULT '',
    ts                REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_sends_conv ON agent_sends(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_agent_sends_ts   ON agent_sends(ts);

CREATE TABLE IF NOT EXISTS agent_prefs (
    agent_id          TEXT PRIMARY KEY,
    warn_sec          INTEGER NOT NULL DEFAULT 0,   -- 0=沿用全局
    crit_sec          INTEGER NOT NULL DEFAULT 0,   -- 0=沿用全局
    muted             INTEGER NOT NULL DEFAULT 0,   -- 1=完全静音告警
    dnd_start         INTEGER NOT NULL DEFAULT -1,  -- 免打扰起(本地分钟 0-1439)，-1=关
    dnd_end           INTEGER NOT NULL DEFAULT -1,  -- 免打扰止(本地分钟 0-1439)，-1=关
    updated_at        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS escalations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    agent_id          TEXT NOT NULL DEFAULT '',   -- 升级时的认领人(问责)
    agent_name        TEXT NOT NULL DEFAULT '',
    wait_sec          INTEGER NOT NULL DEFAULT 0,
    ts                REAL NOT NULL DEFAULT 0,
    assigned_to       TEXT NOT NULL DEFAULT ''    -- 负责处理此次升级的主管 agent_id
);
CREATE INDEX IF NOT EXISTS idx_escalations_conv     ON escalations(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_escalations_ts       ON escalations(ts);
CREATE INDEX IF NOT EXISTS idx_escalations_assigned ON escalations(assigned_to, ts);
"""

# 对存量 escalations 表补列（新安装已由 DDL 建好，旧库通过 migration 追加）
_MIGRATIONS = [
    "ALTER TABLE escalations ADD COLUMN assigned_to TEXT NOT NULL DEFAULT ''",
]


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
            for _sql in _MIGRATIONS:
                try:
                    self._conn.execute(_sql)
                except Exception:
                    pass  # 列已存在则忽略
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

    def list_recent_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """取会话**最近** limit 条（可用 before_ts 游标向更早翻页），返回 ts 升序。

        与 list_messages（取最旧 limit 条）相反，用于时间线展示与分页加载。
        """
        limit = max(1, min(500, int(limit or 50)))
        with self._lock:
            if before_ts is not None:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND ts < ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (conversation_id, float(before_ts), limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY ts DESC LIMIT ?",
                    (conversation_id, limit),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_message_dirs(
        self, conversation_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """每个会话最后一条消息的方向与时间（SLA：当前未回复时长用）。

        conversation_ids=None → 全部会话；否则仅限给定集合（会话列表批量）。
        返回 {conversation_id: {"direction": "in"/"out", "ts": float}}。
        """
        where = ""
        params: List[Any] = []
        if conversation_ids is not None:
            ids = list({c for c in conversation_ids if c})
            if not ids:
                return {}
            ph = ",".join("?" * len(ids))
            where = f"WHERE conversation_id IN ({ph})"
            params = ids
        sql = (
            "SELECT m.conversation_id AS cid, m.direction AS direction, m.ts AS ts "
            "FROM messages m JOIN (SELECT conversation_id, MAX(ts) AS mts FROM messages "
            f"{where} GROUP BY conversation_id) x "
            "ON m.conversation_id=x.conversation_id AND m.ts=x.mts"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return {str(r["cid"]): {"direction": str(r["direction"] or "in"),
                                "ts": float(r["ts"] or 0)} for r in rows}

    def first_response_rows(
        self, since_ts: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """每会话首响原始数据（首条入站 ts → 首条其后出站 ts）。

        仅返回 t_in >= since_ts 的会话（窗口内首次进线）。t_out 为 None ⇒ 尚未回复。
        首响时长/达标率/趋势的聚合交由调用方（路由）在内存完成，保持本方法纯查询。
        """
        sql = (
            "WITH firstin AS ("
            "  SELECT conversation_id, MIN(ts) AS t_in FROM messages "
            "  WHERE direction='in' GROUP BY conversation_id"
            ") "
            "SELECT f.conversation_id AS cid, f.t_in AS t_in, "
            "  (SELECT MIN(m.ts) FROM messages m "
            "   WHERE m.conversation_id=f.conversation_id AND m.direction='out' "
            "   AND m.ts>=f.t_in) AS t_out "
            "FROM firstin f WHERE f.t_in >= ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (float(since_ts),)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            t_out = r["t_out"]
            out.append({
                "cid": str(r["cid"]),
                "t_in": float(r["t_in"] or 0),
                "t_out": float(t_out) if t_out is not None else None,
            })
        return out

    def record_agent_send(
        self, conversation_id: str, agent_id: str, *,
        agent_name: str = "", ts: Optional[float] = None,
    ) -> None:
        """记录一次坐席人工发送（用于历史首响坐席归属）。

        与消息 ingest 解耦：发送瞬间打点，不依赖 RPA 出站消息何时被旁路 ingest。
        """
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid:
            return
        t = float(ts) if ts is not None else self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_sends (conversation_id, agent_id, agent_name, ts) "
                "VALUES (?,?,?,?)",
                (cid, aid, str(agent_name or ""), t),
            )
            self._conn.commit()

    def agent_first_responses(
        self, since_ts: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """每会话首响坐席归属：首条入站 → 其后**首次坐席发送**（agent_sends）。

        仅统计 t_in>=since_ts 的会话。resp_ts/agent_id 为 None ⇒ 该会话尚无坐席首响
        （可能 AI 自动回复或未回复）。聚合（按坐席的均值/达标率）交由调用方完成。
        """
        sql = (
            "WITH firstin AS ("
            "  SELECT conversation_id, MIN(ts) AS t_in FROM messages "
            "  WHERE direction='in' GROUP BY conversation_id"
            ") "
            "SELECT f.conversation_id AS cid, f.t_in AS t_in, "
            "  (SELECT s.ts FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS resp_ts, "
            "  (SELECT s.agent_id FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS agent_id, "
            "  (SELECT s.agent_name FROM agent_sends s WHERE s.conversation_id=f.conversation_id "
            "   AND s.ts>=f.t_in ORDER BY s.ts ASC LIMIT 1) AS agent_name "
            "FROM firstin f WHERE f.t_in >= ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (float(since_ts),)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            resp = r["resp_ts"]
            out.append({
                "cid": str(r["cid"]),
                "t_in": float(r["t_in"] or 0),
                "resp_ts": float(resp) if resp is not None else None,
                "agent_id": str(r["agent_id"]) if r["agent_id"] is not None else None,
                "agent_name": str(r["agent_name"] or "") if r["agent_name"] is not None else "",
            })
        return out

    def count_agent_sends_by_day(
        self, agent_id: str, since_ts: float = 0.0,
    ) -> Dict[str, int]:
        """某坐席按本地日期的人工发送条数（个人日报：发送量）。"""
        aid = str(agent_id or "").strip()
        if not aid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d, "
                "COUNT(*) AS n FROM agent_sends WHERE agent_id=? AND ts>=? "
                "GROUP BY d", (aid, float(since_ts)),
            ).fetchall()
        return {str(r["d"]): int(r["n"]) for r in rows if r["d"]}

    def update_message_translation(
        self,
        message_id: str,
        *,
        translated_text: str,
        target_lang: str = "zh",
        source_lang: str = "",
    ) -> bool:
        """回写入站消息译文（Phase 5-3 自动翻译缓存）。"""
        mid = str(message_id or "").strip()
        if not mid or not str(translated_text or "").strip():
            return False
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE messages SET
                    translated_text = ?,
                    target_lang = ?,
                    source_lang = CASE WHEN ? != '' THEN ? ELSE source_lang END
                WHERE message_id = ?
                """,
                (
                    str(translated_text),
                    str(target_lang or "zh"),
                    str(source_lang or ""),
                    str(source_lang or ""),
                    mid,
                ),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0

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

    # ── Phase 5：坐席 presence + 会话租约 ─────────────────────

    def upsert_agent_presence(
        self,
        agent_id: str,
        *,
        display_name: str = "",
        status: str = "online",
    ) -> Dict[str, Any]:
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        st = str(status or "online").strip().lower()
        if st not in {"online", "busy", "offline"}:
            raise ValueError(f"invalid status: {st}")
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_presence(agent_id, display_name, status, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name != ''
                        THEN excluded.display_name ELSE agent_presence.display_name END,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (aid, str(display_name or ""), st, now, now),
            )
            self._conn.commit()
        return self.get_agent_presence(aid) or {}

    def get_agent_presence(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_agent_presence(self, *, active_within_sec: float = 120) -> List[Dict[str, Any]]:
        cutoff = self._now() - max(0.0, float(active_within_sec or 0))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_presence WHERE last_seen_at >= ? ORDER BY last_seen_at DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_expired_claims(self) -> int:
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversation_claims WHERE expires_at > 0 AND expires_at < ?",
                (now,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def get_conversation_claim(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        self.purge_expired_claims()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_claims WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        if float(out.get("expires_at") or 0) < self._now():
            return None
        return out

    def get_agent_prefs(self, agent_id: str) -> Dict[str, Any]:
        """坐席告警偏好（不存在则返回全 0/默认=沿用全局、无免打扰）。"""
        aid = str(agent_id or "").strip()
        row = None
        if aid:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM agent_prefs WHERE agent_id=?", (aid,)
                ).fetchone()
        if row is None:
            return {"agent_id": aid, "warn_sec": 0, "crit_sec": 0,
                    "muted": 0, "dnd_start": -1, "dnd_end": -1, "updated_at": 0}
        return dict(row)

    def set_agent_prefs(
        self, agent_id: str, *, warn_sec: int = 0, crit_sec: int = 0,
        muted: int = 0, dnd_start: int = -1, dnd_end: int = -1,
    ) -> Dict[str, Any]:
        """写坐席告警偏好（整条覆盖）。"""
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, warn_sec, crit_sec, muted, "
                "dnd_start, dnd_end, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET warn_sec=excluded.warn_sec, "
                "crit_sec=excluded.crit_sec, muted=excluded.muted, "
                "dnd_start=excluded.dnd_start, dnd_end=excluded.dnd_end, "
                "updated_at=excluded.updated_at",
                (aid, int(warn_sec or 0), int(crit_sec or 0), 1 if muted else 0,
                 int(dnd_start), int(dnd_end), now))
            self._conn.commit()
        return self.get_agent_prefs(aid)

    def record_escalation(
        self, conversation_id: str, *, reason: str = "", agent_id: str = "",
        agent_name: str = "", wait_sec: int = 0, dedup_sec: float = 3600,
        ts: Optional[float] = None,
    ) -> bool:
        """记录一次会话升级（问责审计）。dedup_sec 内同会话已记过则跳过。

        返回 True=本次新记录（边沿），False=去重跳过。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        now = float(ts if ts is not None else self._now())
        with self._lock:
            if dedup_sec > 0:
                row = self._conn.execute(
                    "SELECT 1 FROM escalations WHERE conversation_id=? AND ts>=? "
                    "LIMIT 1", (cid, now - float(dedup_sec))).fetchone()
                if row is not None:
                    return False
            self._conn.execute(
                "INSERT INTO escalations (conversation_id, reason, agent_id, "
                "agent_name, wait_sec, ts) VALUES (?,?,?,?,?,?)",
                (cid, str(reason or ""), str(agent_id or ""),
                 str(agent_name or ""), int(wait_sec or 0), now))
            self._conn.commit()
        return True

    def escalation_takeovers(
        self, since_ts: float = 0.0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """升级历史 + 接管时延：每条升级关联其后首个 agent_send（人工接管）。

        taken_ts/taken_by 为 None ⇒ 升级后尚无人工接管。聚合交调用方。
        """
        sql = (
            "SELECT e.id AS id, e.conversation_id AS cid, e.reason AS reason, "
            "  e.agent_id AS agent_id, e.agent_name AS agent_name, "
            "  e.wait_sec AS wait_sec, e.ts AS ts, "
            "  (SELECT MIN(s.ts) FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts) AS taken_ts, "
            "  (SELECT s.agent_id FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts "
            "   ORDER BY s.ts ASC LIMIT 1) AS taken_by "
            "FROM escalations e WHERE e.ts>=? ORDER BY e.ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (float(since_ts), int(max(1, min(1000, limit))))).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            taken = r["taken_ts"]
            out.append({
                "id": int(r["id"]),
                "conversation_id": str(r["cid"]),
                "reason": str(r["reason"] or ""),
                "agent_id": str(r["agent_id"] or ""),
                "agent_name": str(r["agent_name"] or ""),
                "wait_sec": int(r["wait_sec"] or 0),
                "ts": float(r["ts"] or 0),
                "taken_ts": float(taken) if taken is not None else None,
                "taken_by": str(r["taken_by"]) if r["taken_by"] is not None else "",
            })
        return out

    def count_escalations_since(self, since_ts: float = 0.0) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM escalations WHERE ts>=?", (float(since_ts),)
            ).fetchone()[0]

    def list_escalations(
        self, since_ts: float = 0.0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM escalations WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                (float(since_ts), int(max(1, min(1000, limit)))),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Phase 6-24：定向升级 / 指定主管指派 ──────────────────────────

    def set_escalation_assigned(self, esc_id: int, assigned_to: str) -> bool:
        """把一条升级记录指派给指定主管（写 assigned_to，幂等）。返回是否有行更新。"""
        aid = str(assigned_to or "").strip()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE escalations SET assigned_to=? WHERE id=?",
                (aid, int(esc_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def count_assigned_escalations(
        self, agent_id: str, since_ts: float = 0.0,
    ) -> int:
        """某主管在 since_ts 之后被指派的升级条数（用于负载均衡选最轻的主管）。"""
        aid = str(agent_id or "").strip()
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM escalations "
                "WHERE assigned_to=? AND ts>=?",
                (aid, float(since_ts)),
            ).fetchone()[0]

    def list_my_escalations(
        self,
        agent_id: str,
        since_ts: float = 0.0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """返回指派给 agent_id 的升级列表（主管个人视图，含接管时延）。"""
        aid = str(agent_id or "").strip()
        sql = (
            "SELECT e.id AS id, e.conversation_id AS cid, e.reason AS reason, "
            "  e.agent_id AS agent_id, e.agent_name AS agent_name, "
            "  e.wait_sec AS wait_sec, e.ts AS ts, e.assigned_to AS assigned_to, "
            "  (SELECT MIN(s.ts) FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts) AS taken_ts, "
            "  (SELECT s.agent_id FROM agent_sends s "
            "   WHERE s.conversation_id=e.conversation_id AND s.ts>=e.ts "
            "   ORDER BY s.ts ASC LIMIT 1) AS taken_by "
            "FROM escalations e WHERE e.assigned_to=? AND e.ts>=? "
            "ORDER BY e.ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (aid, float(since_ts), int(max(1, min(500, limit))))
            ).fetchall()
        out = []
        for r in rows:
            taken = r["taken_ts"]
            out.append({
                "id": int(r["id"]),
                "conversation_id": str(r["cid"]),
                "reason": str(r["reason"] or ""),
                "agent_id": str(r["agent_id"] or ""),
                "agent_name": str(r["agent_name"] or ""),
                "wait_sec": int(r["wait_sec"] or 0),
                "ts": float(r["ts"] or 0),
                "assigned_to": str(r["assigned_to"] or ""),
                "taken_ts": float(taken) if taken is not None else None,
                "taken_by": str(r["taken_by"]) if r["taken_by"] is not None else "",
                "takeover_sec": int(float(taken) - float(r["ts"])) if taken else None,
            })
        return out

    def list_conversation_claims(self) -> List[Dict[str, Any]]:
        self.purge_expired_claims()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_claims ORDER BY claimed_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        agent_name: str = "",
        ttl_sec: float = 900,
        force: bool = False,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid or not aid:
            raise ValueError("conversation_id and agent_id required")
        self.purge_expired_claims()
        existing = self.get_conversation_claim(cid)
        if existing and existing.get("agent_id") != aid and not force:
            return {
                "ok": False,
                "reason": "already_claimed",
                "claim": existing,
            }
        now = self._now()
        exp = now + max(60.0, float(ttl_sec or 900))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_claims
                    (conversation_id, agent_id, agent_name, claimed_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agent_name = excluded.agent_name,
                    claimed_at = excluded.claimed_at,
                    expires_at = excluded.expires_at
                """,
                (cid, aid, str(agent_name or ""), now, exp),
            )
            self._conn.commit()
        claim = self.get_conversation_claim(cid) or {}
        return {"ok": True, "claim": claim}

    def renew_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        ttl_sec: float = 900,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        existing = self.get_conversation_claim(cid)
        if not existing:
            return {"ok": False, "reason": "not_claimed"}
        if existing.get("agent_id") != aid:
            return {"ok": False, "reason": "not_owner", "claim": existing}
        now = self._now()
        exp = now + max(60.0, float(ttl_sec or 900))
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_claims SET expires_at = ? WHERE conversation_id = ?",
                (exp, cid),
            )
            self._conn.commit()
        return {"ok": True, "claim": self.get_conversation_claim(cid)}

    def release_conversation_claim(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        existing = self.get_conversation_claim(cid)
        if not existing:
            return {"ok": True, "released": False}
        if existing.get("agent_id") != aid and not force:
            return {"ok": False, "reason": "not_owner", "claim": existing}
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversation_claims WHERE conversation_id = ?", (cid,)
            )
            self._conn.commit()
        return {"ok": True, "released": True, "conversation_id": cid}
