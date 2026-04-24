"""ReactivationScheduler — 每日找出"该重新激活"的 Contact。

场景：LINE 上加完好友聊了一次后对方沉默 3 天——漏斗会卡死在 LINE_ENGAGED 或 BONDED。
这时需要主动说一句（"好久没聊了"）把关系继续下去。本模块负责生成候选名单，
消息发送由 LINE RPA 消费后上报 mark_sent。

候选条件（全部满足）：
  1. funnel_stage ∈ {LINE_ENGAGED, BONDED, LINE_ACCEPTED}
  2. 距 journey.updated_at 超过 min_silent_days
  3. intimacy_score ≥ min_intimacy
  4. 最近 cooldown_days 没发过 reactivation_sent（避免骚扰）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from src.contacts.models import (
    STAGE_BONDED, STAGE_LINE_ACCEPTED, STAGE_LINE_ENGAGED,
)

logger = logging.getLogger(__name__)


@dataclass
class ReactivationCandidate:
    journey_id: str
    contact_id: str
    funnel_stage: str
    intimacy_score: float
    silent_days: float
    last_reactivation_ts: int           # 0 = 从未 ping 过


class ReactivationScheduler:
    def __init__(
        self,
        store,
        *,
        min_silent_days: float = 3.0,
        min_intimacy: float = 40.0,
        cooldown_days: float = 7.0,
        limit: int = 50,
    ) -> None:
        self._store = store
        self._min_silent_s = int(min_silent_days * 86400)
        self._min_intimacy = float(min_intimacy)
        self._cooldown_s = int(cooldown_days * 86400)
        self._limit = int(limit)

    def list_candidates(
        self, *, now: Optional[int] = None,
    ) -> List[ReactivationCandidate]:
        now = now if now is not None else int(time.time())
        cutoff_silent = now - self._min_silent_s
        active_stages = (STAGE_LINE_ENGAGED, STAGE_BONDED, STAGE_LINE_ACCEPTED)
        # 一次查出所有 eligible journey
        placeholders = ",".join("?" * len(active_stages))
        sql = (
            f"SELECT journey_id, contact_id, funnel_stage, intimacy_score, updated_at "
            f"FROM journeys "
            f"WHERE funnel_stage IN ({placeholders}) "
            f"  AND updated_at < ? "
            f"  AND intimacy_score >= ? "
            f"ORDER BY updated_at ASC "
            f"LIMIT ?"
        )
        with self._store._lock:  # noqa: SLF001
            rows = self._store._conn.execute(  # noqa: SLF001
                sql,
                (*active_stages, cutoff_silent, self._min_intimacy, self._limit),
            ).fetchall()

        out: List[ReactivationCandidate] = []
        cooldown_cutoff = now - self._cooldown_s
        for r in rows:
            last_react = self._last_reactivation_ts(r["journey_id"])
            if last_react and last_react >= cooldown_cutoff:
                continue    # 还在 cooldown 内，跳过
            silent_days = (now - r["updated_at"]) / 86400.0
            out.append(ReactivationCandidate(
                journey_id=r["journey_id"],
                contact_id=r["contact_id"],
                funnel_stage=r["funnel_stage"],
                intimacy_score=r["intimacy_score"],
                silent_days=round(silent_days, 2),
                last_reactivation_ts=last_react,
            ))
        return out

    def mark_sent(
        self, journey_id: str, *, note: str = "", trace_id: str = "",
    ) -> str:
        """runner 发出一条 reactivation 消息后调用。落事件。"""
        return self._store.append_event(
            journey_id=journey_id,
            event_type="reactivation_sent",
            payload={"note": note},
            trace_id=trace_id,
        )

    # ── 内部 ─────────────────────────────────────────────
    def _last_reactivation_ts(self, journey_id: str) -> int:
        """返回该 journey 上最近一条 reactivation_sent 事件的 ts；无则 0。"""
        with self._store._lock:  # noqa: SLF001
            row = self._store._conn.execute(  # noqa: SLF001
                "SELECT ts FROM journey_events "
                "WHERE journey_id=? AND event_type='reactivation_sent' "
                "ORDER BY ts DESC LIMIT 1",
                (journey_id,),
            ).fetchone()
        return int(row["ts"]) if row else 0
