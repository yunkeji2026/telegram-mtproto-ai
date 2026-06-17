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

-- P1：出向译文旁路表。坐席「一击直发」让后端把中文原文译成客户语言后投递，
-- 但出向消息回到 messages 的路径异构（web record_message / protocol worker push /
-- 多数 RPA 根本不回读），无法在 messages 里稳定保存「中文原文 ↔ 实发译文」配对。
-- 故此处旁路记录，按 (conversation_id, 实发译文 hash) 键；thread 读取时富集回原文，
-- 实现跨刷新/重启/设备的出向双行展示，且完全不触碰 messages 去重。
CREATE TABLE IF NOT EXISTS outbound_translations (
    conversation_id   TEXT NOT NULL,
    sent_hash         TEXT NOT NULL,
    original_text     TEXT NOT NULL DEFAULT '',
    source_lang       TEXT NOT NULL DEFAULT '',
    target_lang       TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    error             TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, sent_hash)
);
CREATE INDEX IF NOT EXISTS idx_outxl_conv ON outbound_translations(conversation_id);

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
    # B2: 草稿强制审计日志（安全不变量：L4 拦截 / force-override / autosend 全部留档）
    """CREATE TABLE IF NOT EXISTS draft_audit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id        TEXT NOT NULL DEFAULT '',
        autopilot_level TEXT NOT NULL DEFAULT '',
        action          TEXT NOT NULL DEFAULT '',
        agent_id        TEXT NOT NULL DEFAULT '',
        reason          TEXT NOT NULL DEFAULT '',
        risk_level      TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        ts              REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_draft ON draft_audit_log(draft_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_ts    ON draft_audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_agent ON draft_audit_log(agent_id, ts)",
    # I1: 对话智能分析元数据（最近意图/情绪趋势/风险）
    """CREATE TABLE IF NOT EXISTS conversation_meta (
        conversation_id  TEXT PRIMARY KEY,
        platform         TEXT NOT NULL DEFAULT '',
        last_intent      TEXT NOT NULL DEFAULT '',
        last_emotion     TEXT NOT NULL DEFAULT '',
        last_risk        TEXT NOT NULL DEFAULT 'low',
        intent_history   TEXT NOT NULL DEFAULT '[]',
        emotion_history  TEXT NOT NULL DEFAULT '[]',
        msg_count        INTEGER NOT NULL DEFAULT 0,
        updated_at       REAL NOT NULL DEFAULT 0
    )""",
    # M1: conversation_meta 新增 csat_score 列
    "ALTER TABLE conversation_meta ADD COLUMN csat_score REAL NOT NULL DEFAULT -1",
    # N1: conversation_meta 新增 contact_id 列（用于跨平台会话归档）
    "ALTER TABLE conversation_meta ADD COLUMN contact_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_contact ON conversation_meta(contact_id)",
    # P3: 多租户 workspace_id 列（默认 'default'，向下兼容）
    "ALTER TABLE conversation_meta ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE draft_audit_log ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
    """CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id   TEXT PRIMARY KEY,
        display_name   TEXT NOT NULL DEFAULT '',
        config_json    TEXT NOT NULL DEFAULT '{}',
        created_at     REAL NOT NULL DEFAULT 0,
        updated_at     REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_workspace ON conversation_meta(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_draft_audit_workspace ON draft_audit_log(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_updated ON conversation_meta(updated_at DESC)",
    # Q1: conversation_meta 新增 summary 列（对话摘要自动归档）
    "ALTER TABLE conversation_meta ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    # Q2: reply_drafts 新增质量评分列
    "ALTER TABLE reply_drafts ADD COLUMN quality_score REAL NOT NULL DEFAULT -1",
    "ALTER TABLE reply_drafts ADD COLUMN quality_breakdown TEXT NOT NULL DEFAULT '{}'",
    # Q3: KB 推荐命中率记录表
    """CREATE TABLE IF NOT EXISTS kb_recommendation_log (
        id              TEXT PRIMARY KEY,
        entry_id        TEXT NOT NULL DEFAULT '',
        entry_title     TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        agent_id        TEXT NOT NULL DEFAULT '',
        recommended_ts  REAL NOT NULL DEFAULT 0,
        clicked         INTEGER NOT NULL DEFAULT 0,
        used_in_draft   INTEGER NOT NULL DEFAULT 0,
        draft_id        TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_kb_rec_entry ON kb_recommendation_log(entry_id)",
    "CREATE INDEX IF NOT EXISTS idx_kb_rec_ts    ON kb_recommendation_log(recommended_ts DESC)",
    # R3: CSAT 问卷表
    """CREATE TABLE IF NOT EXISTS csat_surveys (
        id               TEXT PRIMARY KEY,
        conversation_id  TEXT NOT NULL DEFAULT '',
        draft_id         TEXT NOT NULL DEFAULT '',
        agent_id         TEXT NOT NULL DEFAULT '',
        scheduled_at     REAL NOT NULL DEFAULT 0,
        send_at          REAL NOT NULL DEFAULT 0,
        sent             INTEGER NOT NULL DEFAULT 0,
        response_score   INTEGER NOT NULL DEFAULT -1,
        response_ts      REAL NOT NULL DEFAULT 0,
        created_at       REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_survey_conv ON csat_surveys(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_survey_due  ON csat_surveys(send_at, sent)",
    # S1: A/B 测试表
    """CREATE TABLE IF NOT EXISTS ab_tests (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL DEFAULT '',
        intent_filter   TEXT NOT NULL DEFAULT '',
        template_a_id   TEXT NOT NULL DEFAULT '',
        template_b_id   TEXT NOT NULL DEFAULT '',
        description     TEXT NOT NULL DEFAULT '',
        min_sample      INTEGER NOT NULL DEFAULT 30,
        status          TEXT NOT NULL DEFAULT 'active',
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL DEFAULT 0,
        updated_at      REAL NOT NULL DEFAULT 0,
        n_a             INTEGER NOT NULL DEFAULT 0,
        n_b             INTEGER NOT NULL DEFAULT 0,
        sat_a           INTEGER NOT NULL DEFAULT 0,
        sat_b           INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS ab_assignments (
        test_id         TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        variant         TEXT NOT NULL DEFAULT 'A',
        assigned_ts     REAL NOT NULL DEFAULT 0,
        csat_score      REAL NOT NULL DEFAULT -1,
        outcome_ts      REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (test_id, conversation_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ab_assign_conv ON ab_assignments(conversation_id)",
    # S3: 全链路追踪 trace_id
    "ALTER TABLE conversation_meta ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE reply_drafts ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_conv_trace ON conversation_meta(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_draft_trace ON reply_drafts(trace_id)",
    # I3: 回复模板库
    """CREATE TABLE IF NOT EXISTS reply_templates (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        language     TEXT NOT NULL DEFAULT 'zh',
        platform     TEXT NOT NULL DEFAULT '',
        scene        TEXT NOT NULL DEFAULT '',
        created_by   TEXT NOT NULL DEFAULT 'system',
        created_at   REAL NOT NULL DEFAULT 0,
        updated_at   REAL NOT NULL DEFAULT 0,
        used_count   INTEGER NOT NULL DEFAULT 0,
        is_active    INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_templates_scene    ON reply_templates(scene, language, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_templates_platform ON reply_templates(platform, language, is_active)",
    # T1: 会话级标签 + 归档（Phase 14）
    "ALTER TABLE conversation_meta ADD COLUMN conv_tags TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE conversation_meta ADD COLUMN archived  INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_archived ON conversation_meta(archived)",
    # U1: FTS5 全文索引（Phase 22）
    # FTS5 虚拟表：独立存储，触发器同步，搜索降级至 LIKE 若不可用
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
       USING fts5(message_id UNINDEXED, conversation_id UNINDEXED,
                  text, ts UNINDEXED, direction UNINDEXED,
                  tokenize='unicode61 remove_diacritics 1')""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
         INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
         VALUES (new.message_id, new.conversation_id, new.text, new.ts, new.direction);
       END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
         DELETE FROM messages_fts WHERE message_id = old.message_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
         DELETE FROM messages_fts WHERE message_id = old.message_id;
         INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
         VALUES (new.message_id, new.conversation_id, new.text, new.ts, new.direction);
       END""",
    # Y1: QA 质检评分 + 流失风险（Phase 34/35）
    "ALTER TABLE conversation_meta ADD COLUMN qa_score TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE conversation_meta ADD COLUMN churn_risk TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN auto_archived_at REAL NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_meta_last_ts ON conversation_meta(updated_at DESC)",
    # V1: 坐席协作注解（Phase 25）
    """CREATE TABLE IF NOT EXISTS conv_notes (
         note_id    TEXT PRIMARY KEY,
         conversation_id TEXT NOT NULL,
         agent_id   TEXT NOT NULL DEFAULT '',
         agent_name TEXT NOT NULL DEFAULT '',
         body       TEXT NOT NULL DEFAULT '',
         mentions   TEXT NOT NULL DEFAULT '[]',
         ts         REAL NOT NULL DEFAULT 0,
         edited_ts  REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_notes_conv ON conv_notes(conversation_id, ts DESC)",
    # AA1: 自定义动作 + 工作链（Phase 37）
    """CREATE TABLE IF NOT EXISTS workflow_actions (
         action_id   TEXT PRIMARY KEY,
         name        TEXT NOT NULL DEFAULT '',
         action_type TEXT NOT NULL DEFAULT 'template',
         config_json TEXT NOT NULL DEFAULT '{}',
         icon        TEXT NOT NULL DEFAULT '💡',
         enabled     INTEGER NOT NULL DEFAULT 1,
         sort_order  INTEGER NOT NULL DEFAULT 0,
         created_at  REAL NOT NULL DEFAULT 0,
         updated_at  REAL NOT NULL DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS workflow_chains (
         chain_id    TEXT PRIMARY KEY,
         name        TEXT NOT NULL DEFAULT '',
         steps_json  TEXT NOT NULL DEFAULT '[]',
         trigger_conditions TEXT NOT NULL DEFAULT '{}',
         enabled     INTEGER NOT NULL DEFAULT 1,
         created_at  REAL NOT NULL DEFAULT 0,
         updated_at  REAL NOT NULL DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS workflow_executions (
         exec_id       TEXT PRIMARY KEY,
         chain_id      TEXT NOT NULL DEFAULT '',
         conversation_id TEXT NOT NULL DEFAULT '',
         current_step  INTEGER NOT NULL DEFAULT 0,
         status        TEXT NOT NULL DEFAULT 'pending',
         context_json  TEXT NOT NULL DEFAULT '{}',
         started_at    REAL NOT NULL DEFAULT 0,
         updated_at    REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_wf_exec_conv ON workflow_executions(conversation_id, updated_at DESC)",
    "ALTER TABLE workflow_executions ADD COLUMN next_step_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE workflow_executions ADD COLUMN last_result_json TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_wf_exec_due ON workflow_executions(status, next_step_at)",
    # DD1: 关系阶段缓存（P43 进阶检测）
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_cached TEXT NOT NULL DEFAULT ''",
    # P46: 关系阶段人工确认
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_pending TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversation_meta ADD COLUMN rel_stage_pending_ts REAL NOT NULL DEFAULT 0",
    "ALTER TABLE conversation_meta ADD COLUMN rel_reunion_ack_ts REAL NOT NULL DEFAULT 0",
    # P50: 客户级关系阶段（跨会话同步）
    """CREATE TABLE IF NOT EXISTS contact_rel_stage (
         contact_id       TEXT PRIMARY KEY,
         confirmed_stage  TEXT NOT NULL DEFAULT '',
         updated_by       TEXT NOT NULL DEFAULT '',
         updated_at       REAL NOT NULL DEFAULT 0,
         reunion_ack_ts   REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_contact_rel_stage_updated ON contact_rel_stage(updated_at DESC)",
    # BB1: 分流路由规则（Phase 38）
    """CREATE TABLE IF NOT EXISTS routing_rules (
         rule_id    TEXT PRIMARY KEY,
         name       TEXT NOT NULL DEFAULT '',
         conditions TEXT NOT NULL DEFAULT '{}',
         assign_to  TEXT NOT NULL DEFAULT '',
         priority   INTEGER NOT NULL DEFAULT 0,
         enabled    INTEGER NOT NULL DEFAULT 1,
         created_at REAL NOT NULL DEFAULT 0,
         updated_at REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_routing_rules_priority ON routing_rules(priority DESC, enabled)",
    # CC1: 剧本话题 + 互动积分（Phase 40/41）
    """CREATE TABLE IF NOT EXISTS script_topics (
         topic_id    TEXT PRIMARY KEY,
         stage       TEXT NOT NULL DEFAULT 'initial',
         title       TEXT NOT NULL DEFAULT '',
         opener      TEXT NOT NULL DEFAULT '',
         hint        TEXT NOT NULL DEFAULT '',
         tags_json   TEXT NOT NULL DEFAULT '[]',
         chain_id    TEXT NOT NULL DEFAULT '',
         enabled     INTEGER NOT NULL DEFAULT 1,
         sort_order  INTEGER NOT NULL DEFAULT 0,
         created_at  REAL NOT NULL DEFAULT 0,
         updated_at  REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_script_topics_stage ON script_topics(stage, enabled, sort_order)",
    """CREATE TABLE IF NOT EXISTS contact_engagement (
         contact_id       TEXT PRIMARY KEY,
         points           INTEGER NOT NULL DEFAULT 0,
         level            TEXT NOT NULL DEFAULT 'new',
         breakdown_json   TEXT NOT NULL DEFAULT '{}',
         achievements_json TEXT NOT NULL DEFAULT '[]',
         history_json     TEXT NOT NULL DEFAULT '[]',
         updated_at       REAL NOT NULL DEFAULT 0
       )""",
    # P61-3：分组批量触达（再激活）日志——cooldown 判定 + 回执统计
    """CREATE TABLE IF NOT EXISTS outreach_log (
         id              INTEGER PRIMARY KEY AUTOINCREMENT,
         conversation_id TEXT NOT NULL,
         batch_id        TEXT NOT NULL DEFAULT '',
         platform        TEXT NOT NULL DEFAULT '',
         account_id      TEXT NOT NULL DEFAULT '',
         status          TEXT NOT NULL DEFAULT 'sent',
         note            TEXT NOT NULL DEFAULT '',
         ts              REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_outreach_conv ON outreach_log(conversation_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_batch ON outreach_log(batch_id)",
    # P3：坐席技能语言（CSV 规范 ISO 码，如 "en,ja"）。供 auto_assign 的 match_language
    # 把外语会话优先派给会该语言的坐席（坐席在工作台「我的偏好」声明）。
    "ALTER TABLE agent_prefs ADD COLUMN languages TEXT NOT NULL DEFAULT ''",
    # P3：出向翻译漏斗「按日」持久化聚合（看板按 7/30 日窗读取，跨重启/含趋势线）。
    # 与内存版 OutboundTranslationStats 同口径；day 用本地日期，与 dashboard 其它面板分桶一致。
    # by_lang_json 为该日各目标语译出次数的 JSON（读改写，已在锁内）。
    """CREATE TABLE IF NOT EXISTS outbound_xlate_daily (
         day             TEXT PRIMARY KEY,
         sends           INTEGER NOT NULL DEFAULT 0,
         requested       INTEGER NOT NULL DEFAULT 0,
         translated      INTEGER NOT NULL DEFAULT 0,
         skipped         INTEGER NOT NULL DEFAULT 0,
         failed          INTEGER NOT NULL DEFAULT 0,
         auto_requested  INTEGER NOT NULL DEFAULT 0,
         auto_unresolved INTEGER NOT NULL DEFAULT 0,
         degraded        INTEGER NOT NULL DEFAULT 0,
         by_lang_json    TEXT NOT NULL DEFAULT '{}'
       )""",
    # P3：入站翻译漏斗「按日」持久化（客户→坐席自动翻译）。语义与出向不同：入站为打开会话
    # 时懒翻译，只记「新译出」（成功后即缓存，再开走 store 不重复计）与 failed；by_lang_json
    # 为该日各「客户来源语言」译出次数（与出向的目标语分布合成跨语言总览）。
    """CREATE TABLE IF NOT EXISTS inbound_xlate_daily (
         day          TEXT PRIMARY KEY,
         translated   INTEGER NOT NULL DEFAULT 0,
         failed       INTEGER NOT NULL DEFAULT 0,
         by_lang_json TEXT NOT NULL DEFAULT '{}'
       )""",
    # P3：自动派单（AutoClaimWorker）「按日」持久化。进程内 status_snapshot 是累计且重启清零，
    # 看板需按 7/30 日窗回溯，故落表。claimed=当日自动认领总数，lang_matched=其中按语言命中数，
    # by_lang_json=命中派单的会话语言分布（系统按哪些语言在精准路由）。
    """CREATE TABLE IF NOT EXISTS auto_claim_daily (
         day          TEXT PRIMARY KEY,
         claimed      INTEGER NOT NULL DEFAULT 0,
         lang_matched INTEGER NOT NULL DEFAULT 0,
         by_lang_json TEXT NOT NULL DEFAULT '{}'
       )""",
    # E2：运维事件（health_alert 闭环）。watchdog 红/黄告警按 signature 去重落表，
    # 恢复时自动 resolve；主管可 ack/指派。让系统告警可追踪到处理人而非只推一条通知。
    """CREATE TABLE IF NOT EXISTS ops_incidents (
         id            INTEGER PRIMARY KEY AUTOINCREMENT,
         kind          TEXT NOT NULL DEFAULT 'health',
         signature     TEXT NOT NULL DEFAULT '',
         light         TEXT NOT NULL DEFAULT '',
         summary_json  TEXT NOT NULL DEFAULT '{}',
         problems_json TEXT NOT NULL DEFAULT '[]',
         status        TEXT NOT NULL DEFAULT 'open',
         assigned_to   TEXT NOT NULL DEFAULT '',
         opened_ts     REAL NOT NULL DEFAULT 0,
         updated_ts    REAL NOT NULL DEFAULT 0,
         acked_ts      REAL NOT NULL DEFAULT 0,
         resolved_ts   REAL NOT NULL DEFAULT 0
       )""",
    "CREATE INDEX IF NOT EXISTS idx_ops_incidents_status ON ops_incidents(status, opened_ts)",
    "CREATE INDEX IF NOT EXISTS idx_ops_incidents_sig    ON ops_incidents(kind, signature, status)",
    # 存量库（本分支早期已建表者）补 kind 列
    "ALTER TABLE ops_incidents ADD COLUMN kind TEXT NOT NULL DEFAULT 'health'",
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
        # C3：L2 草稿写入通知钩子（AutosendWorker 注册，线程安全回调）
        self._l2_callbacks: List[Any] = []
        # E2：入站新消息通知钩子（AutoDraft 注册，参数 conv_dict + text）
        self._new_inbound_cbs: List[Any] = []
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
            # U1: FTS5 冷启动重建（首次建表后一次性填充存量消息，后续由触发器维护）
            self._fts5_available = self._rebuild_fts5_if_empty()

    def _rebuild_fts5_if_empty(self) -> bool:
        """U1：检查 FTS5 表是否可用且已填充；若空则从存量 messages 批量导入。

        返回 True 表示 FTS5 可用（可用于 search_messages 优先路径）。
        """
        try:
            # 确认 messages_fts 表存在
            self._conn.execute("SELECT count(*) FROM messages_fts LIMIT 1").fetchone()
        except Exception:
            return False  # FTS5 不可用（SQLite 编译时未包含）
        try:
            # 若 FTS5 表为空且 messages 表有数据，执行全量同步（best-effort）
            fts_cnt = self._conn.execute(
                "SELECT count(*) FROM messages_fts"
            ).fetchone()[0]
            if fts_cnt == 0:
                self._conn.execute(
                    """INSERT INTO messages_fts(message_id, conversation_id, text, ts, direction)
                       SELECT message_id, conversation_id, text, ts, direction
                       FROM messages WHERE text != ''"""
                )
                self._conn.commit()
            return True
        except Exception:
            return False  # 插入失败，降级为 LIKE

    def register_l2_callback(self, cb: Any) -> None:
        """注册 L2 草稿写入通知回调（C3 事件驱动 AutosendWorker）。

        cb 为无参可调用对象，在 upsert_draft 写入 L2 草稿后从同步上下文调用。
        实现方可用 loop.call_soon_threadsafe 安全地唤醒异步任务。
        """
        self._l2_callbacks.append(cb)

    def register_new_inbound_cb(self, cb: Any) -> None:
        """注册入站新消息通知回调（E2 自动草稿生成）。

        cb 签名：cb(conv: dict, text: str)，在 ingest 检测到新入站消息后调用。
        在 ingest 锁外调用，best-effort，异常自动静默。
        """
        self._new_inbound_cbs.append(cb)

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

    def search_messages(
        self, query: str, *, limit: int = 20, platform: str = ""
    ) -> List[Dict[str, Any]]:
        """Phase 22（U1）：跨会话消息全文检索。

        优先路径：FTS5 MATCH（精准分词 + rank 排序）。
        降级路径：SQLite LIKE（兜底，FTS5 不可用时自动切换）。

        每个命中返回：message_id, conversation_id, text, ts, direction,
                      platform, display_name, fts_mode（'fts5'|'like'）。
        """
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(100, int(limit or 20)))

        # FTS5 优先路径：支持 phrase、prefix、NOT 等高级语法
        if getattr(self, "_fts5_available", False):
            try:
                return self._search_messages_fts5(query, limit=limit, platform=platform)
            except Exception:
                pass  # FTS5 查询失败（特殊字符/语法错），降级 LIKE

        return self._search_messages_like(query, limit=limit, platform=platform)

    def _search_messages_fts5(
        self, query: str, *, limit: int, platform: str
    ) -> List[Dict[str, Any]]:
        """FTS5 全文检索路径（Phase 22）。"""
        # 净化 query：去掉 FTS5 特殊字符，防止语法报错
        safe_q = query.replace('"', '').replace("'", "")
        params: List[Any] = [safe_q]
        extra = ""
        if platform:
            extra = " AND c.platform = ?"
            params.append(str(platform))
        params.append(limit)
        sql = f"""
            SELECT f.message_id, f.conversation_id, f.text, f.ts, f.direction,
                   c.platform, c.display_name, 'fts5' AS fts_mode
            FROM messages_fts f
            LEFT JOIN conversations c ON c.conversation_id = f.conversation_id
            WHERE messages_fts MATCH ?{extra}
            ORDER BY rank
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _search_messages_like(
        self, query: str, *, limit: int, platform: str
    ) -> List[Dict[str, Any]]:
        """LIKE 降级路径（Phase 21 / 22 兜底）。"""
        like = f"%{query}%"
        params: List[Any] = [like]
        extra = ""
        if platform:
            extra = " AND c.platform = ?"
            params.append(str(platform))
        params.append(limit)
        sql = f"""
            SELECT m.message_id, m.conversation_id, m.text, m.ts, m.direction,
                   c.platform, c.display_name, 'like' AS fts_mode
            FROM messages m
            LEFT JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE m.text LIKE ?{extra}
            ORDER BY m.ts DESC
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

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

    @staticmethod
    def _sent_hash(sent_text: str) -> str:
        return hashlib.sha256(str(sent_text or "").encode("utf-8")).hexdigest()[:16]

    def record_outbound_translation(
        self,
        conversation_id: str,
        sent_text: str,
        original_text: str,
        *,
        source_lang: str = "",
        target_lang: str = "",
        provider: str = "",
        error: str = "",
    ) -> bool:
        """P1：记录一条出向译文 → 原文/质量映射（一击直发后供 thread 富集双行）。

        按 (conversation_id, hash(实发译文)) 去重 upsert；译文与原文相同（未真正翻译）
        时不记录，避免无意义副行。best-effort，调用方包 try。
        """
        cid = str(conversation_id or "").strip()
        sent = str(sent_text or "").strip()
        orig = str(original_text or "").strip()
        if not cid or not sent or not orig or sent == orig:
            return False
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO outbound_translations
                    (conversation_id, sent_hash, original_text, source_lang,
                     target_lang, provider, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, sent_hash) DO UPDATE SET
                    original_text = excluded.original_text,
                    source_lang   = excluded.source_lang,
                    target_lang   = excluded.target_lang,
                    provider      = excluded.provider,
                    error         = excluded.error,
                    created_at    = excluded.created_at
                """,
                (cid, self._sent_hash(sent), orig, str(source_lang or ""),
                 str(target_lang or ""), str(provider or ""), str(error or ""),
                 self._now()),
            )
            self._conn.commit()
        return True

    def get_outbound_translations(self, conversation_id: str) -> Dict[str, Dict[str, Any]]:
        """返回该会话的出向译文映射 {sent_hash: {original_text, target_lang, provider, error}}。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT sent_hash, original_text, source_lang, target_lang, provider, error"
                " FROM outbound_translations WHERE conversation_id = ?",
                (cid,),
            ).fetchall()
        return {
            str(r["sent_hash"]): {
                "original_text": r["original_text"],
                "source_lang": r["source_lang"],
                "target_lang": r["target_lang"],
                "provider": r["provider"],
                "error": r["error"],
            }
            for r in rows
        }

    def record_outbound_xlate(
        self,
        *,
        requested: bool,
        is_auto: bool = False,
        auto_resolved: Optional[bool] = None,
        translated: bool = False,
        target_lang: str = "",
        degraded: bool = False,
        failed: bool = False,
    ) -> None:
        """P3：把一次出向发送的翻译漏斗结果累计进「按日」表（看板窗口读取，跨重启）。

        口径与内存版 ``OutboundTranslationStats.record_send`` 完全一致：
        failed 与 translated 互斥优先 failed；skipped = 请求了但未译且未失败。
        day 用本地日期，与 dashboard 其它面板的 day 分桶对齐。best-effort，调用方包 try。
        """
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        inc_translated = 1 if (translated and not failed) else 0
        inc_skipped = 1 if (requested and not failed and not inc_translated) else 0
        inc_degraded = 1 if (inc_translated and degraded) else 0
        inc_auto_req = 1 if is_auto else 0
        inc_auto_unres = 1 if (is_auto and auto_resolved is False) else 0
        inc_requested = 1 if requested else 0
        inc_failed = 1 if failed else 0
        lang = (str(target_lang or "").strip() or "unknown") if inc_translated else ""
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM outbound_xlate_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                by_lang: Dict[str, int] = {}
                if lang:
                    by_lang[lang] = 1
                self._conn.execute(
                    """INSERT INTO outbound_xlate_daily
                         (day, sends, requested, translated, skipped, failed,
                          auto_requested, auto_unresolved, degraded, by_lang_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (day, 1, inc_requested, inc_translated, inc_skipped, inc_failed,
                     inc_auto_req, inc_auto_unres, inc_degraded,
                     json.dumps(by_lang, ensure_ascii=False)),
                )
            else:
                try:
                    by_lang = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    by_lang = {}
                if lang:
                    by_lang[lang] = int(by_lang.get(lang, 0)) + 1
                self._conn.execute(
                    """UPDATE outbound_xlate_daily SET
                         sends = sends + 1,
                         requested = requested + ?,
                         translated = translated + ?,
                         skipped = skipped + ?,
                         failed = failed + ?,
                         auto_requested = auto_requested + ?,
                         auto_unresolved = auto_unresolved + ?,
                         degraded = degraded + ?,
                         by_lang_json = ?
                       WHERE day = ?""",
                    (inc_requested, inc_translated, inc_skipped, inc_failed,
                     inc_auto_req, inc_auto_unres, inc_degraded,
                     json.dumps(by_lang, ensure_ascii=False), day),
                )
            self._conn.commit()

    def get_outbound_xlate_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的出向翻译漏斗按日聚合（看板窗口数据 + 趋势）。

        返回与内存版 ``dump()`` 同形的 totals/coverage/by_target_lang，并附 ``trend``
        （每日 sends/translated/coverage 百分比，供 sparkPct 折线）。
        """
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, sends, requested, translated, skipped, failed,"
                " auto_requested, auto_unresolved, degraded, by_lang_json"
                " FROM outbound_xlate_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        tot = {"sends_total": 0, "requested": 0, "translated": 0, "skipped": 0,
               "failed": 0, "auto_requested": 0, "auto_unresolved": 0, "degraded": 0}
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            s = int(r["sends"] or 0)
            t = int(r["translated"] or 0)
            tot["sends_total"] += s
            tot["requested"] += int(r["requested"] or 0)
            tot["translated"] += t
            tot["skipped"] += int(r["skipped"] or 0)
            tot["failed"] += int(r["failed"] or 0)
            tot["auto_requested"] += int(r["auto_requested"] or 0)
            tot["auto_unresolved"] += int(r["auto_unresolved"] or 0)
            tot["degraded"] += int(r["degraded"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({"day": str(r["day"])[5:], "sends": s, "translated": t,
                          "cov_pct": round(t / s * 100, 1) if s else 0.0})
        sends = tot["sends_total"]
        areq = tot["auto_requested"]
        out = dict(tot)
        out["coverage_rate"] = round(tot["translated"] / sends, 4) if sends else 0
        out["auto_unresolved_rate"] = round(tot["auto_unresolved"] / areq, 4) if areq else 0
        out["by_target_lang"] = dict(sorted(by_lang.items()))
        out["trend"] = trend
        return out

    def record_inbound_xlate(
        self,
        *,
        translated: int = 0,
        failed: int = 0,
        by_lang: Optional[Dict[str, int]] = None,
    ) -> None:
        """P3：累计一次会话打开的入站翻译结果进「按日」表（客户→坐席）。

        translated 为本次**新译出**条数（命中 store 缓存的不计，避免重开重复计数）；
        by_lang 为这些新译出消息的客户来源语言分布。translated 与 failed 全 0 时不写。
        best-effort，调用方包 try。
        """
        translated = max(0, int(translated or 0))
        failed = max(0, int(failed or 0))
        if translated <= 0 and failed <= 0:
            return
        by_lang = {str(k): int(v) for k, v in (by_lang or {}).items() if int(v) > 0}
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM inbound_xlate_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO inbound_xlate_daily (day, translated, failed, by_lang_json)
                       VALUES (?,?,?,?)""",
                    (day, translated, failed, json.dumps(by_lang, ensure_ascii=False)),
                )
            else:
                try:
                    merged = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    merged = {}
                for k, v in by_lang.items():
                    merged[k] = int(merged.get(k, 0)) + int(v)
                self._conn.execute(
                    """UPDATE inbound_xlate_daily SET
                         translated = translated + ?,
                         failed = failed + ?,
                         by_lang_json = ?
                       WHERE day = ?""",
                    (translated, failed, json.dumps(merged, ensure_ascii=False), day),
                )
            self._conn.commit()

    def get_inbound_xlate_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的入站翻译按日聚合（客户来源语言分布 + 趋势）。"""
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, translated, failed, by_lang_json"
                " FROM inbound_xlate_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        translated = failed = 0
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            t = int(r["translated"] or 0)
            translated += t
            failed += int(r["failed"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({"day": str(r["day"])[5:], "translated": t})
        return {
            "translated": translated,
            "failed": failed,
            "by_source_lang": dict(sorted(by_lang.items())),
            "trend": trend,
        }

    def record_auto_claim(self, *, matched: bool, lang: str = "") -> None:
        """P3：累计一次自动派单进「按日」表。

        matched 表示该次派单是否按坐席语言命中；lang 为命中时的会话语言（用于分布）。
        best-effort，调用方包 try。
        """
        lang = str(lang or "").strip()
        day = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        inc_matched = 1 if matched else 0
        bl_inc = {lang: 1} if (matched and lang) else {}
        with self._lock:
            row = self._conn.execute(
                "SELECT by_lang_json FROM auto_claim_daily WHERE day = ?", (day,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO auto_claim_daily
                         (day, claimed, lang_matched, by_lang_json)
                       VALUES (?,?,?,?)""",
                    (day, 1, inc_matched, json.dumps(bl_inc, ensure_ascii=False)),
                )
            else:
                try:
                    merged = json.loads(row["by_lang_json"] or "{}")
                except Exception:
                    merged = {}
                for k, v in bl_inc.items():
                    merged[k] = int(merged.get(k, 0)) + int(v)
                self._conn.execute(
                    """UPDATE auto_claim_daily SET
                         claimed = claimed + 1,
                         lang_matched = lang_matched + ?,
                         by_lang_json = ?
                       WHERE day = ?""",
                    (inc_matched, json.dumps(merged, ensure_ascii=False), day),
                )
            self._conn.commit()

    def get_auto_claim_stats(self, since_ts: float) -> Dict[str, Any]:
        """P3：读取 since_ts 起的自动派单按日聚合（命中语言分布 + 趋势）。"""
        since_day = time.strftime("%Y-%m-%d", time.localtime(since_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, claimed, lang_matched, by_lang_json"
                " FROM auto_claim_daily WHERE day >= ? ORDER BY day",
                (since_day,),
            ).fetchall()
        claimed = lang_matched = 0
        by_lang: Dict[str, int] = {}
        trend = []
        for r in rows:
            c = int(r["claimed"] or 0)
            claimed += c
            lang_matched += int(r["lang_matched"] or 0)
            try:
                bl = json.loads(r["by_lang_json"] or "{}")
            except Exception:
                bl = {}
            for k, v in bl.items():
                by_lang[k] = by_lang.get(k, 0) + int(v)
            trend.append({"day": str(r["day"])[5:], "claimed": c})
        return {
            "claimed": claimed,
            "lang_matched": lang_matched,
            "by_lang": dict(sorted(by_lang.items())),
            "trend": trend,
        }

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
                     created_at, updated_at, trace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at = excluded.updated_at,
                    trace_id = CASE WHEN reply_drafts.trace_id = '' OR reply_drafts.trace_id IS NULL
                               THEN excluded.trace_id ELSE reply_drafts.trace_id END
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
                    now, now, str(draft.get("trace_id") or ""),
                ),
            )
            self._conn.commit()
        # C3：L2 落库后锁外通知（避免回调持锁引起死锁）
        if str(draft.get("autopilot_level") or "L1") == "L2":
            for _cb in self._l2_callbacks:
                try:
                    _cb()
                except Exception:
                    pass
        return draft_id

    def update_draft_status(
        self,
        draft_id: str,
        *,
        status: str,
        final_text: str = "",
        decided_by: str = "",
    ) -> bool:
        """H1/H2：通过 draft_id 直接更新草稿状态（适用于 inbox 源草稿）。

        返回 True 表示找到并更新了记录。
        """
        now = self._now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reply_drafts SET status=?, final_text=CASE WHEN ?!='' THEN ? ELSE final_text END, "
                "decided_by=?, decided_at=?, updated_at=? WHERE draft_id=?",
                (status, final_text, final_text, decided_by, now, now, draft_id),
            )
            self._conn.commit()
        return int(cur.rowcount or 0) > 0

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
        self,
        *,
        source_kind: str = "",
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
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
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
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

    def cleanup_old_drafts(
        self,
        *,
        max_age_days: int = 7,
        statuses: Optional[List[str]] = None,
    ) -> int:
        """H3：删除超龄的已处理草稿，防止 reply_drafts 表无限膨胀。

        仅删除 statuses 中指定状态的草稿（默认 approved/rejected/cancelled），
        绝不删除 pending 草稿（安全不变量）。
        返回实际删除的行数。
        """
        if statuses is None:
            statuses = ["approved", "rejected", "cancelled"]
        # 安全过滤：强制移除 pending，不允许误删待处理草稿
        safe_statuses = [s for s in statuses if s != "pending"]
        if not safe_statuses:
            return 0
        cutoff_ts = self._now() - max(1, int(max_age_days)) * 86400
        placeholders = ",".join("?" for _ in safe_statuses)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM reply_drafts WHERE status IN ({placeholders})"
                f" AND updated_at < ?",
                (*safe_statuses, cutoff_ts),
            )
            self._conn.commit()
        count = int(cur.rowcount or 0)
        if count > 0:
            logger.info("cleanup_old_drafts: 删除 %d 条超龄草稿（age>%dd statuses=%s）",
                        count, max_age_days, safe_statuses)
        return count

    def cleanup_outbound_translations(self, *, max_age_days: int = 30) -> int:
        """P3：删除超龄出向译文旁路记录，防 outbound_translations 表无限膨胀。

        出向译文映射仅为「双行展示」服务，超过保留期的历史会话基本不再回看，
        按 created_at 删除即可。返回删除行数。best-effort。
        """
        cutoff_ts = self._now() - max(1, int(max_age_days)) * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM outbound_translations WHERE created_at < ?",
                (cutoff_ts,),
            )
            self._conn.commit()
        count = int(cur.rowcount or 0)
        if count > 0:
            logger.info("cleanup_outbound_translations: 删除 %d 条超龄出向译文（age>%dd）",
                        count, max_age_days)
        return count

    # ── B2 草稿审计日志 ──────────────────────────────────────────

    def record_draft_audit(
        self,
        draft_id: str,
        *,
        autopilot_level: str = "",
        action: str = "",
        agent_id: str = "",
        reason: str = "",
        risk_level: str = "",
        conversation_id: str = "",
        ts: Optional[float] = None,
    ) -> int:
        """记录一条草稿处置审计事件，返回插入的 id。

        action 枚举：autosend（L2 自动）/ blocked（L4 拦截）/
                     force_override（主管强制放行）/ approved / rejected /
                     edit_send / cancelled。
        """
        now = float(ts if ts is not None else self._now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO draft_audit_log "
                "(draft_id, autopilot_level, action, agent_id, reason, "
                " risk_level, conversation_id, ts) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(draft_id or ""), str(autopilot_level or ""),
                    str(action or ""), str(agent_id or ""),
                    str(reason or ""), str(risk_level or ""),
                    str(conversation_id or ""), now,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_draft_audit(
        self,
        *,
        draft_id: str = "",
        agent_id: str = "",
        since_ts: float = 0.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查审计日志（可按 draft_id / agent_id / 时间过滤）。"""
        clauses: List[str] = ["ts>=?"]
        params: List[Any] = [float(since_ts)]
        if draft_id:
            clauses.append("draft_id=?")
            params.append(str(draft_id))
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        sql = (
            "SELECT * FROM draft_audit_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(int(max(1, min(1000, limit))))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Phase A / C1：坐席绩效聚合 ───────────────────────────

    def get_agent_perf(
        self,
        *,
        since_ts: float = 0.0,
        agent_id: str = "",
    ) -> List[Dict[str, Any]]:
        """按 agent_id 聚合 draft_audit_log，返回每坐席的草稿处置绩效。

        返回字段：agent_id, total, approved, rejected, blocked,
                   force_override, autosend, avg_action（秒），
                   last_action_ts
        """
        clauses = ["dal.ts>=?"]
        params: List[Any] = [float(since_ts)]
        if agent_id:
            clauses.append("dal.agent_id=?")
            params.append(str(agent_id))
        sql = f"""
            SELECT
                dal.agent_id,
                COUNT(*)                                       AS total,
                SUM(CASE WHEN dal.action='approved'       THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN dal.action='rejected'       THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN dal.action='blocked'        THEN 1 ELSE 0 END) AS blocked,
                SUM(CASE WHEN dal.action='force_override' THEN 1 ELSE 0 END) AS force_override,
                SUM(CASE WHEN dal.action='autosend'       THEN 1 ELSE 0 END) AS autosend,
                SUM(CASE WHEN dal.action='edit_send'      THEN 1 ELSE 0 END) AS edit_send,
                MAX(dal.ts)                                    AS last_action_ts
            FROM draft_audit_log dal
            WHERE {' AND '.join(clauses)}
              AND dal.agent_id != ''
            GROUP BY dal.agent_id
            ORDER BY total DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        perf = [dict(r) for r in rows]

        # M1: 为每个坐席聚合 avg_csat（其处置的对话的 CSAT 均值）
        try:
            for p in perf:
                aid = str(p.get("agent_id") or "")
                if not aid:
                    continue
                # 找该坐席处置的所有对话 ID
                conv_ids = self._conn.execute(
                    "SELECT DISTINCT conversation_id FROM draft_audit_log "
                    "WHERE agent_id=? AND ts>=? AND conversation_id!=''",
                    (aid, float(since_ts)),
                ).fetchall()
                cids = [r[0] for r in conv_ids]
                if not cids:
                    p["avg_csat"] = None
                    continue
                # 查有 csat_score > 0 的对话
                placeholders = ",".join("?" * len(cids))
                csat_rows = self._conn.execute(
                    f"SELECT csat_score FROM conversation_meta "
                    f"WHERE conversation_id IN ({placeholders}) AND csat_score >= 0",
                    cids,
                ).fetchall()
                scores = [r[0] for r in csat_rows if r[0] is not None and r[0] >= 0]
                p["avg_csat"] = round(sum(scores) / len(scores), 1) if scores else None
        except Exception:
            pass

        return perf

    def get_agent_perf_timeline(
        self,
        *,
        since_ts: float = 0.0,
        agent_id: str = "",
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """按时间桶聚合 draft_audit_log，返回趋势数据（默认按天）。

        返回字段：bucket_ts（UTC 秒，每桶起始）, agent_id, total, approved, rejected
        """
        clauses = ["ts>=?"]
        params: List[Any] = [float(since_ts)]
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        bkt = max(3600, int(bucket_sec))
        sql = f"""
            SELECT
                (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                agent_id,
                COUNT(*) AS total,
                SUM(CASE WHEN action='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM draft_audit_log
            WHERE {' AND '.join(clauses)}
              AND agent_id != ''
            GROUP BY bucket_ts, agent_id
            ORDER BY bucket_ts
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_reliability_timeline(
        self, since_ts: float = 0.0, *, bucket_sec: int = 3600,
    ) -> List[Dict[str, Any]]:
        """D2 运维可靠性：按时间桶聚合 draft_audit_log 全量处置（不过滤 agent）。

        持久来源，重启不丢。返回每桶 total/autosend/blocked/rejected，
        供「处置量 + 拦截/弃用率」趋势曲线。block_rate=blocked/total 是系统拦截高风险的占比。
        """
        bkt = max(300, int(bucket_sec))
        sql = f"""
            SELECT
                (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                COUNT(*) AS total,
                SUM(CASE WHEN action='autosend' THEN 1 ELSE 0 END) AS autosend,
                SUM(CASE WHEN action='blocked'  THEN 1 ELSE 0 END) AS blocked,
                SUM(CASE WHEN action='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM draft_audit_log
            WHERE ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts
        """
        with self._lock:
            rows = self._conn.execute(sql, [float(since_ts)]).fetchall()
        return [dict(r) for r in rows]

    def get_csat_trend(
        self,
        *,
        since_ts: float = 0.0,
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """O1：按时间桶聚合 conversation_meta CSAT 均值趋势。

        返回字段：bucket_ts（UTC 秒，每桶起始）, avg_csat, count
        仅统计 csat_score >= 0（已评分）的会话。
        """
        bkt = max(3600, int(bucket_sec))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    (CAST(updated_at / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                    AVG(csat_score) AS avg_csat,
                    COUNT(*) AS count
                FROM conversation_meta
                WHERE csat_score >= 0 AND updated_at >= ?
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                (float(since_ts),),
            ).fetchall()
        return [
            {
                "bucket_ts": r[0],
                "avg_csat": round(float(r[1]), 2) if r[1] is not None else None,
                "count": int(r[2]),
            }
            for r in rows
        ]

    def get_draft_level_trend(
        self,
        *,
        since_ts: float = 0.0,
        bucket_sec: int = 86400,
    ) -> List[Dict[str, Any]]:
        """O1：按时间桶聚合 draft_audit_log，统计 L3/L4 占比趋势。

        返回字段：bucket_ts, total, l3, l4, high_risk_rate（L3+L4 / total）
        """
        bkt = max(3600, int(bucket_sec))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    (CAST(ts / {bkt} AS INTEGER) * {bkt}) AS bucket_ts,
                    COUNT(*) AS total,
                    SUM(CASE WHEN autopilot_level='L3' THEN 1 ELSE 0 END) AS l3,
                    SUM(CASE WHEN autopilot_level='L4' THEN 1 ELSE 0 END) AS l4
                FROM draft_audit_log
                WHERE ts >= ? AND action IN ('approved','rejected','autosend','force_override','blocked')
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                (float(since_ts),),
            ).fetchall()
        result = []
        for r in rows:
            total = int(r[1] or 0)
            l3 = int(r[2] or 0)
            l4 = int(r[3] or 0)
            result.append({
                "bucket_ts": r[0],
                "total": total,
                "l3": l3,
                "l4": l4,
                "high_risk_rate": round((l3 + l4) / total, 3) if total > 0 else 0.0,
            })
        return result

    def get_automation_roi_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P0-3 ROI：按动作聚合 draft_audit_log，拆「AI 自动发 / 人工发 / 拦截」。

        与 ``get_agent_perf`` 不同：**不过滤 agent_id**，故 AutosendWorker 的无主
        autosend 也计入——这正是「AI 替代多少人工」的真实口径。

        ``until_ts`` 给定时只统计 ``[since_ts, until_ts)`` 半开区间（用于环比上一窗口）。

        返回::

            {
              "ai_sent": int,        # autosend（AI 自动发送）
              "human_sent": int,     # approved + edit_send + force_override（坐席处置后发）
              "suppressed": int,     # rejected + blocked（生成但未发）
              "total_sent": int,     # ai_sent + human_sent
              "ai_share": float,     # ai_sent / total_sent（0–1）
              "trend": [{"day": "MM-DD", "ai": int, "human": int}, ...],
            }
        """
        _AI = ("autosend",)
        _HUMAN = ("approved", "edit_send", "force_override")
        _SUPPRESS = ("rejected", "blocked")
        clause = "ts >= ?"
        params: List[Any] = [float(since_ts)]
        if until_ts is not None:
            clause += " AND ts < ?"
            params.append(float(until_ts))
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, COUNT(*) FROM draft_audit_log "
                f"WHERE {clause} GROUP BY action",
                params,
            ).fetchall()
            day_rows = self._conn.execute(
                f"SELECT action, ts FROM draft_audit_log WHERE {clause}",
                params,
            ).fetchall()
        counts: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0) for r in rows}
        ai_sent = sum(counts.get(a, 0) for a in _AI)
        human_sent = sum(counts.get(a, 0) for a in _HUMAN)
        suppressed = sum(counts.get(a, 0) for a in _SUPPRESS)
        total_sent = ai_sent + human_sent
        per_day: Dict[str, Dict[str, int]] = {}
        for action, ts in day_rows:
            act = str(action or "")
            if act in _AI:
                key = "ai"
            elif act in _HUMAN:
                key = "human"
            else:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            bucket = per_day.setdefault(day, {"ai": 0, "human": 0})
            bucket[key] += 1
        trend = [{"day": d[5:], "ai": v["ai"], "human": v["human"]}
                 for d, v in sorted(per_day.items())]
        return {
            "ai_sent": ai_sent,
            "human_sent": human_sent,
            "suppressed": suppressed,
            "total_sent": total_sent,
            "ai_share": round(ai_sent / total_sent, 3) if total_sent else 0.0,
            "trend": trend,
        }

    def get_engagement_stats(
        self,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """情感陪聊·关系参与度：按 **会话(=关系)** 聚合 ``messages``，衡量「聊得多深、多黏」。

        与客服「解决率」相反——陪聊的目标是 **更长、更黏、用户愿意回来**，故这里看
        参与轮次、互惠比、跨天活跃（黏性），而非「快速结案」。窗口 ``[since_ts, until_ts)``。

        口径：
        - ``active_relationships``：窗口内 **有 ≥1 条用户入站(in)** 的会话数（真正在聊的关系）。
        - ``messages_in/out``：用户/角色消息条数；``total_turns`` 两者之和。
        - ``avg_turns``：人均(每关系)往来轮次——关系深度。
        - ``reciprocity``：out/in 比值，衡量 AI 角色的应答充分度（≈1 健康，过低=冷落）。
        - ``sticky_relationships``：窗口内在 **≥2 个不同自然日** 有入站的会话（会回来的关系）。
        - ``sticky_rate`` = sticky / active：黏性（陪聊的核心健康指标）。
        - ``trend``：逐日 {day, active, in, out}。
        """
        import datetime as _dt
        clause = "ts >= ? AND conversation_id != ''"
        params: List[Any] = [float(since_ts)]
        if until_ts is not None:
            clause += " AND ts < ?"
            params.append(float(until_ts))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, direction, ts FROM messages WHERE {clause}",
                params,
            ).fetchall()
        convs: Dict[str, Dict[str, Any]] = {}
        per_day: Dict[str, Dict[str, Any]] = {}
        messages_in = messages_out = 0
        for cid, direction, ts in rows:
            cid = str(cid or "")
            if not cid:
                continue
            is_in = str(direction or "in").lower() == "in"
            tsf = float(ts or 0.0)
            day = _dt.date.fromtimestamp(tsf).isoformat()
            c = convs.setdefault(cid, {"in": 0, "out": 0, "days": set()})
            if is_in:
                c["in"] += 1
                messages_in += 1
                c["days"].add(day)
            else:
                c["out"] += 1
                messages_out += 1
            b = per_day.setdefault(day, {"active": set(), "in": 0, "out": 0})
            if is_in:
                b["in"] += 1
                b["active"].add(cid)
            else:
                b["out"] += 1
        active = [c for c in convs.values() if c["in"] > 0]
        active_n = len(active)
        sticky_n = sum(1 for c in active if len(c["days"]) >= 2)
        total_turns = messages_in + messages_out
        trend = [
            {"day": d[5:], "active": len(v["active"]), "in": v["in"], "out": v["out"]}
            for d, v in sorted(per_day.items())
        ]
        return {
            "active_relationships": active_n,
            "messages_in": messages_in,
            "messages_out": messages_out,
            "total_turns": total_turns,
            "avg_turns": round(total_turns / active_n, 1) if active_n else 0.0,
            "reciprocity": round(messages_out / messages_in, 2) if messages_in else 0.0,
            "sticky_relationships": sticky_n,
            "sticky_rate": round(sticky_n / active_n, 4) if active_n else 0.0,
            "trend": trend,
        }

    def get_retention_cohorts(
        self,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
        *,
        horizons: tuple = (1, 7, 30),
    ) -> Dict[str, Any]:
        """情感陪聊·留存：以「首次入站落在窗口内」的会话为同期群，算 D1/D7/D30 回访率。

        留存 = 用户在首次接触后的第 N 天内 **又回来发消息**——这才是陪聊的"解决率"。
        以入站(in)消息为准（用户主动说话才算"在关系里"）。日偏移按自然日计算。
        """
        import datetime as _dt
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, ts FROM messages "
                "WHERE direction='in' AND conversation_id != '' ORDER BY ts ASC",
            ).fetchall()
        # 每会话的入站自然日序列 + 全局首次入站 ts
        first_ts: Dict[str, float] = {}
        days_by_conv: Dict[str, set] = {}
        for cid, ts in rows:
            cid = str(cid or "")
            if not cid:
                continue
            tsf = float(ts or 0.0)
            if cid not in first_ts:
                first_ts[cid] = tsf
            days_by_conv.setdefault(cid, set()).add(_dt.date.fromtimestamp(tsf).toordinal())
        hi = sorted(int(h) for h in horizons if int(h) > 0)
        cohort = [
            cid for cid, t0 in first_ts.items()
            if t0 >= float(since_ts) and (until_ts is None or t0 < float(until_ts))
        ]
        retained = {h: 0 for h in hi}
        for cid in cohort:
            first_ord = _dt.date.fromtimestamp(first_ts[cid]).toordinal()
            offsets = {d - first_ord for d in days_by_conv[cid]}
            for h in hi:
                if any(1 <= off <= h for off in offsets):
                    retained[h] += 1
        size = len(cohort)
        return {
            "cohort_size": size,
            "horizons": hi,
            "retained": {f"d{h}": retained[h] for h in hi},
            "retention_rate": {
                f"d{h}": round(retained[h] / size, 4) if size else 0.0 for h in hi
            },
        }

    def get_quality_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P3-1 AI 回复质量：按动作 + 风险等级聚合 draft_audit_log（**不过滤 agent**）。

        ``until_ts`` 给定时只统计 ``[since_ts, until_ts)`` 半开区间（用于环比上一窗口）。

        与 ``get_agent_perf`` 不同：含 AutosendWorker 的无主 autosend，故口径是
        「全部 AI 草稿处置」，用于「AI 答得好不好」质量闭环。

        派生率：
        - ``auto_pass_rate``：autosend / 总处置（AI 直接放行占比，越高越省人）
        - ``edit_rate``：edit_send / 人工发送（坐席需改写才发的占比，越高说明 AI 初稿越差）
        - ``reject_rate``：rejected / 总处置（被坐席弃用占比）
        - ``block_rate``：blocked / 总处置（L4 高风险拦截占比）
        - ``high_risk_rate``：(L3+L4) / 有等级的处置

        返回 counts / levels / 各率 + 按日 trend（total/autosend/edit_send/rejected/high_risk）。
        """
        _DISPOSITIONS = ("autosend", "approved", "edit_send",
                         "rejected", "blocked", "force_override")
        _HUMAN_SENT = ("approved", "edit_send", "force_override")
        _IN = ("autosend", "approved", "edit_send",
               "rejected", "blocked", "force_override")
        _ph = ",".join("?" * len(_IN))
        win = "ts >= ?"
        base: list = [float(since_ts)]
        if until_ts is not None:
            win += " AND ts < ?"
            base.append(float(until_ts))
        with self._lock:
            act_rows = self._conn.execute(
                f"SELECT action, COUNT(*) FROM draft_audit_log "
                f"WHERE {win} AND action IN ({_ph}) GROUP BY action",
                base + list(_IN),
            ).fetchall()
            lvl_rows = self._conn.execute(
                f"SELECT autopilot_level, COUNT(*) FROM draft_audit_log "
                f"WHERE {win} AND autopilot_level != '' GROUP BY autopilot_level",
                base,
            ).fetchall()
            day_rows = self._conn.execute(
                f"SELECT action, autopilot_level, ts FROM draft_audit_log "
                f"WHERE {win} AND action IN ({_ph})",
                base + list(_IN),
            ).fetchall()
        counts: Dict[str, int] = {a: 0 for a in _DISPOSITIONS}
        for action, cnt in act_rows:
            counts[str(action)] = int(cnt or 0)
        levels: Dict[str, int] = {str(lv): int(c or 0) for lv, c in lvl_rows}
        total = sum(counts.values())
        human_sent = sum(counts[a] for a in _HUMAN_SENT)
        leveled = sum(levels.values())
        high_risk = levels.get("L3", 0) + levels.get("L4", 0)

        def _rate(num: int, den: int) -> float:
            return round(num / den, 3) if den else 0.0

        per_day: Dict[str, Dict[str, int]] = {}
        for action, level, ts in day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            b = per_day.setdefault(
                day, {"total": 0, "autosend": 0, "edit_send": 0,
                      "rejected": 0, "high_risk": 0})
            b["total"] += 1
            act = str(action or "")
            if act in b:
                b[act] += 1
            if str(level or "") in ("L3", "L4"):
                b["high_risk"] += 1
        trend = [
            {"day": d[5:], **v} for d, v in sorted(per_day.items())
        ]
        return {
            "counts": counts,
            "levels": levels,
            "total": total,
            "sent": counts["autosend"] + human_sent,
            "human_sent": human_sent,
            "auto_pass_rate": _rate(counts["autosend"], total),
            "edit_rate": _rate(counts["edit_send"], human_sent),
            "reject_rate": _rate(counts["rejected"], total),
            "block_rate": _rate(counts["blocked"], total),
            "high_risk_rate": _rate(high_risk, leveled),
            "trend": trend,
        }

    def get_usage_stats(
        self, since_ts: float = 0.0, until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """C0-2 用量计量：从既有 messages + draft_audit_log 聚合可计费用量。

        单一数据源、零新表零迁移，口径与 ROI/质量看板一致。``until_ts`` 给定时只统计
        ``[since_ts, until_ts)`` 半开区间（环比）。

        返回::

            {
              "messages_in": int, "messages_out": int, "messages_total": int,
              "ai_calls": int,        # draft_audit_log 处置行数 ≈ AI 生成草稿数
              "ai_sent": int,         # autosend（AI 自动发）
              "active_agents": int,   # 窗口内有处置记录的去重坐席数（计费口径代理）
              "active_agent_ids": [...],
              "trend": [{"day":"MM-DD","messages":int,"ai_calls":int}, ...],
            }
        """
        mwin = "ts >= ?"
        mparams: List[Any] = [float(since_ts)]
        if until_ts is not None:
            mwin += " AND ts < ?"
            mparams.append(float(until_ts))
        with self._lock:
            msg_rows = self._conn.execute(
                f"SELECT direction, COUNT(*) FROM messages WHERE {mwin} "
                "GROUP BY direction",
                mparams,
            ).fetchall()
            msg_day_rows = self._conn.execute(
                f"SELECT ts FROM messages WHERE {mwin}", mparams,
            ).fetchall()
            audit_act_rows = self._conn.execute(
                f"SELECT action, COUNT(*) FROM draft_audit_log WHERE {mwin} "
                "GROUP BY action",
                mparams,
            ).fetchall()
            agent_rows = self._conn.execute(
                f"SELECT DISTINCT agent_id FROM draft_audit_log WHERE {mwin} "
                "AND agent_id != ''",
                mparams,
            ).fetchall()
            audit_day_rows = self._conn.execute(
                f"SELECT ts FROM draft_audit_log WHERE {mwin}", mparams,
            ).fetchall()
        msg_by_dir: Dict[str, int] = {str(r[0] or "in"): int(r[1] or 0)
                                      for r in msg_rows}
        messages_in = msg_by_dir.get("in", 0)
        messages_out = msg_by_dir.get("out", 0)
        act_counts: Dict[str, int] = {str(r[0] or ""): int(r[1] or 0)
                                      for r in audit_act_rows}
        ai_calls = sum(act_counts.values())
        ai_sent = act_counts.get("autosend", 0)
        agent_ids = sorted(str(r[0]) for r in agent_rows if r[0])

        per_day: Dict[str, Dict[str, int]] = {}
        for (ts,) in msg_day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            per_day.setdefault(day, {"messages": 0, "ai_calls": 0})["messages"] += 1
        for (ts,) in audit_day_rows:
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
            per_day.setdefault(day, {"messages": 0, "ai_calls": 0})["ai_calls"] += 1
        trend = [{"day": d[5:], "messages": v["messages"], "ai_calls": v["ai_calls"]}
                 for d, v in sorted(per_day.items())]
        return {
            "messages_in": messages_in,
            "messages_out": messages_out,
            "messages_total": messages_in + messages_out,
            "ai_calls": ai_calls,
            "ai_sent": ai_sent,
            "active_agents": len(agent_ids),
            "active_agent_ids": agent_ids,
            "trend": trend,
        }

    def ping(self) -> bool:
        """轻量连通性自检：能否对 DB 执行一次 SELECT（健康检查用）。"""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            logger.debug("InboxStore.ping 失败", exc_info=True)
            return False

    def count_demo(self, prefix: str = "demo:") -> Dict[str, int]:
        """统计 demo 命名空间（conversation_id 以 prefix 开头）的行数。"""
        like = f"{prefix}%"
        with self._lock:
            conv = self._conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
            msg = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
            audit = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE conversation_id LIKE ?",
                (like,)).fetchone()[0]
        return {"conversations": int(conv), "messages": int(msg),
                "draft_audits": int(audit)}

    def purge_demo(self, prefix: str = "demo:") -> Dict[str, int]:
        """删除 demo 命名空间的全部行（会话/消息/草稿审计），返回删除计数。

        仅按 conversation_id 前缀删除，绝不触碰真实数据。messages FTS 触发器随删同步。
        """
        like = f"{prefix}%"
        before = self.count_demo(prefix)
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id LIKE ?", (like,))
            self._conn.execute(
                "DELETE FROM draft_audit_log WHERE conversation_id LIKE ?", (like,))
            self._conn.execute(
                "DELETE FROM conversations WHERE conversation_id LIKE ?", (like,))
            self._conn.commit()
        return before

    def get_kb_improvement_candidates(
        self, since_ts: float = 0.0, *, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """P3-2 质量→KB 闭环：把「AI 答错被改写/拒绝」的会话挑成 KB 改进候选。

        取 ``draft_audit_log`` 中 action ∈ {edit_send, rejected} 的近期记录，关联：
        - ``question``：处置时刻前最后一条**入站**消息（客户问句 → 候选 trigger）；
        - ``suggested_reply``：edit_send 时取处置时刻后第一条**出站**消息（坐席改写后真正
          发出的好答案 → 候选 example_reply）；rejected 无好答案，留空待人工补。

        无客户问句（纯媒体/取不到）的候选跳过。返回最多 limit 条，最新在前。
        """
        limit = max(1, min(100, int(limit or 20)))
        with self._lock:
            audit_rows = self._conn.execute(
                "SELECT conversation_id, action, ts, reason FROM draft_audit_log "
                "WHERE ts >= ? AND action IN ('edit_send','rejected') "
                "AND conversation_id != '' ORDER BY ts DESC LIMIT ?",
                (float(since_ts), limit * 3),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            seen_q: set = set()
            for ar in audit_rows:
                cid = str(ar["conversation_id"] or "")
                ats = float(ar["ts"] or 0)
                action = str(ar["action"] or "")
                q = self._conn.execute(
                    "SELECT text FROM messages WHERE conversation_id=? "
                    "AND direction='in' AND ts<=? AND text!='' "
                    "ORDER BY ts DESC LIMIT 1",
                    (cid, ats + 1),
                ).fetchone()
                question = str(q["text"]).strip() if q and q["text"] else ""
                if not question:
                    continue
                dedup_key = (cid, question[:80])
                if dedup_key in seen_q:
                    continue
                seen_q.add(dedup_key)
                suggested = ""
                if action == "edit_send":
                    rep = self._conn.execute(
                        "SELECT text FROM messages WHERE conversation_id=? "
                        "AND direction='out' AND ts>=? AND text!='' "
                        "ORDER BY ts ASC LIMIT 1",
                        (cid, ats - 1),
                    ).fetchone()
                    suggested = str(rep["text"]).strip() if rep and rep["text"] else ""
                out.append({
                    "conversation_id": cid,
                    "action": action,
                    "ts": ats,
                    "reason": str(ar["reason"] or ""),
                    "question": question,
                    "suggested_reply": suggested,
                })
                if len(out) >= limit:
                    break
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
            # P3：LEFT JOIN agent_prefs 带出坐席技能语言（供 auto_assign match_language）。
            rows = self._conn.execute(
                "SELECT p.*, COALESCE(pr.languages, '') AS languages"
                " FROM agent_presence p"
                " LEFT JOIN agent_prefs pr ON pr.agent_id = p.agent_id"
                " WHERE p.last_seen_at >= ? ORDER BY p.last_seen_at DESC",
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
                    "muted": 0, "dnd_start": -1, "dnd_end": -1, "updated_at": 0,
                    "languages": ""}
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

    def set_agent_languages(self, agent_id: str, languages: str) -> Dict[str, Any]:
        """P3：写坐席技能语言（CSV 规范码），只动 languages 列，不影响告警偏好。

        languages 已由调用方规范化（normalize_lang + 去重）。新坐席无 prefs 行时插入，
        告警字段取默认（沿用全局 / 无免打扰）。
        """
        aid = str(agent_id or "").strip()
        if not aid:
            raise ValueError("agent_id required")
        langs = str(languages or "")
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_prefs (agent_id, languages, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET languages=excluded.languages, "
                "updated_at=excluded.updated_at",
                (aid, langs, now))
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

    # ── E2：运维事件（health_alert 闭环）────────────────────────────────────

    def open_or_update_incident(
        self,
        *,
        signature: str,
        light: str,
        kind: str = "health",
        summary: Optional[Dict[str, Any]] = None,
        problems: Optional[List[Dict[str, Any]]] = None,
        ts: Optional[float] = None,
    ) -> int:
        """按 (kind, signature) 去重地开/更新一条未关闭的运维事件。

        同类型同 signature 已有未 resolved 事件 → 更新 light/summary/problems/updated_ts；
        否则新建 open 事件。返回事件 id。kind ∈ health|billing。
        """
        now = float(ts if ts is not None else time.time())
        sig = str(signature or "")
        knd = str(kind or "health")
        summary_json = json.dumps(summary or {}, ensure_ascii=False)
        problems_json = json.dumps(problems or [], ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM ops_incidents "
                "WHERE kind=? AND signature=? AND status!='resolved' "
                "ORDER BY id DESC LIMIT 1",
                (knd, sig),
            ).fetchone()
            if row:
                iid = int(row["id"])
                self._conn.execute(
                    "UPDATE ops_incidents SET light=?, summary_json=?, "
                    "problems_json=?, updated_ts=? WHERE id=?",
                    (str(light or ""), summary_json, problems_json, now, iid),
                )
                self._conn.commit()
                return iid
            cur = self._conn.execute(
                "INSERT INTO ops_incidents "
                "(kind, signature, light, summary_json, problems_json, status, "
                " opened_ts, updated_ts) "
                "VALUES (?,?,?,?,?,'open',?,?)",
                (knd, sig, str(light or ""), summary_json, problems_json, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def resolve_open_incidents(
        self, *, kind: str = "", ts: Optional[float] = None,
    ) -> int:
        """把未 resolved 的事件标记为 resolved；kind 非空时仅限该类型。返回受影响条数。"""
        now = float(ts if ts is not None else time.time())
        sql = "UPDATE ops_incidents SET status='resolved', resolved_ts=?, updated_ts=? WHERE status!='resolved'"
        params: List[Any] = [now, now]
        if kind:
            sql += " AND kind=?"
            params.append(str(kind))
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
        return cur.rowcount

    def ack_incident(
        self, incident_id: int, *, assigned_to: str = "", ts: Optional[float] = None,
    ) -> bool:
        """主管确认/认领一条事件（status→acked，记 acked_ts 与 assigned_to）。"""
        now = float(ts if ts is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE ops_incidents SET status='acked', acked_ts=?, "
                "assigned_to=?, updated_ts=? WHERE id=? AND status!='resolved'",
                (now, str(assigned_to or ""), now, int(incident_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_incidents(
        self, *, status: str = "", kind: str = "", limit: int = 50,
        before_id: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出运维事件（默认全部，可按 status / kind 过滤），新→旧。

        before_id>0 时只返回 id 小于它的（游标分页，回看历史）。
        """
        sql = "SELECT * FROM ops_incidents"
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(str(status))
        if kind:
            clauses.append("kind=?")
            params.append(str(kind))
        if before_id and int(before_id) > 0:
            clauses.append("id<?")
            params.append(int(before_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        out: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        for r in rows:
            d = dict(r)
            try:
                d["summary"] = json.loads(d.pop("summary_json", "{}") or "{}")
            except Exception:
                d["summary"] = {}
            try:
                d["problems"] = json.loads(d.pop("problems_json", "[]") or "[]")
            except Exception:
                d["problems"] = []
            out.append(d)
        return out

    def count_open_incidents(self) -> int:
        """未关闭（open + acked）的事件数。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM ops_incidents WHERE status!='resolved'"
            ).fetchone()[0]

    def get_incident_stats(self, since_ts: float = 0.0,
                           until_ts: Optional[float] = None) -> Dict[str, Any]:
        """统计 [since_ts, until_ts) 开启的运维事件：总数/各状态/各类型/平均解决时长（秒）。

        until_ts=None 表示「到现在」（无上界），用于环比上一窗口时显式给 until。
        """
        with self._lock:
            if until_ts is None:
                rows = self._conn.execute(
                    "SELECT kind, status, opened_ts, resolved_ts FROM ops_incidents "
                    "WHERE opened_ts>=?",
                    (float(since_ts),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT kind, status, opened_ts, resolved_ts FROM ops_incidents "
                    "WHERE opened_ts>=? AND opened_ts<?",
                    (float(since_ts), float(until_ts)),
                ).fetchall()
        total = len(rows)
        by_status: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        durations: List[float] = []
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            if r["status"] == "resolved" and r["resolved_ts"] and r["opened_ts"]:
                d = float(r["resolved_ts"]) - float(r["opened_ts"])
                if d >= 0:
                    durations.append(d)
        return {
            "total": total,
            "open": by_status.get("open", 0) + by_status.get("acked", 0),
            "resolved": by_status.get("resolved", 0),
            "by_status": by_status,
            "by_kind": by_kind,
            "mttr_sec": round(sum(durations) / len(durations), 1) if durations else None,
        }

    def purge_resolved_incidents(self, older_than_ts: float) -> int:
        """删除 resolved_ts 早于阈值的已关闭事件（保留期清理）。返回删除条数。

        只删 status='resolved' 且 resolved_ts>0 的；未关闭事件永不删。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ops_incidents "
                "WHERE status='resolved' AND resolved_ts>0 AND resolved_ts<?",
                (float(older_than_ts),),
            )
            self._conn.commit()
        return cur.rowcount

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

    def list_claims_by_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        """K2：列出指定坐席当前持有的所有 conversation claims（已过期的不计）。"""
        self.purge_expired_claims()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_claims WHERE agent_id = ? ORDER BY claimed_at DESC",
                (str(agent_id or ""),),
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

    # ── I1 对话智能分析元数据 ─────────────────────────────────────

    _EMOTION_ORDER = ["愤怒", "不满", "催促", "焦虑", "平稳", "满意", "感谢"]

    def update_conv_meta(
        self,
        conversation_id: str,
        *,
        platform: str = "",
        intent: str = "",
        emotion: str = "",
        risk: str = "low",
        contact_id: str = "",
        workspace_id: str = "default",
        max_history: int = 10,
    ) -> None:
        """I1：每次新入站消息后，更新对话智能元数据。

        维护滚动窗口情绪/意图历史（max_history 条），用于趋势计算。
        N1: contact_id 用于跨平台会话归档，传入后持久化。
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        existing = self.get_conv_meta(cid)
        trace_id_to_use = ""
        if existing:
            ih = list(existing.get("intent_history") or [])
            eh = list(existing.get("emotion_history") or [])
            mc = int(existing.get("msg_count") or 0) + 1
            # 保留已有 contact_id（如未传新值）
            if not contact_id:
                contact_id = str(existing.get("contact_id") or "")
            # S3: 继承已有 trace_id；没有则新生成
            trace_id_to_use = str(existing.get("trace_id") or "")
        else:
            ih, eh, mc = [], [], 1
        if not trace_id_to_use:
            from src.inbox.tracer import new_trace_id
            trace_id_to_use = new_trace_id()
        if intent:
            ih.append(str(intent))
        if emotion:
            eh.append(str(emotion))
        # 保持滚动窗口
        ih = ih[-max_history:]
        eh = eh[-max_history:]
        _wsid = str(workspace_id or "default")
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversation_meta
                   (conversation_id, platform, last_intent, last_emotion, last_risk,
                    intent_history, emotion_history, msg_count, updated_at, contact_id, workspace_id, trace_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     platform       = excluded.platform,
                     last_intent    = CASE WHEN excluded.last_intent != ''
                                      THEN excluded.last_intent ELSE conversation_meta.last_intent END,
                     last_emotion   = CASE WHEN excluded.last_emotion != ''
                                      THEN excluded.last_emotion ELSE conversation_meta.last_emotion END,
                     last_risk      = excluded.last_risk,
                     intent_history = excluded.intent_history,
                     emotion_history= excluded.emotion_history,
                     msg_count      = excluded.msg_count,
                     updated_at     = excluded.updated_at,
                     contact_id     = CASE WHEN excluded.contact_id != ''
                                      THEN excluded.contact_id ELSE conversation_meta.contact_id END,
                     workspace_id   = CASE WHEN excluded.workspace_id != 'default'
                                      THEN excluded.workspace_id ELSE conversation_meta.workspace_id END,
                     trace_id       = CASE WHEN conversation_meta.trace_id = '' OR conversation_meta.trace_id IS NULL
                                      THEN excluded.trace_id ELSE conversation_meta.trace_id END
                """,
                (cid, str(platform or ""), str(intent or ""), str(emotion or ""),
                 str(risk or "low"),
                 json.dumps(ih, ensure_ascii=False),
                 json.dumps(eh, ensure_ascii=False),
                 mc, now, str(contact_id or ""), _wsid, trace_id_to_use),
            )
            self._conn.commit()

    # ── R3: CSAT 问卷 ────────────────────────────────────────────────────
    def schedule_csat_survey(
        self,
        *,
        survey_id: str,
        conversation_id: str,
        draft_id: str,
        agent_id: str,
        delay_seconds: float = 300.0,
    ) -> None:
        """R3：将待发 CSAT 问卷登记到 csat_surveys 表（由 SurveyWorker 轮询发送）。"""
        now = self._now()
        send_at = now + float(delay_seconds)
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO csat_surveys
                   (id, conversation_id, draft_id, agent_id, scheduled_at, send_at,
                    sent, response_score, response_ts, created_at)
                   VALUES (?,?,?,?,?,?,0,-1,0,?)""",
                (survey_id, conversation_id, draft_id, agent_id, now, send_at, now),
            )
            self._conn.commit()

    def list_due_surveys(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        """R3：返回到期未发的 CSAT 问卷（send_at <= now, sent=0）。"""
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, conversation_id, draft_id, agent_id, send_at
                   FROM csat_surveys WHERE send_at <= ? AND sent = 0
                   ORDER BY send_at LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [
            dict(zip(["id", "conversation_id", "draft_id", "agent_id", "send_at"], r))
            for r in rows
        ]

    def mark_survey_sent(self, survey_id: str) -> None:
        """R3：标记问卷已发送。"""
        with self._lock:
            self._conn.execute(
                "UPDATE csat_surveys SET sent=1 WHERE id=?", (survey_id,)
            )
            self._conn.commit()

    def record_survey_response(
        self,
        conversation_id: str,
        score: int,
    ) -> bool:
        """R3：记录客户 CSAT 问卷回复（score 1-5），同时更新 conversation_meta.csat_score。

        返回 True 表示匹配到待回复问卷。
        """
        score = max(1, min(5, int(score)))
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                """SELECT id FROM csat_surveys
                   WHERE conversation_id=? AND sent=1 AND response_score=-1
                   ORDER BY send_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE csat_surveys SET response_score=?, response_ts=? WHERE id=?",
                (score, now, row[0]),
            )
            # 同步更新 conv_meta.csat_score
            self._conn.execute(
                "UPDATE conversation_meta SET csat_score=?, updated_at=? WHERE conversation_id=?",
                (float(score), now, conversation_id),
            )
            self._conn.commit()
        return True

    def set_conv_survey_awaiting(self, conversation_id: str, flag: bool) -> None:
        """R3：在 conv_meta 中标记/清除 survey_awaiting 状态（用于识别客户回复是否为问卷响应）。"""
        # survey_awaiting 存储在 summary 字段前缀 __survey__ 标记（简洁，不加新列）
        now = self._now()
        with self._lock:
            if flag:
                self._conn.execute(
                    """UPDATE conversation_meta
                       SET summary=CASE WHEN summary NOT LIKE '__survey__%'
                           THEN '__survey__' || summary ELSE summary END,
                       updated_at=?
                       WHERE conversation_id=?""",
                    (now, conversation_id),
                )
            else:
                self._conn.execute(
                    """UPDATE conversation_meta
                       SET summary=REPLACE(summary,'__survey__',''),
                       updated_at=?
                       WHERE conversation_id=?""",
                    (now, conversation_id),
                )
            self._conn.commit()

    def is_survey_awaiting(self, conversation_id: str) -> bool:
        """R3：检查会话是否正在等待 CSAT 问卷回复。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM conversation_meta WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return False
        return str(row[0] or "").startswith("__survey__")

    # ── Q1: 对话摘要 ─────────────────────────────────────────────────────
    def update_conv_summary(self, conversation_id: str, summary: str) -> None:
        """Q1：写入/更新对话摘要到 conversation_meta.summary。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_meta SET summary=?, updated_at=? WHERE conversation_id=?",
                (str(summary or ""), now, cid),
            )
            self._conn.commit()

    # ── Q2: 草稿质量评分 ─────────────────────────────────────────────────
    def update_draft_quality(
        self,
        draft_id: str,
        quality_score: float,
        quality_breakdown: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Q2：写入草稿质量分及维度明细到 reply_drafts。"""
        did = str(draft_id or "").strip()
        if not did:
            return
        breakdown_json = json.dumps(quality_breakdown or {}, ensure_ascii=False)
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE reply_drafts SET quality_score=?, quality_breakdown=?, updated_at=? WHERE draft_id=?",
                (float(quality_score), breakdown_json, now, did),
            )
            self._conn.commit()

    def get_draft_quality(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """Q2：读取草稿质量评分（含明细）。"""
        did = str(draft_id or "").strip()
        if not did:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT quality_score, quality_breakdown FROM reply_drafts WHERE draft_id=?", (did,)
            ).fetchone()
        if row is None:
            return None
        score = float(row[0]) if row[0] is not None else -1.0
        try:
            breakdown = json.loads(row[1] or "{}")
        except Exception:
            breakdown = {}
        return {"quality_score": score, "breakdown": breakdown}

    def list_draft_quality_stats(
        self,
        *,
        since_ts: float = 0.0,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Q2：汇总质量分分布（用于 dashboard 统计）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT quality_score FROM reply_drafts WHERE quality_score >= 0 AND created_at >= ? LIMIT ?",
                (float(since_ts), limit),
            ).fetchall()
        scores = [float(r[0]) for r in rows]
        if not scores:
            return {"count": 0, "avg": None, "excellent": 0, "good": 0, "fair": 0, "poor": 0}
        avg = sum(scores) / len(scores)
        return {
            "count": len(scores),
            "avg": round(avg, 1),
            "excellent": sum(1 for s in scores if s >= 80),
            "good": sum(1 for s in scores if 60 <= s < 80),
            "fair": sum(1 for s in scores if 40 <= s < 60),
            "poor": sum(1 for s in scores if s < 40),
        }

    # ── Q3: KB 命中率监控 ─────────────────────────────────────────────────
    def record_kb_recommendation(
        self,
        *,
        rec_id: str,
        entry_id: str,
        entry_title: str = "",
        conversation_id: str = "",
        agent_id: str = "",
    ) -> None:
        """Q3：记录 KB 条目被推荐给坐席一次。"""
        import uuid as _uuid
        rid = str(rec_id or _uuid.uuid4().hex[:12])
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO kb_recommendation_log
                   (id, entry_id, entry_title, conversation_id, agent_id, recommended_ts)
                   VALUES (?,?,?,?,?,?)""",
                (rid, str(entry_id), str(entry_title), str(conversation_id), str(agent_id), now),
            )
            self._conn.commit()

    def click_kb_recommendation(
        self,
        *,
        rec_id: str,
        used_in_draft: bool = False,
        draft_id: str = "",
    ) -> None:
        """Q3：标记坐席点击了某次 KB 推荐（可选：是否用于草稿）。"""
        now = self._now()
        with self._lock:
            self._conn.execute(
                """UPDATE kb_recommendation_log
                   SET clicked=1, used_in_draft=?, draft_id=?, recommended_ts=recommended_ts
                   WHERE id=?""",
                (1 if used_in_draft else 0, str(draft_id), str(rec_id)),
            )
            self._conn.commit()

    def get_kb_hit_stats(
        self,
        *,
        since_ts: float = 0.0,
        top_n: int = 20,
    ) -> List[Dict[str, Any]]:
        """Q3：返回 KB 条目推荐/点击/使用统计（按命中率排序）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT entry_id, entry_title,
                          COUNT(*) as recommended,
                          SUM(clicked) as clicked,
                          SUM(used_in_draft) as used
                   FROM kb_recommendation_log
                   WHERE recommended_ts >= ?
                   GROUP BY entry_id, entry_title
                   ORDER BY clicked DESC
                   LIMIT ?""",
                (float(since_ts), top_n),
            ).fetchall()
        result = []
        for row in rows:
            entry_id, entry_title, recommended, clicked, used = row
            recommended = int(recommended or 0)
            clicked = int(clicked or 0)
            used = int(used or 0)
            hit_rate = round(clicked / recommended * 100, 1) if recommended > 0 else 0.0
            use_rate = round(used / recommended * 100, 1) if recommended > 0 else 0.0
            result.append({
                "entry_id": entry_id,
                "entry_title": entry_title,
                "recommended": recommended,
                "clicked": clicked,
                "used": used,
                "hit_rate": hit_rate,
                "use_rate": use_rate,
            })
        return result

    # ── R2: 坐席工作负荷均衡 ─────────────────────────────────────────────
    def get_agent_workload(
        self,
        agent_id: str,
        *,
        active_within_sec: float = 3600,
    ) -> Dict[str, Any]:
        """R2：返回指定坐席当前工作负荷（活跃会话数 / 审计操作数 / 最近处置率）。

        "活跃会话"定义：conversation_claims 中该坐席持有 + 过去 N 秒内有 draft_audit_log。
        """
        aid = str(agent_id or "").strip()
        if not aid:
            return {"agent_id": aid, "active_convs": 0, "recent_actions": 0, "status": "unknown"}
        now = self._now()
        since = now - active_within_sec
        with self._lock:
            # 当前会话租约数
            claimed = self._conn.execute(
                "SELECT COUNT(*) FROM conversation_claims WHERE agent_id=? AND expires_at>?",
                (aid, now),
            ).fetchone()[0]
            # 过去 N 秒内的审计操作数
            recent_actions = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE agent_id=? AND ts>=?",
                (aid, since),
            ).fetchone()[0]
            # 在线状态
            pres_row = self._conn.execute(
                "SELECT status FROM agent_presence WHERE agent_id=?", (aid,)
            ).fetchone()
            status = pres_row[0] if pres_row else "offline"

        return {
            "agent_id": aid,
            "active_convs": int(claimed or 0),
            "recent_actions": int(recent_actions or 0),
            "status": str(status),
        }

    def list_agent_workloads(
        self,
        *,
        active_within_sec: float = 120,
        max_load_cap: int = 0,
    ) -> List[Dict[str, Any]]:
        """R2：列出所有在线坐席的工作负荷（用于负荷均衡决策）。

        active_within_sec：判断在线的心跳窗口（秒），默认 120s。
        max_load_cap：若 > 0，标记超负荷坐席。
        """
        now = self._now()
        cutoff = now - active_within_sec
        with self._lock:
            agents = self._conn.execute(
                "SELECT agent_id FROM agent_presence WHERE last_seen_at >= ?",
                (cutoff,),
            ).fetchall()
        result = []
        for (aid,) in agents:
            wl = self.get_agent_workload(aid, active_within_sec=active_within_sec)
            if max_load_cap > 0:
                wl["overloaded"] = wl["active_convs"] >= max_load_cap
            result.append(wl)
        # 按负荷升序（用于负荷均衡时选最空的坐席）
        result.sort(key=lambda x: x["active_convs"])
        return result

    def get_lightest_agent(
        self,
        *,
        active_within_sec: float = 120,
        max_load_cap: int = 0,
        exclude_agent: str = "",
    ) -> Optional[str]:
        """R2：返回负荷最轻的在线坐席 ID（用于自动再分配）。

        max_load_cap > 0 时，过滤掉已超负荷的坐席。
        """
        workloads = self.list_agent_workloads(active_within_sec=active_within_sec)
        for wl in workloads:
            if wl["agent_id"] == exclude_agent:
                continue
            if max_load_cap > 0 and wl["active_convs"] >= max_load_cap:
                continue
            if wl.get("status", "offline") in ("offline",):
                continue
            return wl["agent_id"]
        return None

    def upsert_workspace(
        self,
        workspace_id: str,
        display_name: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """P3：创建或更新工作区配置。"""
        wid = str(workspace_id or "").strip()
        if not wid:
            return
        now = self._now()
        config_json = json.dumps(config or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """INSERT INTO workspaces (workspace_id, display_name, config_json, created_at, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(workspace_id) DO UPDATE SET
                     display_name = excluded.display_name,
                     config_json  = excluded.config_json,
                     updated_at   = excluded.updated_at
                """,
                (wid, str(display_name or ""), config_json, now, now),
            )
            self._conn.commit()

    def list_workspaces(self) -> List[Dict[str, Any]]:
        """P3：列出所有工作区。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT workspace_id, display_name, config_json, created_at, updated_at "
                "FROM workspaces ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            d = dict(zip(["workspace_id", "display_name", "config_json", "created_at", "updated_at"], row))
            try:
                d["config"] = json.loads(d.pop("config_json") or "{}")
            except Exception:
                d["config"] = {}
            result.append(d)
        return result

    def get_workspace_stats(self, workspace_id: str) -> Dict[str, Any]:
        """P3：返回指定工作区的基本统计（会话数/审计数/CSAT 均值）。"""
        wid = str(workspace_id or "default")
        with self._lock:
            conv_count = self._conn.execute(
                "SELECT COUNT(*) FROM conversation_meta WHERE workspace_id=?", (wid,)
            ).fetchone()[0]
            audit_count = self._conn.execute(
                "SELECT COUNT(*) FROM draft_audit_log WHERE workspace_id=?", (wid,)
            ).fetchone()[0]
            csat_row = self._conn.execute(
                "SELECT AVG(csat_score) FROM conversation_meta WHERE workspace_id=? AND csat_score>=0",
                (wid,),
            ).fetchone()
            avg_csat = round(float(csat_row[0]), 1) if csat_row and csat_row[0] is not None else None
        return {
            "workspace_id": wid,
            "conversation_count": int(conv_count or 0),
            "audit_count": int(audit_count or 0),
            "avg_csat": avg_csat,
        }

    def get_contact_sessions(
        self,
        contact_id: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """N1：返回同一 contact_id 的所有跨平台会话记录（含 CSAT/情绪趋势）。

        用于客户画像 (K3) 展示该客户历史会话全貌。
        """
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_meta WHERE contact_id=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (cid, max(1, int(limit))),
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for key in ("intent_history", "emotion_history"):
                try:
                    d[key] = json.loads(d.get(key) or "[]")
                except Exception:
                    d[key] = []
            d["emotion_trend"] = self._compute_emotion_trend(d.get("emotion_history") or [])
            result.append(d)
        return result

    def get_contact_csat_avg(self, contact_id: str) -> Optional[float]:
        """N1：返回同一 contact_id 所有对话的 CSAT 均值（-1 表示无数据）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT AVG(csat_score) FROM conversation_meta "
                "WHERE contact_id=? AND csat_score >= 0",
                (cid,),
            ).fetchone()
        if row and row[0] is not None:
            return round(float(row[0]), 1)
        return None

    def update_conv_csat(self, conversation_id: str, csat_score: float) -> None:
        """M1：写入对话 CSAT 评分（会话结束时调用），同时触发 S1 A/B 结果回填。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        csat_score = round(max(0.0, min(5.0, float(csat_score))), 1)
        with self._lock:
            self._conn.execute(
                "UPDATE conversation_meta SET csat_score=? WHERE conversation_id=?",
                (csat_score, cid),
            )
            self._conn.commit()
        # S1: 自动回填 A/B 测试结果（best-effort，不影响主流程）
        try:
            from src.inbox.ab_testing import ABTestingStore
            ab = ABTestingStore(self)
            ab.record_outcome(conversation_id=cid, csat_score=csat_score)
        except Exception:
            import logging as _log
            _log.getLogger(__name__).debug("S1 A/B outcome 回填失败（已忽略）", exc_info=True)

    # ── P61-3：分组批量触达日志 ───────────────────────────
    def record_outreach(
        self, conversation_id: str, *, batch_id: str = "", platform: str = "",
        account_id: str = "", status: str = "sent", note: str = "",
        ts: Optional[float] = None,
    ) -> int:
        """记录一次触达（execution 阶段调用）。返回新行 id。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0
        t = float(ts if ts is not None else self._now())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outreach_log (conversation_id, batch_id, platform, "
                "account_id, status, note, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, str(batch_id or ""), str(platform or ""), str(account_id or ""),
                 str(status or "sent"), str(note or ""), t),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def last_outreach_ts(self, conversation_id: str) -> float:
        """该会话最近一次触达时间戳；从未触达返回 0（用于 cooldown 判定）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM outreach_log WHERE conversation_id=? "
                "ORDER BY ts DESC LIMIT 1", (cid,),
            ).fetchone()
        return float(row["ts"]) if row else 0.0

    def last_outreach_ts_bulk(self, conversation_ids: List[str]) -> Dict[str, float]:
        """批量取多会话最近触达 ts（避免 N+1 查询）。"""
        ids = [str(c or "").strip() for c in (conversation_ids or []) if str(c or "").strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, MAX(ts) AS mx FROM outreach_log "
                f"WHERE conversation_id IN ({placeholders}) GROUP BY conversation_id",
                ids,
            ).fetchall()
        return {r["conversation_id"]: float(r["mx"] or 0) for r in rows}

    def outreach_batch_stats(self, batch_id: str) -> Dict[str, Any]:
        """某批次的回执统计：按 status 计数 + 总数。"""
        bid = str(batch_id or "").strip()
        if not bid:
            return {"batch_id": "", "total": 0, "by_status": {}}
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM outreach_log WHERE batch_id=? "
                "GROUP BY status", (bid,),
            ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        return {"batch_id": bid, "total": sum(by_status.values()), "by_status": by_status}

    def outreach_response_stats(
        self, batch_id: str, *,
        response_window_days: float = 7.0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """P61-5：触达效果回流——某批次"已发送"消息的回复率。

        判定：对每条 status='sent' 的触达，看其会话在触达 ts 之后（且在
        response_window_days 窗口内，<=0 表示不限窗）是否收到**入站**消息。
        返回 sent / responded / response_rate / avg_response_minutes。
        纯查询、无副作用，可随时回看（回复是异步累积的）。
        """
        bid = str(batch_id or "").strip()
        if not bid:
            return {"batch_id": "", "sent": 0, "responded": 0,
                    "response_rate": 0.0, "avg_response_minutes": 0.0}
        window_s = float(response_window_days) * 86400.0 if response_window_days and response_window_days > 0 else 0.0
        # 每条 sent 触达 → 该会话触达后首个入站消息 ts（相关子查询，走 idx_msg_conv_ts）
        sql = (
            "SELECT o.ts AS sent_ts, "
            "(SELECT MIN(m.ts) FROM messages m "
            " WHERE m.conversation_id = o.conversation_id "
            "   AND m.direction = 'in' AND m.ts > o.ts) AS reply_ts "
            "FROM outreach_log o WHERE o.batch_id = ? AND o.status = 'sent'"
        )
        with self._lock:
            rows = self._conn.execute(sql, (bid,)).fetchall()
        sent = len(rows)
        responded = 0
        latencies: List[float] = []
        for r in rows:
            reply_ts = r["reply_ts"]
            if reply_ts is None:
                continue
            delta = float(reply_ts) - float(r["sent_ts"])
            if delta <= 0:
                continue
            if window_s > 0 and delta > window_s:
                continue
            responded += 1
            latencies.append(delta)
        rate = round(responded / sent, 4) if sent else 0.0
        avg_min = round(sum(latencies) / len(latencies) / 60.0, 1) if latencies else 0.0
        return {
            "batch_id": bid, "sent": sent, "responded": responded,
            "response_rate": rate, "avg_response_minutes": avg_min,
            "response_window_days": response_window_days,
        }

    def get_conv_meta(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """I1：获取对话智能元数据，含情绪趋势计算结果。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_meta WHERE conversation_id = ?", (cid,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for key in ("intent_history", "emotion_history"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except Exception:
                d[key] = []
        # 计算情绪趋势
        d["emotion_trend"] = self._compute_emotion_trend(d.get("emotion_history") or [])
        return d

    def _compute_emotion_trend(self, history: List[str]) -> str:
        """将最近情绪序列映射为 rising/falling/stable 趋势。

        用于 UI 显示趋势箭头（📈升级/📉降级/📊平稳）。
        """
        if len(history) < 2:
            return "stable"
        # 映射情绪到数值（愤怒=最高负面=0，感谢=最高正面=6）
        order = self._EMOTION_ORDER
        def score(e: str) -> int:
            try:
                idx = order.index(e)
                # 情绪紧张度：愤怒=高，感谢=低
                return len(order) - 1 - idx
            except ValueError:
                return 2  # 默认中间值
        recent = history[-5:]
        scores = [score(e) for e in recent]
        if len(scores) >= 2:
            delta = scores[-1] - scores[0]
            if delta >= 2:
                return "rising"   # 情绪在恶化
            if delta <= -2:
                return "falling"  # 情绪在好转
        return "stable"

    # ── I3 回复模板库 ─────────────────────────────────────────────

    def seed_templates(self, templates: List[Dict[str, Any]]) -> int:
        """I3：预置模板（幂等：id 冲突则跳过）。返回实际插入数量。"""
        now = self._now()
        inserted = 0
        for t in templates:
            tid = str(t.get("id") or uuid.uuid4().hex)
            with self._lock:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO reply_templates
                       (id, title, content, language, platform, scene,
                        created_by, created_at, updated_at, used_count, is_active)
                       VALUES (?,?,?,?,?,?,?,?,?,0,1)""",
                    (tid, str(t.get("title") or ""), str(t.get("content") or ""),
                     str(t.get("language") or "zh"), str(t.get("platform") or ""),
                     str(t.get("scene") or ""), str(t.get("created_by") or "system"),
                     now, now),
                )
                self._conn.commit()
            inserted += int(cur.rowcount or 0)
        return inserted

    def list_templates(
        self,
        *,
        language: str = "",
        platform: str = "",
        scene: str = "",
        search: str = "",
        limit: int = 100,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """I3：列出模板，支持多维度过滤。"""
        clauses, params = [], []
        if active_only:
            clauses.append("is_active = 1")
        if language:
            clauses.append("language = ?")
            params.append(language)
        if platform:
            clauses.append("(platform = ? OR platform = '')")
            params.append(platform)
        if scene:
            clauses.append("scene = ?")
            params.append(scene)
        if search:
            clauses.append("(title LIKE ? OR content LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(200, max(1, int(limit))))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM reply_templates {where} ORDER BY used_count DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def create_template(
        self,
        *,
        title: str,
        content: str,
        language: str = "zh",
        platform: str = "",
        scene: str = "",
        created_by: str = "admin",
    ) -> str:
        """I3：创建新模板，返回 id。"""
        tid = uuid.uuid4().hex
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO reply_templates
                   (id, title, content, language, platform, scene,
                    created_by, created_at, updated_at, used_count, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,0,1)""",
                (tid, str(title), str(content), str(language or "zh"),
                 str(platform or ""), str(scene or ""), str(created_by or "admin"),
                 now, now),
            )
            self._conn.commit()
        return tid

    def update_template(
        self,
        tid: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        language: Optional[str] = None,
        platform: Optional[str] = None,
        scene: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> bool:
        """I3：更新模板字段（仅传入非 None 的字段），返回是否找到并更新。"""
        updates, params = [], []
        if title is not None:
            updates.append("title = ?")
            params.append(str(title))
        if content is not None:
            updates.append("content = ?")
            params.append(str(content))
        if language is not None:
            updates.append("language = ?")
            params.append(str(language))
        if platform is not None:
            updates.append("platform = ?")
            params.append(str(platform))
        if scene is not None:
            updates.append("scene = ?")
            params.append(str(scene))
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if is_active else 0)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(self._now())
        params.append(str(tid))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE reply_templates SET {', '.join(updates)} WHERE id = ?", params
            )
            self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def delete_template(self, tid: str) -> bool:
        """I3：软删除（is_active=0），保留历史记录。"""
        return self.update_template(tid, is_active=False)

    def increment_template_usage(self, tid: str) -> None:
        """I3：模板使用计数 +1（best-effort）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE reply_templates SET used_count = used_count + 1, updated_at = ?"
                " WHERE id = ?",
                (self._now(), str(tid)),
            )
            self._conn.commit()

    # ── T1: 会话级标签 + 归档 ──────────────────────────────────────────
    def save_conv_summary(self, conversation_id: str, summary: str) -> bool:
        """Phase 19：写入会话 AI 摘要（归档时自动生成）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta (conversation_id, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary    = excluded.summary,
                    updated_at = excluded.updated_at
                """,
                (cid, str(summary or ""), self._now()),
            )
            self._conn.commit()
        return True

    def get_conv_tags(self, conversation_id: str) -> List[str]:
        """T1：获取会话标签列表。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        with self._lock:
            row = self._conn.execute(
                "SELECT conv_tags FROM conversation_meta WHERE conversation_id = ?", (cid,)
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["conv_tags"] or "[]")
        except Exception:
            return []

    def set_conv_tags(self, conversation_id: str, tags: List[str]) -> bool:
        """T1：覆写会话标签列表；如 conversation_meta 行不存在则插入。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        tags = [str(t).strip() for t in tags if str(t).strip()]
        tags_json = json.dumps(tags, ensure_ascii=False)
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta (conversation_id, conv_tags, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    conv_tags  = excluded.conv_tags,
                    updated_at = excluded.updated_at
                """,
                (cid, tags_json, now),
            )
            self._conn.commit()
        return True

    def set_conv_archived(self, conversation_id: str, archived: bool) -> bool:
        """T1：标记/取消归档；如 conversation_meta 行不存在则插入。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return False
        val = 1 if archived else 0
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_meta (conversation_id, archived, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    archived   = excluded.archived,
                    updated_at = excluded.updated_at
                """,
                (cid, val, now),
            )
            self._conn.commit()
        return True

    def tag_stats(self, *, since: float = 0.0) -> List[Dict[str, Any]]:
        """T2：聚合每个标签的会话数、未读数、平均等待秒数（用于标签概览 strip）。

        算法：扫 conversation_meta 中 conv_tags 非空的行，join conversations 表取
        unread / last_ts；join messages 最近1条方向判断是否等待回复。
        since=0 表示全量（不按时间过滤）。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.conversation_id, m.conv_tags, m.archived,
                       c.unread, c.last_ts, c.platform
                FROM conversation_meta m
                LEFT JOIN conversations c ON c.conversation_id = m.conversation_id
                WHERE m.conv_tags != '[]' AND m.conv_tags != ''
                """
            ).fetchall()
        # 聚合
        tag_map: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                tags = json.loads(r["conv_tags"] or "[]")
            except Exception:
                tags = []
            for tag in tags:
                if not tag:
                    continue
                entry = tag_map.setdefault(tag, {
                    "tag": tag, "count": 0, "unread": 0,
                    "archived": 0, "platforms": set(),
                })
                entry["count"] += 1
                entry["unread"] += int(r["unread"] or 0)
                if r.get("archived"):
                    entry["archived"] += 1
                if r.get("platform"):
                    entry["platforms"].add(str(r["platform"]))
        result = []
        for entry in sorted(tag_map.values(), key=lambda x: -x["count"]):
            result.append({
                "tag": entry["tag"],
                "count": entry["count"],
                "unread": entry["unread"],
                "archived": entry["archived"],
                "platforms": sorted(entry["platforms"]),
            })
        return result

    def list_conv_tags_map(self, conversation_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """T1：批量获取会话标签+归档状态（用于列表渲染）。

        返回 {conv_id: {"tags": [...], "archived": bool}}。
        """
        if not conversation_ids:
            return {}
        placeholders = ",".join("?" * len(conversation_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT conversation_id, conv_tags, archived FROM conversation_meta"
                f" WHERE conversation_id IN ({placeholders})",
                conversation_ids,
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                tags = json.loads(r["conv_tags"] or "[]")
            except Exception:
                tags = []
            result[r["conversation_id"]] = {
                "tags": tags,
                "archived": bool(r.get("archived", 0)),
            }
        return result

    # ── V1: 坐席协作注解（Phase 25） ─────────────────────────────────────
    def add_conv_note(
        self,
        conversation_id: str,
        body: str,
        *,
        agent_id: str = "",
        agent_name: str = "",
        mentions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """V1：在会话中添加内部注解（对客户不可见）。

        返回刚插入的 note dict；body 为空时抛 ValueError。
        """
        body = str(body or "").strip()
        if not body:
            raise ValueError("note body 不能为空")
        cid = str(conversation_id or "").strip()
        if not cid:
            raise ValueError("conversation_id 不能为空")
        import uuid as _uuid
        note_id = str(_uuid.uuid4())
        ts = self._now()
        mentions_list: List[str] = [str(m) for m in (mentions or []) if str(m).strip()]
        with self._lock:
            self._conn.execute(
                """INSERT INTO conv_notes
                   (note_id, conversation_id, agent_id, agent_name, body, mentions, ts, edited_ts)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (note_id, cid, str(agent_id or ""), str(agent_name or ""),
                 body, json.dumps(mentions_list, ensure_ascii=False), ts),
            )
            self._conn.commit()
        return {
            "note_id": note_id, "conversation_id": cid,
            "agent_id": agent_id, "agent_name": agent_name,
            "body": body, "mentions": mentions_list, "ts": ts, "edited_ts": 0,
        }

    def list_conv_notes(
        self, conversation_id: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """V1：获取会话的全部内部注解（按时间升序）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return []
        limit = max(1, min(200, int(limit or 50)))
        with self._lock:
            rows = self._conn.execute(
                """SELECT note_id, conversation_id, agent_id, agent_name,
                          body, mentions, ts, edited_ts
                   FROM conv_notes WHERE conversation_id = ?
                   ORDER BY ts ASC LIMIT ?""",
                (cid, limit),
            ).fetchall()
        result = []
        for r in rows:
            try:
                mentions_list = json.loads(r["mentions"] or "[]")
            except Exception:
                mentions_list = []
            result.append({
                "note_id": r["note_id"], "conversation_id": r["conversation_id"],
                "agent_id": r["agent_id"], "agent_name": r["agent_name"],
                "body": r["body"], "mentions": mentions_list,
                "ts": r["ts"], "edited_ts": r["edited_ts"],
            })
        return result

    def edit_conv_note(
        self, note_id: str, body: str, *, agent_id: str = ""
    ) -> bool:
        """V1：编辑注解内容（仅注解作者或管理员可编辑，此处由 API 层鉴权）。"""
        body = str(body or "").strip()
        if not body:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE conv_notes SET body=?, edited_ts=? WHERE note_id=?",
                (body, self._now(), str(note_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_conv_note(self, note_id: str, *, agent_id: str = "") -> bool:
        """V1：删除注解（由 API 层控制权限）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conv_notes WHERE note_id=?", (str(note_id),)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── X1: 客户 360° 时间轴（Phase 31） ────────────────────────────────

    def get_contact_timeline(
        self, contact_id: str, *, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """X1：聚合指定客户所有互动事件，按时间倒序返回时间轴。

        事件类型（event_type）：
          message   — 入站/出站消息
          note      — 坐席内部注解（含@提及）
          archived  — 会话归档
          summary   — AI 摘要生成
          conv_open — 会话首次建立

        优化：主体采用 UNION ALL 单次扫描 + Python 侧合并，减少 DB 往返次数。
        """
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        limit = max(1, min(500, int(limit or 100)))

        events: List[Dict[str, Any]] = []

        with self._lock:
            # 第一步：查属于该 contact 的所有会话（最多 50 条）
            conv_rows = self._conn.execute(
                """SELECT conversation_id, platform, display_name, created_at
                   FROM conversations WHERE contact_id = ?
                   ORDER BY last_ts DESC LIMIT 50""",
                (cid,),
            ).fetchall()
            if not conv_rows:
                return []

            conv_ids = [r["conversation_id"] for r in conv_rows]
            conv_info = {r["conversation_id"]: dict(r) for r in conv_rows}

            # 会话建立事件
            for cv in conv_rows:
                events.append({
                    "event_type": "conv_open",
                    "ts": float(cv["created_at"] or 0),
                    "conversation_id": cv["conversation_id"],
                    "platform": cv["platform"],
                    "display_name": cv["display_name"],
                    "preview": f"开始 {cv['platform']} 会话",
                    "meta": {},
                    "_sort_key": float(cv["created_at"] or 0),
                })

            ph = ",".join("?" * len(conv_ids))
            fetch_limit = min(limit * 4, 400)  # 多拉一些以保证排序后截断准确

            # 第二步：UNION ALL 一次性拉取消息 + 注解（同型字段）
            union_rows = self._conn.execute(
                f"""
                SELECT 'message' AS etype, m.conversation_id, m.ts,
                       substr(m.text, 1, 120) AS preview,
                       m.direction AS dir, m.media_type AS extra1, m.message_id AS extra2, '' AS extra3
                FROM messages m
                WHERE m.conversation_id IN ({ph}) AND m.text != ''

                UNION ALL

                SELECT 'note' AS etype, n.conversation_id, n.ts,
                       substr(n.body, 1, 120) AS preview,
                       n.agent_name AS dir, n.note_id AS extra1, n.mentions AS extra2, '' AS extra3
                FROM conv_notes n
                WHERE n.conversation_id IN ({ph})

                ORDER BY ts DESC LIMIT ?
                """,
                conv_ids + conv_ids + [fetch_limit],
            ).fetchall()

            for r in union_rows:
                cv = conv_info.get(r["conversation_id"], {})
                ts = float(r["ts"] or 0)
                if r["etype"] == "message":
                    events.append({
                        "event_type": "message",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["preview"] or ""),
                        "meta": {
                            "direction": r["dir"],
                            "media_type": r["extra1"] or None,
                            "message_id": r["extra2"],
                        },
                        "_sort_key": ts,
                    })
                else:  # note
                    try:
                        mentions = json.loads(r["extra2"] or "[]")
                    except Exception:
                        mentions = []
                    events.append({
                        "event_type": "note",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["preview"] or ""),
                        "meta": {
                            "note_id": r["extra1"],
                            "agent_name": r["dir"],
                            "mentions": mentions,
                        },
                        "_sort_key": ts,
                    })

            # 第三步：会话 meta（归档 + 摘要，单独查询，数量少）
            meta_rows = self._conn.execute(
                f"""SELECT conversation_id, archived, summary, updated_at
                    FROM conversation_meta WHERE conversation_id IN ({ph})""",
                conv_ids,
            ).fetchall()
            for r in meta_rows:
                cv = conv_info.get(r["conversation_id"], {})
                ts = float(r["updated_at"] or 0)
                if r["archived"]:
                    events.append({
                        "event_type": "archived",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": "会话已归档",
                        "meta": {"archived": True},
                        "_sort_key": ts,
                    })
                if r["summary"]:
                    events.append({
                        "event_type": "summary",
                        "ts": ts,
                        "conversation_id": r["conversation_id"],
                        "platform": cv.get("platform", ""),
                        "display_name": cv.get("display_name", ""),
                        "preview": str(r["summary"] or "")[:120],
                        "meta": {"summary": r["summary"]},
                        "_sort_key": ts,
                    })

        # 全局按 ts 降序，移除内部排序辅助键，截断
        events.sort(key=lambda e: e.get("_sort_key", e["ts"]), reverse=True)
        for e in events:
            e.pop("_sort_key", None)
        return events[:limit]

    # ── W1: 客户活跃时段热力图（Phase 27） ───────────────────────────────
    def activity_heatmap(
        self, *, days: int = 30, platform: str = "", direction: str = "inbound"
    ) -> Dict[str, Any]:
        """W1：统计最近 N 天的消息量按星期×小时分布（本地时区）。

        返回:
          {
            hours: [0..23],          # x 轴
            weekdays: [0..6],        # y 轴 (0=周一, 6=周日)
            matrix: [[int,…],…],    # shape: 7×24, 每格消息数
            peak_hour: int,          # 全局峰值小时
            peak_weekday: int,       # 全局峰值星期
            total: int,              # 总消息数
          }
        """
        import time as _time
        since_ts = _time.time() - max(1, min(365, int(days or 30))) * 86400
        dir_cond = ""
        params: List[Any] = [since_ts]
        if direction in ("inbound", "outbound"):
            dir_cond = " AND direction = ?"
            params.append(direction)
        plat_cond = ""
        if platform:
            plat_cond = """
                AND conversation_id IN (
                    SELECT conversation_id FROM conversations WHERE platform = ?
                )"""
            params.append(str(platform))

        sql = f"""
            SELECT ts FROM messages
            WHERE ts >= ?{dir_cond}{plat_cond}
              AND text != ''
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # 初始化 7×24 矩阵
        matrix = [[0] * 24 for _ in range(7)]
        for r in rows:
            ts_val = r[0]
            try:
                import datetime as _dt
                dt = _dt.datetime.fromtimestamp(float(ts_val))
                wd = dt.weekday()  # 0=Monday
                hr = dt.hour
                matrix[wd][hr] += 1
            except Exception:
                pass

        total = sum(matrix[wd][hr] for wd in range(7) for hr in range(24))
        peak_hour, peak_wd = 0, 0
        peak_val = -1
        for wd in range(7):
            for hr in range(24):
                if matrix[wd][hr] > peak_val:
                    peak_val = matrix[wd][hr]
                    peak_hour = hr
                    peak_wd = wd

        return {
            "hours": list(range(24)),
            "weekdays": list(range(7)),
            "matrix": matrix,
            "peak_hour": peak_hour,
            "peak_weekday": peak_wd,
            "total": total,
            "days": days,
            "direction": direction,
        }

    # ── Y1: QA 质检评分（Phase 34） ───────────────────────────────────────

    def compute_and_store_qa_score(self, conversation_id: str) -> Dict[str, Any]:
        """Y1：计算指定会话的质检评分并持久化到 conversation_meta。

        Steps:
          1. 拉取会话全量消息
          2. 调用 QAScorer 进行规则评分
          3. 将结果 JSON 写入 conversation_meta.qa_score
          4. 返回评分结果
        """
        from src.inbox.qa_scorer import QAScorer
        cid = str(conversation_id or "").strip()
        if not cid:
            return {}
        # 拉取消息（最多最近 200 条，足够评分）
        with self._lock:
            rows = self._conn.execute(
                """SELECT direction, text, ts FROM messages
                   WHERE conversation_id = ?
                   ORDER BY ts ASC LIMIT 200""",
                (cid,),
            ).fetchall()
        messages = [dict(r) for r in rows]
        result = QAScorer().score(messages)
        # 写回 conversation_meta
        self.patch_conv_meta(cid, {"qa_score": json.dumps(result, ensure_ascii=False)})
        return result

    def get_qa_score(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Y1：读取已存储的质检评分（不重新计算）。"""
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT qa_score FROM conversation_meta WHERE conversation_id = ?",
                (cid,),
            ).fetchone()
        if not row:
            return None
        raw = str(row["qa_score"] or "").strip()
        if not raw or raw == "{}":
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def batch_agent_qa_stats(
        self, *, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Y1：聚合最近 N 天各坐席的 QA 评分均值（用于 agent_perf 看板）。

        返回 [{agent_id, agent_name, avg_score, count, grade_dist}]
        """
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT cm.claimed_by, cm.qa_score, cm.updated_at
                   FROM conversation_meta cm
                   WHERE cm.updated_at >= ? AND cm.qa_score != '' AND cm.qa_score != '{}'
                   ORDER BY cm.updated_at DESC LIMIT 2000""",
                (since,),
            ).fetchall()
        # 按 claimed_by 分组聚合
        agg: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            agent = str(row["claimed_by"] or "").strip()
            if not agent:
                continue
            try:
                qa = json.loads(row["qa_score"] or "{}")
                score = int(qa.get("score") or 0)
                grade = str(qa.get("grade") or "N/A")
            except Exception:
                continue
            if agent not in agg:
                agg[agent] = {"agent_id": agent, "scores": [], "grades": {}}
            agg[agent]["scores"].append(score)
            agg[agent]["grades"][grade] = agg[agent]["grades"].get(grade, 0) + 1

        result = []
        for agent_id, data in sorted(agg.items()):
            scores = data["scores"]
            avg = round(sum(scores) / len(scores)) if scores else 0
            result.append({
                "agent_id": agent_id,
                "agent_name": agent_id,  # 可在 API 层替换真实姓名
                "avg_score": avg,
                "count": len(scores),
                "grade": QAScorer_grade(avg),
                "grade_dist": data["grades"],
            })
        result.sort(key=lambda x: x["avg_score"], reverse=True)
        return result

    # ── Z1: 流失预警（Phase 35）──────────────────────────────────────────────

    def list_churn_risk_conversations(
        self,
        *,
        silence_days: int = 7,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Z1：列出高流失风险会话（最近 N 天无活动且末条为入站）。

        辅助 ChurnPredictor 的数据获取层（独立于联系人表）。
        """
        cutoff = time.time() - silence_days * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, c.platform, c.display_name,
                          c.contact_id, c.last_ts, cm.claimed_by, cm.churn_risk,
                          cm.qa_score, cm.archived
                   FROM conversations c
                   LEFT JOIN conversation_meta cm
                     ON cm.conversation_id = c.conversation_id
                   WHERE c.last_ts <= ? AND (cm.archived IS NULL OR cm.archived = 0)
                   ORDER BY c.last_ts ASC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def store_churn_risk(self, conversation_id: str, risk_level: str, reasons: List[str]) -> None:
        """Z1：持久化流失风险评估结果。"""
        data = json.dumps({"level": risk_level, "reasons": reasons, "ts": time.time()}, ensure_ascii=False)
        self.patch_conv_meta(conversation_id, {"churn_risk": data})

    # ── 辅助（跨方法） ───────────────────────────────────────────────────────

    def _auto_archive_candidates(self, idle_hours: int = 24) -> List[Dict[str, Any]]:
        """P36：查找超过 idle_hours 小时未活动且未归档的会话。"""
        cutoff = time.time() - idle_hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, c.display_name, c.platform, c.last_ts, c.contact_id
                   FROM conversations c
                   LEFT JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
                   WHERE c.last_ts <= ? AND (cm.archived IS NULL OR cm.archived = 0)
                     AND (cm.auto_archived_at IS NULL OR cm.auto_archived_at = 0)
                   ORDER BY c.last_ts ASC LIMIT 100""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]


    # ── AA1: 自定义动作 / 工作链 CRUD（Phase 37） ─────────────────────────

    def list_workflow_actions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_actions ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_workflow_action(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        action_id = str(data.get("action_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_actions
                   (action_id, name, action_type, config_json, icon, enabled, sort_order, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(action_id) DO UPDATE SET
                     name=excluded.name, action_type=excluded.action_type,
                     config_json=excluded.config_json, icon=excluded.icon,
                     enabled=excluded.enabled, sort_order=excluded.sort_order,
                     updated_at=excluded.updated_at""",
                (
                    action_id,
                    str(data.get("name") or ""),
                    str(data.get("action_type") or "template"),
                    json.dumps(data.get("config") or {}, ensure_ascii=False),
                    str(data.get("icon") or "💡"),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("sort_order") or 0),
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return action_id

    def delete_workflow_action(self, action_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workflow_actions WHERE action_id = ?", (action_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_workflow_chains(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_chains ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_workflow_chain(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        chain_id = str(data.get("chain_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_chains
                   (chain_id, name, steps_json, trigger_conditions, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(chain_id) DO UPDATE SET
                     name=excluded.name, steps_json=excluded.steps_json,
                     trigger_conditions=excluded.trigger_conditions,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    chain_id,
                    str(data.get("name") or ""),
                    json.dumps(data.get("steps") or [], ensure_ascii=False),
                    json.dumps(data.get("trigger_conditions") or {}, ensure_ascii=False),
                    1 if data.get("enabled", True) else 0,
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return chain_id

    def delete_workflow_chain(self, chain_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM workflow_chains WHERE chain_id = ?", (chain_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def start_chain_execution(
        self,
        chain_id: str,
        conversation_id: str,
        context: Dict[str, Any],
        *,
        schedule_first_step: bool = True,
    ) -> str:
        import uuid as _uuid
        exec_id = str(_uuid.uuid4())
        now = time.time()
        next_at = 0.0
        if schedule_first_step:
            chain = self.get_workflow_chain(chain_id)
            if chain:
                try:
                    steps = json.loads(chain.get("steps_json") or "[]")
                    if steps:
                        delay_h = float(steps[0].get("delay_hours") or 0)
                        next_at = now if delay_h <= 0 else now + delay_h * 3600
                except Exception:
                    next_at = now
            else:
                next_at = now
        with self._lock:
            self._conn.execute(
                """INSERT INTO workflow_executions
                   (exec_id, chain_id, conversation_id, current_step, status,
                    context_json, started_at, updated_at, next_step_at, last_result_json)
                   VALUES (?,?,?,0,'running',?,?,?,?, '')""",
                (exec_id, chain_id, conversation_id,
                 json.dumps(context, ensure_ascii=False), now, now, next_at),
            )
            self._conn.commit()
        return exec_id

    def get_workflow_chain(self, chain_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_chains WHERE chain_id = ?", (chain_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_workflow_execution(self, exec_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT we.*, wc.name as chain_name, wc.steps_json
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.exec_id = ?""",
                (exec_id,),
            ).fetchone()
        return dict(row) if row else None

    def has_running_chain(self, conversation_id: str, chain_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM workflow_executions
                   WHERE conversation_id = ? AND chain_id = ? AND status = 'running' LIMIT 1""",
                (conversation_id, chain_id),
            ).fetchone()
        return row is not None

    def list_due_workflow_executions(
        self, now: float, *, limit: int = 30, stuck_sec: float = 120,
    ) -> List[Dict[str, Any]]:
        """P44/P47：拉取到期应执行的工作链（含卡住恢复）。"""
        stuck_before = now - stuck_sec
        with self._lock:
            rows = self._conn.execute(
                """SELECT we.*, wc.name as chain_name, wc.steps_json
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.status = 'running'
                     AND (
                       (we.next_step_at > 0 AND we.next_step_at <= ?)
                       OR (we.next_step_at = 0 AND we.updated_at <= ?)
                     )
                   ORDER BY we.next_step_at ASC, we.updated_at ASC LIMIT ?""",
                (now, stuck_before, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_workflow_execution(
        self,
        exec_id: str,
        *,
        current_step: int = 0,
        next_step_at: float = 0,
        last_result: Optional[Dict[str, Any]] = None,
        status: str = "",
        context_json: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        sets = ["current_step = ?", "updated_at = ?", "next_step_at = ?"]
        params: List[Any] = [current_step, now, next_step_at]
        if last_result is not None:
            sets.append("last_result_json = ?")
            params.append(json.dumps(last_result, ensure_ascii=False))
        if context_json is not None:
            sets.append("context_json = ?")
            params.append(json.dumps(context_json, ensure_ascii=False))
        if status:
            sets.append("status = ?")
            params.append(status)
        params.append(exec_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE workflow_executions SET {', '.join(sets)} WHERE exec_id = ?",
                params,
            )
            self._conn.commit()

    def complete_workflow_execution(self, exec_id: str, *, status: str = "completed") -> None:
        self.update_workflow_execution(exec_id, status=status, next_step_at=0)

    def cancel_workflow_execution(self, exec_id: str) -> bool:
        """P47：取消运行中的工作链执行。"""
        ex = self.get_workflow_execution(exec_id)
        if not ex or ex.get("status") != "running":
            return False
        self.complete_workflow_execution(exec_id, status="cancelled")
        return True

    def list_chain_executions(
        self,
        *,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """P47：全局/按会话列出工作链执行记录（含会话展示名）。"""
        clauses = ["1=1"]
        params: List[Any] = []
        if status:
            clauses.append("we.status = ?")
            params.append(status)
        if conversation_id:
            clauses.append("we.conversation_id = ?")
            params.append(conversation_id)
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT we.*, wc.name as chain_name, wc.steps_json,
                           c.display_name, c.platform
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   LEFT JOIN conversations c ON c.conversation_id = we.conversation_id
                   WHERE {' AND '.join(clauses)}
                   ORDER BY
                     CASE we.status WHEN 'running' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                     we.updated_at DESC
                   LIMIT ?""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    _META_PATCH_COLUMNS = frozenset({
        "rel_stage_cached", "rel_stage_pending", "rel_stage_pending_ts",
        "rel_reunion_ack_ts", "qa_score", "churn_risk",
    })

    def _ensure_conv_meta_row(self, conversation_id: str) -> None:
        cid = str(conversation_id or "").strip()
        if not cid:
            return
        now = self._now()
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO conversation_meta (conversation_id, updated_at)
                   VALUES (?, ?)""",
                (cid, now),
            )
            self._conn.commit()

    def patch_conv_meta(self, conversation_id: str, fields: Dict[str, Any]) -> None:
        """按列名局部更新 conversation_meta（仅允许白名单字段）。"""
        cid = str(conversation_id or "").strip()
        if not cid or not fields:
            return
        safe = {k: v for k, v in fields.items() if k in self._META_PATCH_COLUMNS}
        if not safe:
            return
        self._ensure_conv_meta_row(cid)
        now = self._now()
        sets = [f"{k}=?" for k in safe]
        vals = list(safe.values()) + [now, cid]
        with self._lock:
            self._conn.execute(
                f"UPDATE conversation_meta SET {', '.join(sets)}, updated_at=? WHERE conversation_id=?",
                vals,
            )
            self._conn.commit()

    def get_rel_stage_cached(self, conversation_id: str) -> str:
        meta = self.get_rel_stage_meta(conversation_id)
        return meta["confirmed"]

    def get_rel_stage_meta(self, conversation_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT rel_stage_cached, rel_stage_pending, rel_stage_pending_ts,
                          rel_reunion_ack_ts
                   FROM conversation_meta WHERE conversation_id = ?""",
                (conversation_id,),
            ).fetchone()
        if not row:
            return {
                "confirmed": "", "pending": "", "pending_ts": 0.0, "reunion_ack_ts": 0.0,
            }
        return {
            "confirmed": str(row["rel_stage_cached"] or ""),
            "pending": str(row["rel_stage_pending"] or ""),
            "pending_ts": float(row["rel_stage_pending_ts"] or 0),
            "reunion_ack_ts": float(row["rel_reunion_ack_ts"] or 0),
        }

    def set_rel_stage_cached(self, conversation_id: str, stage: str) -> None:
        self.patch_conv_meta(conversation_id, {"rel_stage_cached": str(stage or "")})

    def set_rel_stage_pending(self, conversation_id: str, stage: str, *, ts: Optional[float] = None) -> None:
        now = float(ts if ts is not None else self._now())
        self.patch_conv_meta(conversation_id, {
            "rel_stage_pending": str(stage or ""),
            "rel_stage_pending_ts": now,
        })

    def clear_rel_stage_pending(self, conversation_id: str) -> None:
        self.patch_conv_meta(conversation_id, {
            "rel_stage_pending": "",
            "rel_stage_pending_ts": 0,
        })

    def confirm_rel_stage(self, conversation_id: str, stage: str) -> None:
        self.patch_conv_meta(conversation_id, {
            "rel_stage_cached": str(stage or ""),
            "rel_stage_pending": "",
            "rel_stage_pending_ts": 0,
        })

    # ── P50: 客户级关系阶段 ───────────────────────────────────────────────

    def get_contact_rel_stage(self, contact_id: str) -> Optional[Dict[str, Any]]:
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contact_rel_stage WHERE contact_id = ?", (cid,),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def set_contact_rel_stage(
        self,
        contact_id: str,
        stage: str,
        *,
        updated_by: str = "",
        reunion_ack_ts: Optional[float] = None,
    ) -> None:
        cid = str(contact_id or "").strip()
        if not cid:
            return
        now = self._now()
        ack = float(reunion_ack_ts) if reunion_ack_ts is not None else None
        with self._lock:
            if ack is not None:
                self._conn.execute(
                    """INSERT INTO contact_rel_stage
                       (contact_id, confirmed_stage, updated_by, updated_at, reunion_ack_ts)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(contact_id) DO UPDATE SET
                         confirmed_stage = excluded.confirmed_stage,
                         updated_by = excluded.updated_by,
                         updated_at = excluded.updated_at,
                         reunion_ack_ts = excluded.reunion_ack_ts""",
                    (cid, str(stage or ""), str(updated_by or ""), now, ack),
                )
            else:
                self._conn.execute(
                    """INSERT INTO contact_rel_stage
                       (contact_id, confirmed_stage, updated_by, updated_at, reunion_ack_ts)
                       VALUES (?,?,?,?,0)
                       ON CONFLICT(contact_id) DO UPDATE SET
                         confirmed_stage = excluded.confirmed_stage,
                         updated_by = excluded.updated_by,
                         updated_at = excluded.updated_at""",
                    (cid, str(stage or ""), str(updated_by or ""), now),
                )
            self._conn.commit()

    def list_conv_rel_stages_for_contact(self, contact_id: str) -> Dict[str, str]:
        """返回该客户各会话的已确认阶段。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return {}
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.conversation_id, COALESCE(cm.rel_stage_cached, '') AS stage
                   FROM conversations c
                   LEFT JOIN conversation_meta cm ON cm.conversation_id = c.conversation_id
                   WHERE c.contact_id = ?""",
                (cid,),
            ).fetchall()
        return {str(r["conversation_id"]): str(r["stage"] or "") for r in rows}

    def sync_convs_to_stage(self, contact_id: str, stage: str) -> int:
        """P50：将该客户所有会话的确认阶段对齐为 stage，返回更新条数。"""
        cid = str(contact_id or "").strip()
        st = str(stage or "")
        if not cid or not st:
            return 0
        conv_ids = list(self.list_conv_rel_stages_for_contact(cid).keys())
        n = 0
        for conv_id in conv_ids:
            self.confirm_rel_stage(conv_id, st)
            n += 1
        return n

    def list_contact_stage_audits(
        self, contact_id: str, *, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """P51：查询客户级关系阶段审计事件（跨会话聚合）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        lim = max(1, min(200, int(limit or 50)))
        actions = (
            "stage_confirm", "stage_downgrade", "stage_reunion", "stage_sync",
        )
        ph_act = ",".join("?" * len(actions))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT dal.*, c.platform, c.display_name
                FROM draft_audit_log dal
                LEFT JOIN conversations c ON c.conversation_id = dal.conversation_id
                WHERE (
                    dal.conversation_id IN (
                        SELECT conversation_id FROM conversations WHERE contact_id = ?
                    )
                    OR dal.draft_id = ?
                )
                AND dal.action IN ({ph_act})
                ORDER BY dal.ts DESC
                LIMIT ?
                """,
                (cid, f"contact:{cid}", *actions, lim),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── P54: Copilot 采纳率统计 ─────────────────────────────────────────

    def record_copilot_impression(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        trigger: str = "",
        stage: str = "",
        polished: bool = False,
        suggestion_count: int = 0,
        top_source: str = "",
    ) -> None:
        from src.inbox.copilot_stats import encode_impression
        self.record_draft_audit(
            "", action="copilot_impression", agent_id=agent_id,
            reason=encode_impression(
                trigger=trigger, stage=stage, polished=polished,
                suggestion_count=suggestion_count, top_source=top_source,
            ),
            conversation_id=conversation_id,
        )

    def record_copilot_adopt(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        match: str = "exact",
        source: str = "",
        polished: bool = False,
        trigger: str = "",
        stage: str = "",
        suggested_preview: str = "",
        sent_preview: str = "",
    ) -> None:
        from src.inbox.copilot_stats import encode_adopt
        self.record_draft_audit(
            "", action="copilot_adopt", agent_id=agent_id,
            reason=encode_adopt(
                match=match, source=source, polished=polished,
                trigger=trigger, stage=stage,
                suggested_preview=suggested_preview, sent_preview=sent_preview,
            ),
            conversation_id=conversation_id,
        )

    def list_copilot_audit_rows(
        self, *, since_ts: float = 0.0, agent_id: str = "", limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        actions = ("copilot_impression", "copilot_adopt", "copilot_polish")
        ph = ",".join("?" * len(actions))
        clauses = [f"action IN ({ph})", "ts>=?"]
        params: List[Any] = [*actions, float(since_ts)]
        if agent_id:
            clauses.append("agent_id=?")
            params.append(str(agent_id))
        sql = (
            f"SELECT * FROM draft_audit_log WHERE {' AND '.join(clauses)} "
            f"ORDER BY ts DESC LIMIT ?"
        )
        params.append(max(1, min(5000, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_copilot_stats(
        self, *, since_ts: float = 0.0, agent_id: str = "",
    ) -> Dict[str, Any]:
        from src.inbox.copilot_stats import aggregate_copilot_stats
        rows = self.list_copilot_audit_rows(
            since_ts=since_ts, agent_id=agent_id, limit=3000,
        )
        return aggregate_copilot_stats(rows)

    def confirm_rel_stage_with_contact(
        self,
        conversation_id: str,
        contact_id: str,
        stage: str,
        *,
        updated_by: str = "",
        sync_all_convs: bool = True,
    ) -> None:
        """P50：确认会话阶段并同步客户级（可选同步全部会话）。"""
        self.confirm_rel_stage(conversation_id, stage)
        cid = str(contact_id or "").strip()
        if not cid:
            return
        self.set_contact_rel_stage(cid, stage, updated_by=updated_by)
        if sync_all_convs:
            self.sync_convs_to_stage(cid, stage)

    def ack_rel_reunion(self, conversation_id: str, *, ts: Optional[float] = None) -> None:
        now = float(ts if ts is not None else self._now())
        self.patch_conv_meta(conversation_id, {"rel_reunion_ack_ts": now})

    def get_agent_stage_confirm_counts(
        self, *, since_ts: float = 0.0,
    ) -> Dict[str, Dict[str, int]]:
        """P48：统计各坐席确认过的关系阶段次数（来自 stage_confirm 审计）。"""
        from src.utils.companion_relationship import STAGE_ORDER
        stage_set = set(STAGE_ORDER)
        out: Dict[str, Dict[str, int]] = {}
        with self._lock:
            rows = self._conn.execute(
                """SELECT agent_id, reason FROM draft_audit_log
                   WHERE ts >= ? AND action = 'stage_confirm' AND agent_id != ''""",
                (float(since_ts),),
            ).fetchall()
        for row in rows:
            aid = str(row["agent_id"] or "").strip()
            reason = str(row["reason"] or "")
            if not aid or "→" not in reason:
                continue
            target = reason.split("→")[-1].strip()
            stage_id = target if target in stage_set else ""
            if not stage_id:
                from src.utils.companion_relationship import STAGE_LABEL_ZH
                for sid in stage_set:
                    if target == STAGE_LABEL_ZH.get(sid, sid):
                        stage_id = sid
                        break
            if stage_id not in stage_set:
                continue
            out.setdefault(aid, {})
            out[aid][stage_id] = out[aid].get(stage_id, 0) + 1
        return out

    def get_agent_mention_counts(self, *, since_ts: float = 0.0) -> Dict[str, int]:
        """P48：统计各坐席被 @ 次数（协作活跃度代理）。"""
        counts: Dict[str, int] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT mentions FROM conv_notes WHERE ts >= ?",
                (float(since_ts),),
            ).fetchall()
        for row in rows:
            try:
                mentions = json.loads(row["mentions"] or "[]")
            except Exception:
                mentions = []
            for m in mentions:
                aid = str(m or "").strip()
                if aid:
                    counts[aid] = counts.get(aid, 0) + 1
        return counts

    def get_recent_mention_note(
        self,
        conversation_id: str,
        agent_id: str,
        *,
        within_hours: float = 48,
    ) -> Optional[Dict[str, Any]]:
        """P49：获取最近 @ 指定坐席的协作注解。"""
        cid = str(conversation_id or "").strip()
        aid = str(agent_id or "").strip()
        if not cid or not aid:
            return None
        cutoff = time.time() - max(1.0, float(within_hours)) * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT note_id, agent_id, agent_name, body, mentions, ts
                   FROM conv_notes
                   WHERE conversation_id = ? AND ts >= ?
                   ORDER BY ts DESC LIMIT 30""",
                (cid, cutoff),
            ).fetchall()
        for row in rows:
            try:
                mentions = json.loads(row["mentions"] or "[]")
            except Exception:
                mentions = []
            if aid in [str(m) for m in mentions]:
                return {
                    "note_id": row["note_id"],
                    "body": row["body"],
                    "agent_id": row["agent_id"],
                    "agent_name": row["agent_name"],
                    "ts": float(row["ts"] or 0),
                }
        return None

    def has_overdue_chain_execution(
        self, conversation_id: str, *, overdue_sec: float = 3600,
    ) -> bool:
        """P48：会话是否有超时未推进的运行中工作链。"""
        cutoff = time.time() - max(60.0, float(overdue_sec))
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM workflow_executions
                   WHERE conversation_id = ? AND status = 'running'
                     AND next_step_at > 0 AND next_step_at < ?
                   LIMIT 1""",
                (conversation_id, cutoff),
            ).fetchone()
        return row is not None

    def get_conv_chain_executions(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT we.*, wc.name as chain_name
                   FROM workflow_executions we
                   LEFT JOIN workflow_chains wc ON wc.chain_id = we.chain_id
                   WHERE we.conversation_id = ?
                   ORDER BY we.started_at DESC LIMIT 20""",
                (conversation_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── BB1: 分流路由规则 CRUD（Phase 38） ────────────────────────────────

    def list_routing_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM routing_rules ORDER BY priority DESC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_routing_rule(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        rule_id = str(data.get("rule_id") or _uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO routing_rules
                   (rule_id, name, conditions, assign_to, priority, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET
                     name=excluded.name, conditions=excluded.conditions,
                     assign_to=excluded.assign_to, priority=excluded.priority,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (
                    rule_id,
                    str(data.get("name") or ""),
                    json.dumps(data.get("conditions") or {}, ensure_ascii=False),
                    str(data.get("assign_to") or ""),
                    int(data.get("priority") or 0),
                    1 if data.get("enabled", True) else 0,
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return rule_id

    def delete_routing_rule(self, rule_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM routing_rules WHERE rule_id = ?", (rule_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── CC1: 剧本话题 CRUD（Phase 40） ────────────────────────────────────

    def list_script_topics(self, stage: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            if stage:
                rows = self._conn.execute(
                    """SELECT * FROM script_topics WHERE stage = ? AND enabled = 1
                       ORDER BY sort_order ASC, created_at ASC""",
                    (stage,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM script_topics ORDER BY stage, sort_order ASC"
                ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
            except Exception:
                d["tags"] = []
            result.append(d)
        return result

    def upsert_script_topic(self, data: Dict[str, Any]) -> str:
        import uuid as _uuid
        topic_id = str(data.get("topic_id") or _uuid.uuid4())
        now = time.time()
        tags = data.get("tags") or []
        with self._lock:
            self._conn.execute(
                """INSERT INTO script_topics
                   (topic_id, stage, title, opener, hint, tags_json, chain_id,
                    enabled, sort_order, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(topic_id) DO UPDATE SET
                     stage=excluded.stage, title=excluded.title, opener=excluded.opener,
                     hint=excluded.hint, tags_json=excluded.tags_json, chain_id=excluded.chain_id,
                     enabled=excluded.enabled, sort_order=excluded.sort_order,
                     updated_at=excluded.updated_at""",
                (
                    topic_id,
                    str(data.get("stage") or "initial"),
                    str(data.get("title") or ""),
                    str(data.get("opener") or ""),
                    str(data.get("hint") or ""),
                    json.dumps(tags, ensure_ascii=False),
                    str(data.get("chain_id") or ""),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("sort_order") or 0),
                    float(data.get("created_at") or now),
                    now,
                ),
            )
            self._conn.commit()
        return topic_id

    def delete_script_topic(self, topic_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM script_topics WHERE topic_id = ?", (topic_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get_messages_for_contact(self, contact_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """P41：拉取客户所有会话的消息（跨会话聚合）。"""
        cid = str(contact_id or "").strip()
        if not cid:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.conversation_id, m.direction, m.text, m.ts
                   FROM messages m
                   JOIN conversations c ON c.conversation_id = m.conversation_id
                   WHERE c.contact_id = ?
                   ORDER BY m.ts ASC LIMIT ?""",
                (cid, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_and_store_engagement(self, contact_id: str) -> Dict[str, Any]:
        """P41：计算并持久化客户互动积分。"""
        from src.inbox.engagement_scorer import EngagementScorer
        cid = str(contact_id or "").strip()
        if not cid:
            return {}

        existing_ach: List[str] = []
        prev_points = 0
        with self._lock:
            row = self._conn.execute(
                "SELECT points, achievements_json, history_json FROM contact_engagement WHERE contact_id = ?",
                (cid,),
            ).fetchone()
        if row:
            prev_points = int(row["points"] or 0)
            try:
                existing_ach = json.loads(row["achievements_json"] or "[]")
            except Exception:
                existing_ach = []

        messages = self.get_messages_for_contact(cid)
        # 沉默天数
        silence_days = 0.0
        if messages:
            inbound = [m for m in messages if m.get("direction") in ("in", "inbound")]
            if inbound:
                last_ts = max(float(m.get("ts") or 0) for m in inbound)
                silence_days = max(0.0, (time.time() - last_ts) / 86400)

        result = EngagementScorer().compute(
            messages,
            existing_achievements=existing_ach,
            last_silence_days=silence_days,
        )

        # 历史快照（保留最近 30 条）
        history: List[Dict[str, Any]] = []
        if row:
            try:
                history = json.loads(row["history_json"] or "[]")
            except Exception:
                history = []
        history.append({"ts": time.time(), "points": result["points"]})
        history = history[-30:]

        with self._lock:
            self._conn.execute(
                """INSERT INTO contact_engagement
                   (contact_id, points, level, breakdown_json, achievements_json, history_json, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                     points=excluded.points, level=excluded.level,
                     breakdown_json=excluded.breakdown_json,
                     achievements_json=excluded.achievements_json,
                     history_json=excluded.history_json,
                     updated_at=excluded.updated_at""",
                (
                    cid,
                    int(result["points"]),
                    str(result["level"]),
                    json.dumps(result["breakdown"], ensure_ascii=False),
                    json.dumps(result["achievements"], ensure_ascii=False),
                    json.dumps(history, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()
        result["previous_points"] = prev_points
        result["history"] = history
        return result

    def get_contact_engagement(self, contact_id: str) -> Optional[Dict[str, Any]]:
        cid = str(contact_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM contact_engagement WHERE contact_id = ?", (cid,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["breakdown"] = json.loads(d.pop("breakdown_json", "{}") or "{}")
        except Exception:
            d["breakdown"] = {}
        try:
            d["achievements"] = json.loads(d.pop("achievements_json", "[]") or "[]")
        except Exception:
            d["achievements"] = []
        try:
            d["history"] = json.loads(d.pop("history_json", "[]") or "[]")
        except Exception:
            d["history"] = []
        return d


def QAScorer_grade(score: int) -> str:
    """模块级辅助：直接从分数得等级（避免重复实例化）。"""
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 45: return "D"
    return "F"
