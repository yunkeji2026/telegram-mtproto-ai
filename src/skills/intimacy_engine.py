"""IntimacyEngine — 读 journey_events 算 0-100 的 intimacy_score。

设计原则：
  1. **事件流是真相**：所有信号从 journey_events 聚合，不依赖额外字段
  2. **纯计算 + 可选写回**：compute_intimacy 是纯函数；refresh 才写 DB
  3. **和 companion_relationship 并存**：不改现有 conversion 域 4 阶段逻辑；
     intimacy 是平行的数值信号，供 Readiness / Funnel 统计使用

信号（MVP 4 个，合计权重 1.0）：
  - turn_count_in    0.25   累计 msg_in 数（封顶 20）
  - mutuality        0.25   msg_in 与 msg_out 的对称性（单向 spam 得分低）
  - active_days_7d   0.25   最近 7 天有活动的"日"数
  - recency          0.25   距上次活动的近期度（14 天半衰期）

不在 MVP：
  - emotional_keyword_density （需词典 + 稳定 tokenizer）
  - self_disclosure_count     （需 LLM classifier，独立项目）
  - nighttime_ratio            （需 Contact 本地时区，W4 加）

以上三项的 hook 都留在接口里（kwargs 占位），将来加时不改签名。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 权重（sum = 1.0）
_W_TURNS = 0.25
_W_MUTUALITY = 0.25
_W_DAYS = 0.25
_W_RECENCY = 0.25

# 信号的归一化参数
_TURN_SAT = 20          # 20 条 msg_in 即视作 "满"
_DAYS_SAT = 5           # 7 天内活动 5 天视作"满"
_RECENCY_HALFLIFE_S = 14 * 24 * 3600    # 2 周半衰期

# 沉默衰减（W3-D2.4 GAP 修复 / 2026-05-05）：
# turn_count_in / mutuality 不随时间下降，导致"100 轮互动 + 沉默 30 天"
# 用户仍 50 分过 reactivation 阈值。在 score 顶层加每周 5% 全局衰减，
# 7 天 grace period 后开始（短期沉默是正常的）。
_SILENCE_DECAY_GRACE_DAYS = 7
_SILENCE_DECAY_PER_WEEK = 0.95


@dataclass
class IntimacyBreakdown:
    score: float                    # 0-100
    turn_count_in: int
    turn_count_out: int
    active_days_7d: int
    days_since_last_msg: float
    contributions: Dict[str, float]  # 每个信号的加权贡献（round 3 位）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "turn_count_in": self.turn_count_in,
            "turn_count_out": self.turn_count_out,
            "active_days_7d": self.active_days_7d,
            "days_since_last_msg": round(self.days_since_last_msg, 2),
            "contributions": self.contributions,
        }


class IntimacyEngine:
    """计算 + 可选写回 Journey.intimacy_score。"""

    def __init__(self, store) -> None:
        self._store = store

    # ── 纯函数：计算不写库 ────────────────────────────────
    def compute_intimacy(
        self, journey_id: str, *, now: Optional[int] = None,
    ) -> IntimacyBreakdown:
        now = now if now is not None else int(time.time())
        # 读近 500 条事件——正常 Journey 不会超。
        # TODO(perf)：如果 Journey 真跑到 >500 events，这里是热点：
        #   1) 改 store 加 count_events_by_type + 只读活跃窗口内的 msg_*
        #   2) 或按 stage 的"入场快照"只重算增量
        events = self._store.list_events(journey_id, limit=500)
        msg_in_ts = [e["ts"] for e in events if e["event_type"] == "msg_in"]
        msg_out_ts = [e["ts"] for e in events if e["event_type"] == "msg_out"]

        turn_in = len(msg_in_ts)
        turn_out = len(msg_out_ts)

        # 1. turn_count
        s_turns = min(turn_in, _TURN_SAT) / _TURN_SAT

        # 2. mutuality = 0 如果单向；1 如果完全对称
        if turn_in == 0 and turn_out == 0:
            s_mutual = 0.0
        elif turn_in == 0 or turn_out == 0:
            s_mutual = 0.0
        else:
            s_mutual = min(turn_in, turn_out) / max(turn_in, turn_out)

        # 3. active_days_7d
        seven_days_ago = now - 7 * 24 * 3600
        all_msg_ts = msg_in_ts + msg_out_ts
        active_days = len({_to_day(t) for t in all_msg_ts if t >= seven_days_ago})
        s_days = min(active_days, _DAYS_SAT) / _DAYS_SAT

        # 4. recency
        last_ts = max(all_msg_ts) if all_msg_ts else 0
        days_since = (now - last_ts) / 86400.0 if last_ts else float("inf")
        if last_ts == 0:
            s_recency = 0.0
        else:
            dt = max(0, now - last_ts)
            s_recency = 0.5 ** (dt / _RECENCY_HALFLIFE_S)

        # 加权
        contribs = {
            "turn_count_in": round(_W_TURNS * s_turns, 3),
            "mutuality":     round(_W_MUTUALITY * s_mutual, 3),
            "active_days_7d":round(_W_DAYS * s_days, 3),
            "recency":       round(_W_RECENCY * s_recency, 3),
        }
        score = sum(contribs.values())
        score = max(0.0, min(1.0, score))
        score = score * 100  # 0-100

        # P-W3D2.4 (2026-05-05) 沉默衰减：超过 grace 天数后每周乘 0.95
        # 防"长期沉默用户仍被 reactivation 骚扰"
        silence_decay = 1.0
        if (
            last_ts > 0
            and days_since != float("inf")
            and days_since > _SILENCE_DECAY_GRACE_DAYS
        ):
            weeks_silent = (
                days_since - _SILENCE_DECAY_GRACE_DAYS
            ) / 7.0
            silence_decay = _SILENCE_DECAY_PER_WEEK ** weeks_silent
            score = score * silence_decay
        contribs["silence_decay"] = round(silence_decay, 3)
        score = round(score, 1)

        return IntimacyBreakdown(
            score=score,
            turn_count_in=turn_in,
            turn_count_out=turn_out,
            active_days_7d=active_days,
            days_since_last_msg=days_since if last_ts else float("inf"),
            contributions=contribs,
        )

    # ── 写回 Journey ──────────────────────────────────────
    def refresh_journey_intimacy(
        self, journey_id: str, *, now: Optional[int] = None,
    ) -> IntimacyBreakdown:
        """重算并写回 intimacy_score。

        ★ W3-D2.5：用 _touch=False 写库 — intimacy 重算不该把 journey 视为"又活跃"，
        否则 silent_days 永远清零，reactivation_scheduler 永远找不到候选。
        """
        now = now if now is not None else int(time.time())
        bd = self.compute_intimacy(journey_id, now=now)
        self._store.update_journey(
            journey_id,
            _touch=False,
            intimacy_score=bd.score,
            intimacy_updated_at=now,
        )
        return bd


def _to_day(ts: int) -> int:
    """把 unix 秒换成 UTC 日期序（86400 秒一档）。

    注意：不使用 Contact 本地时区——这是 MVP 简化。W4 升级时从 Contact.timezone_hint 读。
    """
    return ts // 86400
