"""Contact 领域对象（dataclass）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── Channel 枚举（字符串常量，不用 Enum 保持 sqlite 友好） ──────────────
CHANNEL_MESSENGER = "messenger"
CHANNEL_LINE = "line"
CHANNEL_TELEGRAM = "telegram"
CHANNEL_MOBILE = "mobile"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_WEB = "web"
VALID_CHANNELS = {
    CHANNEL_MESSENGER, CHANNEL_LINE, CHANNEL_TELEGRAM,
    CHANNEL_MOBILE, CHANNEL_WHATSAPP, CHANNEL_WEB,
}

# ── Journey 状态（状态机节点） ──────────────────────────────────────
STAGE_INITIAL = "INITIAL"
STAGE_ENGAGED = "ENGAGED"
STAGE_WARMING = "WARMING"
STAGE_HANDOFF_READY = "HANDOFF_READY"
STAGE_HANDOFF_SENT = "HANDOFF_SENT"
STAGE_LINE_ADDED = "LINE_ADDED"
STAGE_LINE_ACCEPTED = "LINE_ACCEPTED"
STAGE_LINE_ENGAGED = "LINE_ENGAGED"
STAGE_BONDED = "BONDED"
STAGE_CONVERTED = "CONVERTED"
STAGE_LOST_HANDOFF = "LOST_HANDOFF"
STAGE_LOST_LINE_SILENT = "LOST_LINE_SILENT"
STAGE_NEEDS_MANUAL_MERGE = "NEEDS_MANUAL_MERGE"

# ── 「成交/完成」阶段集合（单一来源） ────────────────────────────────
# 收件箱 done 筛选 + 「已成交」KPI + 经营看板「已成交」卡片 共用此口径，
# 避免前后端各写一份漂移（P5-2）。经 _page_ctx 注入模板（{{ funnel_done_stages|tojson }}），
# 后端指标直接引用本常量。
# 口径说明：沿用收件箱既有「已完成引流并互动」的成功桶（含 LINE 通过/二次互动），
# 保持既有 KPI 与筛选行为不变；狭义「实际成交」为 {BONDED, CONVERTED}。
FUNNEL_DONE_STAGES = frozenset({
    STAGE_LINE_ACCEPTED,
    STAGE_LINE_ENGAGED,
    STAGE_BONDED,
    STAGE_CONVERTED,
})

# 狭义「实际成交」阶段（人工闭环成交徽标 / 标记转化按钮判定用）。
# 与广义 FUNNEL_DONE_STAGES 区分：done = 成功桶（含 LINE 通过/互动），won = 真正成交。
# 收件箱 `_isDone`（是否已成交、是否还显示「标记成交」按钮）走此狭义集合，单一来源注入前端。
WON_STAGES = frozenset({
    STAGE_BONDED,
    STAGE_CONVERTED,
})

# ── 阶段完备性分区（每个 Journey 阶段必属且仅属一个桶）─────────────────────
# 成功桶 = FUNNEL_DONE_STAGES（上方已定义，含 LINE 通过/互动/绑定/成交）。
# 其余阶段按语义分三桶；四桶合起来对全部 STAGE_* 构成**无重无漏**划分，
# 由 tests/test_funnel_done_single_source.py 的完备性门禁锁死——
# 新增 STAGE_* 若漏归类会被门禁点名（防被 KPI/漏斗分析静默漏算）。
# 注：WON_STAGES 是 FUNNEL_DONE_STAGES 的**子分类**（正交），不参与本划分。
IN_PROGRESS_STAGES = frozenset({
    STAGE_INITIAL,
    STAGE_ENGAGED,
    STAGE_WARMING,
    STAGE_HANDOFF_READY,
    STAGE_HANDOFF_SENT,
    STAGE_LINE_ADDED,
})
# 流失桶（引流后失联 / 加 LINE 后长期沉默）
LOST_STAGES = frozenset({
    STAGE_LOST_HANDOFF,
    STAGE_LOST_LINE_SILENT,
})
# 系统标记态（非漏斗位置：等待人工合并身份）
SPECIAL_STAGES = frozenset({
    STAGE_NEEDS_MANUAL_MERGE,
})

# ── 合并决策结果 ────────────────────────────────────────────────
DECISION_AUTO_MERGE = "auto_merge"
DECISION_MANUAL_REVIEW = "manual_review"
DECISION_KEEP_ISOLATED = "keep_isolated"

# 多信号融合阈值。
# 注意：style_match 在 MVP 未启用（权重 0.10 贡献不到分），
# 其余四项全满也只有 0.90；把 auto 阈值设到 0.90 意味着"所有可用信号全中"
# 才允许自动合并，其他情况都要人工审核——
# 宁可错过也不要错认（false negative 可纠，false positive 毁人设）。
MERGE_AUTO_THRESHOLD = 0.90
MERGE_REVIEW_THRESHOLD = 0.60
# 当前两名候选 confidence 差距小于此值时，降级到人工审核（防止"双胞胎"误判）。
MERGE_AMBIGUITY_MARGIN = 0.10


@dataclass
class Contact:
    """跨平台的"人"。"""
    contact_id: str
    primary_name: str = ""
    language_hint: str = ""
    timezone_hint: str = ""
    country_hint: str = ""
    created_at: int = 0
    last_active_at: int = 0
    notes: str = ""
    follow_up_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "primary_name": self.primary_name,
            "language_hint": self.language_hint,
            "timezone_hint": self.timezone_hint,
            "country_hint": self.country_hint,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "notes": self.notes,
            "follow_up_at": self.follow_up_at,
        }


@dataclass
class ChannelIdentity:
    """某 Contact 在某平台/账号上的身份。"""
    channel_identity_id: str
    contact_id: str
    channel: str                      # messenger / line / telegram
    account_id: str                   # 我们的哪个账号接待他
    external_id: str                  # 对方在该平台的 ID（fb_id / line_chat_key / ...）
    direction: str = "first_seen"     # first_seen / linked_from
    linked_at: int = 0
    linked_via: str = ""              # token / regex / heuristic / manual
    attribution_confidence: float = 0.0
    display_name: str = ""            # 对方在该平台的昵称

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_identity_id": self.channel_identity_id,
            "contact_id": self.contact_id,
            "channel": self.channel,
            "account_id": self.account_id,
            "external_id": self.external_id,
            "direction": self.direction,
            "linked_at": self.linked_at,
            "linked_via": self.linked_via,
            "attribution_confidence": self.attribution_confidence,
            "display_name": self.display_name,
        }


@dataclass
class HandoffToken:
    """引流短码。"""
    token: str
    issued_from_ci_id: str
    issued_at: int
    expires_at: int
    consumed_by_ci_id: str = ""
    consumed_at: int = 0
    revoked_reason: str = ""

    @property
    def is_consumed(self) -> bool:
        return bool(self.consumed_by_ci_id)

    @property
    def is_revoked(self) -> bool:
        return bool(self.revoked_reason)

    def is_expired(self, now: int) -> bool:
        # 语义：当前时间达到或超过 expires_at 即算过期。
        # 与 store.consume_token 的 SQL 条件保持一致（WHERE expires_at > ?）。
        return now >= self.expires_at

    def is_active(self, now: int) -> bool:
        return (not self.is_consumed) and (not self.is_revoked) and (not self.is_expired(now))


@dataclass
class Journey:
    """Contact 的关系状态与漏斗位置。"""
    journey_id: str
    contact_id: str
    persona_id: str = ""
    funnel_stage: str = STAGE_INITIAL
    intimacy_score: float = 0.0
    engagement_score: float = 0.0
    readiness_score: float = 0.0
    intimacy_updated_at: int = 0
    context_snapshot_json: str = ""
    snapshot_refreshed_at: int = 0
    created_at: int = 0
    updated_at: int = 0


@dataclass
class MergeSignals:
    """合并决策的输入信号。"""
    name_match: float = 0.0          # 0..1 (Jaccard/Levenshtein 已归一化)
    lang_match: float = 0.0          # 0 / 1
    tz_match: float = 0.0            # 0 / 1
    time_proximity: float = 0.0      # 72h 内 handoff_sent 的 Contact 越近越高
    style_match: float = 0.0         # 0..1（用词/emoji 偏好吻合度）


@dataclass
class MergeDecision:
    """合并决策输出。"""
    confidence: float
    decision: str                    # auto_merge / manual_review / keep_isolated
    reason: str = ""
    breakdown: Dict[str, float] = field(default_factory=dict)
