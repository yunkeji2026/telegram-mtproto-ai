"""Contact 合并决策。

两条合并路径：

1. **Token 合并**（已由 HandoffTokenService.consume 验证过）——直接高置信合并。
2. **多信号融合**——无 token 时，尝试从"最近引流过"的候选池中找最佳匹配。

评分公式（与 v4 设计文档 §4.3 一致）：
    confidence = 0.30*name_match + 0.20*lang_match + 0.15*tz_match
               + 0.25*time_proximity + 0.10*style_match

阈值：
    >= 0.85 → auto_merge
    >= 0.60 → manual_review
    <  0.60 → keep_isolated
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from .models import (
    ChannelIdentity,
    HandoffToken,
    MergeDecision,
    MergeSignals,
    DECISION_AUTO_MERGE,
    DECISION_MANUAL_REVIEW,
    DECISION_KEEP_ISOLATED,
    MERGE_AUTO_THRESHOLD,
    MERGE_REVIEW_THRESHOLD,
    MERGE_AMBIGUITY_MARGIN,
)
from .store import ContactStore

logger = logging.getLogger(__name__)


# ── 信号计算 ─────────────────────────────────────────────
def _name_match(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # SequenceMatcher 的 ratio 对短字符串友好，适合昵称
    return SequenceMatcher(None, a, b).ratio()


def _equal_match(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if a.strip().lower() == b.strip().lower() else 0.0


def _time_proximity(delta_seconds: int, window_seconds: int = 72 * 3600) -> float:
    """delta=0 返回 1.0，delta>=window 返回 0.0，中间线性下降。"""
    if delta_seconds < 0:
        return 0.0
    if delta_seconds >= window_seconds:
        return 0.0
    return 1.0 - (delta_seconds / window_seconds)


def score_signals(s: MergeSignals) -> Tuple[float, dict]:
    """纯函数：按权重合并信号为 [0,1] confidence。

    返回 (confidence, breakdown)；breakdown 保留每个信号的加权贡献，便于事后审计。
    """
    weights = {
        "name_match":     (0.30, s.name_match),
        "lang_match":     (0.20, s.lang_match),
        "tz_match":       (0.15, s.tz_match),
        "time_proximity": (0.25, s.time_proximity),
        "style_match":    (0.10, s.style_match),
    }
    contribs = {k: round(w * v, 4) for k, (w, v) in weights.items()}
    conf = sum(contribs.values())
    # 数值安全夹紧 + 3 位小数：
    # 浮点求和会产生 0.3+0.2+0.15+0.25 = 0.8999999999999999 的边界误差，
    # 合并决策不需要 >3 位精度，round 到 3 位可以稳定阈值判断。
    conf = max(0.0, min(1.0, conf))
    conf = round(conf, 3)
    return conf, contribs


def decide(confidence: float, reason: str = "") -> MergeDecision:
    if confidence >= MERGE_AUTO_THRESHOLD:
        d = DECISION_AUTO_MERGE
    elif confidence >= MERGE_REVIEW_THRESHOLD:
        d = DECISION_MANUAL_REVIEW
    else:
        d = DECISION_KEEP_ISOLATED
    return MergeDecision(confidence=confidence, decision=d, reason=reason)


# ── 服务：把决策作用到 Store ─────────────────────────────
@dataclass
class MergeCandidate:
    """候选匹配的 Messenger 侧身份 + 其最近 token 的元数据。"""
    messenger_ci: ChannelIdentity
    handoff_token_issued_at: int


class MergeService:
    def __init__(self, store: ContactStore) -> None:
        self._store = store

    # ── 路径 1：token 驱动合并（happy path） ─────────────────
    def apply_token_merge(self, consumed_token: HandoffToken, line_ci_id: str, *, trace_id: str = "") -> bool:
        """consume 成功后调用：把 line_ci 迁到 messenger 那边的 Contact。"""
        messenger_ci = self._store.get_channel_identity(consumed_token.issued_from_ci_id)
        if not messenger_ci:
            logger.warning("token %s refers to missing ci %s",
                           consumed_token.token, consumed_token.issued_from_ci_id)
            return False
        return self._store.relink_channel_identity(
            ci_id=line_ci_id,
            new_contact_id=messenger_ci.contact_id,
            linked_via="token",
            attribution_confidence=0.95,
            trace_id=trace_id,
        )

    # ── 路径 2：无 token，走信号融合 ────────────────────────
    def recent_handoff_candidates(self, now: Optional[int] = None) -> List[Tuple[MergeCandidate, dict]]:
        """列出近期引流过、token 仍可用的 Messenger 身份 + 对应 Contact 元数据。

        使用 Store 的 JOIN API 一次读完，避免 N+1。
        返回 (candidate, contact_meta) 列表；contact_meta 含 primary_name/language_hint/timezone_hint。
        """
        rows = self._store.list_all_active_tokens_with_ci()
        out: List[Tuple[MergeCandidate, dict]] = []
        for r in rows:
            ci = ChannelIdentity(
                channel_identity_id=r["channel_identity_id"],
                contact_id=r["contact_id"],
                channel=r["channel"],
                account_id=r["account_id"],
                external_id=r["external_id"],
                display_name=r["display_name"],
            )
            cand = MergeCandidate(messenger_ci=ci, handoff_token_issued_at=r["issued_at"])
            meta = {
                "primary_name": r["primary_name"],
                "language_hint": r["language_hint"],
                "timezone_hint": r["timezone_hint"],
            }
            out.append((cand, meta))
        return out

    def evaluate(
        self,
        *,
        line_ci: ChannelIdentity,
        line_display_name: str,
        line_lang: str,
        line_tz: str,
        now: Optional[int] = None,
    ) -> Tuple[Optional[MergeCandidate], MergeDecision]:
        """遍历候选池，挑 confidence 最高的一个给出决策。

        歧义保护：如果前两名 confidence 差距 < MERGE_AMBIGUITY_MARGIN，即使最高达到了
        auto 阈值，也降级到 manual_review——系统分不清的情况下必须人工。
        """
        now = now if now is not None else self._store._now()  # noqa: SLF001
        candidates = self.recent_handoff_candidates()
        if not candidates:
            return None, decide(0.0, reason="no_candidates")

        scored: List[Tuple[MergeCandidate, float, dict]] = []
        for cand, meta in candidates:
            signals = MergeSignals(
                name_match=_name_match(line_display_name, meta["primary_name"] or cand.messenger_ci.display_name),
                lang_match=_equal_match(line_lang, meta["language_hint"]),
                tz_match=_equal_match(line_tz, meta["timezone_hint"]),
                time_proximity=_time_proximity(now - cand.handoff_token_issued_at),
                style_match=0.0,  # MVP：留给 persona_fingerprint 未来接入
            )
            conf, breakdown = score_signals(signals)
            scored.append((cand, conf, breakdown))

        # 按 confidence 降序
        scored.sort(key=lambda x: x[1], reverse=True)
        best_cand, best_conf, best_breakdown = scored[0]
        second_conf = scored[1][1] if len(scored) >= 2 else 0.0

        decision = decide(best_conf, reason=f"best_of_{len(scored)}_candidates")
        decision.breakdown = best_breakdown

        # 歧义降级：best 和 runner-up 差距太小，降级到 review
        if (decision.decision == DECISION_AUTO_MERGE
                and (best_conf - second_conf) < MERGE_AMBIGUITY_MARGIN):
            decision.decision = DECISION_MANUAL_REVIEW
            decision.reason = (
                f"ambiguous_top2: best={best_conf:.3f} runnerup={second_conf:.3f} "
                f"margin<{MERGE_AMBIGUITY_MARGIN}"
            )

        return best_cand, decision

    def apply_signal_decision(
        self,
        *,
        line_ci_id: str,
        best: MergeCandidate,
        decision: MergeDecision,
        trace_id: str = "",
    ) -> Optional[str]:
        """根据决策执行动作：

        - auto_merge → 直接 relink
        - manual_review → 入审核队列，返回 review_id
        - keep_isolated → 什么也不做
        """
        if decision.decision == DECISION_AUTO_MERGE:
            ok = self._store.relink_channel_identity(
                ci_id=line_ci_id,
                new_contact_id=best.messenger_ci.contact_id,
                linked_via="heuristic",
                attribution_confidence=decision.confidence,
                trace_id=trace_id,
            )
            return "merged" if ok else None
        if decision.decision == DECISION_MANUAL_REVIEW:
            return self._store.enqueue_merge_review(
                candidate_ci_id=line_ci_id,
                target_contact_id=best.messenger_ci.contact_id,
                confidence=decision.confidence,
                breakdown=decision.breakdown,
            )
        return None

    # ── 人工审核动作 ───────────────────────────────────────
    def approve_review(self, review_id: str, *, resolved_by: str = "", trace_id: str = "") -> bool:
        """运营点"通过"：原子地（1）把 review 状态改成 approved（2）触发 relink。

        幂等保证：如果 ci 已经在 target_contact_id 上（可能上次 relink 成功但 resolve 失败），
        直接标记 resolved 并返回 True——避免 review 永远无法关闭。

        失败返回 False（review 不存在 / 已解决 / 候选 ci 消失 / 未知目标 / relink 失败）。
        """
        review = self._store.get_review(review_id)
        if not review or review["status"] != "pending":
            return False
        ci = self._store.get_channel_identity(review["candidate_ci_id"])
        if not ci:
            return False
        # 幂等短路：已经合并过
        if ci.contact_id == review["target_contact_id"]:
            return self._store.resolve_review(
                review_id, status="approved", resolved_by=resolved_by)
        # 先试 relink，成功后再改状态——relink 失败时保持 pending 可重试
        try:
            ok = self._store.relink_channel_identity(
                ci_id=review["candidate_ci_id"],
                new_contact_id=review["target_contact_id"],
                linked_via="manual",
                attribution_confidence=review["confidence"],
                trace_id=trace_id,
            )
        except ValueError:
            # 目标 contact 不存在等硬错误
            return False
        if not ok:
            return False
        return self._store.resolve_review(review_id, status="approved", resolved_by=resolved_by)

    def reject_review(self, review_id: str, *, resolved_by: str = "") -> bool:
        """运营点"拒绝"：只改状态，不动 Contact。"""
        return self._store.resolve_review(review_id, status="rejected", resolved_by=resolved_by)
