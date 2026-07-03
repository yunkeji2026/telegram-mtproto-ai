"""Journey 状态机：合法转移表 + guard + 时间驱动降级。

从 Gateway 抽出单独文件的好处：
  1. Gateway 可以继续保持"业务门面"职责，FSM 变更不会扩散
  2. 时间驱动降级（沉默 7 天退半格）需要单独的扫描循环——独立文件便于组织
  3. 合法转移表由 config 调整时，不需动 Gateway 代码
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    STAGE_BONDED,
    STAGE_ENGAGED,
    STAGE_HANDOFF_READY,
    STAGE_HANDOFF_SENT,
    STAGE_INITIAL,
    STAGE_LINE_ADDED,
    STAGE_LINE_ACCEPTED,
    STAGE_LINE_ENGAGED,
    STAGE_LOST_HANDOFF,
    STAGE_LOST_LINE_SILENT,
)

logger = logging.getLogger(__name__)


# ── 合法前向转移 ────────────────────────────────────────
# 值为允许的前驱集合；key 不在此表则视为"任何前驱都允许"。
STAGE_TRANSITIONS: Dict[str, Set[str]] = {
    STAGE_ENGAGED: {STAGE_INITIAL, STAGE_ENGAGED},
    STAGE_HANDOFF_READY: {STAGE_ENGAGED, "WARMING", STAGE_HANDOFF_READY},
    STAGE_HANDOFF_SENT: {STAGE_HANDOFF_READY, STAGE_HANDOFF_SENT},
    STAGE_LINE_ADDED: {STAGE_HANDOFF_SENT, STAGE_LINE_ADDED},
    STAGE_LINE_ACCEPTED: {STAGE_HANDOFF_SENT, STAGE_LINE_ADDED, STAGE_LINE_ACCEPTED},
    STAGE_LINE_ENGAGED: {STAGE_LINE_ACCEPTED, STAGE_LINE_ENGAGED},
    STAGE_BONDED: {STAGE_LINE_ENGAGED, STAGE_BONDED},
    # LOST_HANDOFF / LOST_LINE_SILENT 是失败分支，可从任何相应状态进入——不限制前驱
}


# ── 时间驱动降级（静默超时）规则 ──────────────────────────
# key 是当前 stage；value 是 (silent_seconds_threshold, target_stage)
# 规则：若 Journey.updated_at 距 now 超过 threshold 且仍停留在 key stage，退到 target。
# 注意：target 可以不在 STAGE_TRANSITIONS 表里（作为降级是合法的）。
SILENCE_DECAY_RULES: Dict[str, Tuple[int, str]] = {
    STAGE_HANDOFF_SENT: (72 * 3600, STAGE_LOST_HANDOFF),         # 3 天没加 LINE
    STAGE_LINE_ADDED: (24 * 3600, STAGE_LOST_LINE_SILENT),        # 加了好友但 24h 没回
    STAGE_LINE_ACCEPTED: (24 * 3600, STAGE_LOST_LINE_SILENT),     # 通过了好友但 24h 没回
    STAGE_HANDOFF_READY: (7 * 24 * 3600, STAGE_ENGAGED),          # 7 天仍未引流 → 退回 engage
    # 注意：Journey 在活跃交互时 updated_at 会被刷新，降级只针对"真正沉默"
}


def is_transition_allowed(from_stage: str, to_stage: str) -> bool:
    """纯函数：判断转移是否合法（不读 DB）。"""
    if from_stage == to_stage:
        return True
    allowed = STAGE_TRANSITIONS.get(to_stage)
    if allowed is None:
        return True          # 未定义 → 默认允许（比如失败分支）
    return from_stage in allowed


# ── Gateway 对外暴露的 guard + 实际写库 ───────────────────
def transit(
    store,
    *,
    journey_id: str,
    to_stage: str,
    trace_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """guarded transition：只在允许的前驱里改 + 落 event。

    不允许的转移 silent 拒绝 + debug 日志，避免脏 stage 写入 DB。
    """
    j = store.get_journey(journey_id)
    if not j:
        return False
    if j.funnel_stage == to_stage:
        return True
    if not is_transition_allowed(j.funnel_stage, to_stage):
        logger.debug(
            "stage transition blocked: journey=%s %s -> %s",
            journey_id, j.funnel_stage, to_stage,
        )
        return False
    store.update_journey(journey_id, funnel_stage=to_stage)
    store.append_event(
        journey_id=journey_id,
        event_type="stage_change",
        payload={"from": j.funnel_stage, "to": to_stage, **(payload or {})},
        trace_id=trace_id,
    )
    return True


# ── 时间驱动扫描 ──────────────────────────────────────
def list_journeys_eligible_for_decay(
    store, *, now: Optional[int] = None,
) -> List[Tuple[str, str, str, int]]:
    """扫描 journeys 表，找出应降级的 journey。

    返回 [(journey_id, current_stage, target_stage, silent_seconds), ...]
    以便 apply_silence_decay 或告警逻辑使用。
    """
    now = now if now is not None else int(time.time())
    out: List[Tuple[str, str, str, int]] = []
    # 复用 store 的 _lock/_conn 通过其公有接口：没法一行 SQL，但可分 stage 扫
    for stage, (threshold, target) in SILENCE_DECAY_RULES.items():
        with store._lock:  # noqa: SLF001 — fsm 和 store 是同一个包意义上的内部协作
            rows = store._conn.execute(  # noqa: SLF001
                "SELECT journey_id, funnel_stage, updated_at FROM journeys "
                "WHERE funnel_stage=? AND updated_at < ?",
                (stage, now - threshold),
            ).fetchall()
        for r in rows:
            out.append((r["journey_id"], r["funnel_stage"], target, now - r["updated_at"]))
    return out


def apply_silence_decay(store, *, now: Optional[int] = None, dry_run: bool = False) -> int:
    """对所有符合条件的 journey 执行降级；返回动作数。

    每降级一个 journey：
      - 更新 funnel_stage 为 target
      - 落 `silence_decay` 事件
      - 不触 updated_at（这是降级本身，不是新活动）

    ★ W3-D2.4 GAP（已闭环，2026-06）：本函数只动 funnel_stage，**不碰 intimacy_score**。
    intimacy 衰减走另一条线，两半已都落地：
      - 选项 B（live 读时衰减，已实现）：``IntimacyEngine.compute_intimacy`` 超 7 天
        grace 后按每周 ×0.95 衰减。即时重算的消费者（handoff_readiness / 趋势 API）已对。
      - 物化（stored 列回写，已实现）：``IntimacyEngine.refresh_stale_journeys`` +
        bootstrap ``_intimacy_refresh_loop``（gated by ``intimacy_refresh_interval_minutes``，
        默认关）周期性把 live 衰减写回 ``journeys.intimacy_score``，修「沉默 journey 无
        msg_in → stored 列冻结高分 → reactivation 反复捞死号」。
    本函数维持「只降 stage」职责不变；intimacy 衰减不在此处耦合。
    """
    now = now if now is not None else int(time.time())
    eligible = list_journeys_eligible_for_decay(store, now=now)
    count = 0
    for jid, from_stage, to_stage, silent_s in eligible:
        if dry_run:
            logger.info("silence_decay (dry_run): %s %s->%s silent=%ds",
                        jid, from_stage, to_stage, silent_s)
            count += 1
            continue
        # 直接更新——这里 guard 允许，因为 target 在 LOST_* 或 ENGAGED，不设 STAGE_TRANSITIONS 约束
        with store._lock:  # noqa: SLF001
            # 只改 stage，不动 updated_at（避免被认为又活跃了）
            store._conn.execute(  # noqa: SLF001
                "UPDATE journeys SET funnel_stage=? WHERE journey_id=? AND funnel_stage=?",
                (to_stage, jid, from_stage),
            )
            store._conn.commit()
        store.append_event(
            journey_id=jid,
            event_type="silence_decay",
            payload={
                "from": from_stage, "to": to_stage,
                "silent_seconds": silent_s,
                "threshold_seconds": SILENCE_DECAY_RULES[from_stage][0],
            },
            trace_id="",
        )
        count += 1
        logger.info("silence_decay applied: %s %s->%s silent=%ds",
                    jid, from_stage, to_stage, silent_s)
    return count
