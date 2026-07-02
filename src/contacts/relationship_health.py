"""Phase P1：单人关系健康度打分（纯函数）。

把分散的关系信号（亲密度 / 真实沉默天数 / 趋势 / 互动对称性 / 漏斗阶段 / 已排关怀）
融成**一张可执行的单人健康卡**：score 0-100 + grade + risk_level + 人话原因 + 建议动作。

与全域 `/api/relations/digest` 的区别：digest 是**全盘聚合**（活跃率/漏斗深度比），
本模块是**逐联系人**打分 + 排序流失风险榜 + 给运营「该对谁、做什么」。

设计纪律（与 intimacy_engine / care_commitment 一致）：
- **纯函数**：入参是 `ContactHealthSignals`（上层从 store/IntimacyEngine 取齐），不触 DB/网络、可单测。
- **沉默是流失第一信号**：churn 主要由「相对关系强度的沉默」驱动——高亲密关系沉默比低亲密更危险。
  故 recency 权重最高（0.40），且单列 `value_at_risk`（高亲密 + 长沉默）供排序置顶。
- grade 带沿用 digest（A≥80/B≥65/C≥45/D），全产品一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# 健康分维度权重（和为 1.0）
_W_RECENCY = 0.40      # 真实沉默天数（流失第一信号）
_W_INTIMACY = 0.25     # 关系强度
_W_TREND = 0.20        # 亲密度环比方向
_W_MUTUALITY = 0.15    # 对话对称性（单向唱独角戏 = 风险）

# 「高价值」阈值：达到此亲密度即视为值得重点维护的关系
_VALUE_INTIMACY = 50.0
_VALUE_SILENT_DAYS = 7.0   # 高价值关系沉默超此天数 → value_at_risk

# 漏斗中代表「已建立关系」的阶段（沉默更值得警觉）——单一来源见 models.FUNNEL_DONE_STAGES（P5-2c）
from src.contacts.models import FUNNEL_DONE_STAGES as _BONDED_STAGES


@dataclass
class ContactHealthSignals:
    """单人健康打分输入（上层填齐；缺失项用合理中性默认）。"""
    intimacy_score: float = 0.0                 # 当前亲密度 0-100
    days_since_last_msg: float = float("inf")   # 真实沉默天数（来自事件流；inf=无消息）
    prev_intimacy_score: Optional[float] = None  # 约 7 天前亲密度（趋势用，None=无基准）
    funnel_stage: str = "INITIAL"
    turn_count_in: int = 0                       # 对方发来轮次
    turn_count_out: int = 0                      # 我方发出轮次
    pending_care: int = 0                        # 已排但未发的关怀待办数
    has_recent_reactivation: bool = False        # 近期是否已唤醒过（cooldown 内）


@dataclass
class HealthCard:
    score: float                  # 0-100，越高越健康（流失风险越低）
    grade: str                    # A/B/C/D
    risk_level: str               # healthy | watch | at_risk | critical
    value_at_risk: bool           # 高价值关系正在流失（排序置顶）
    action: str                   # 建议动作 code
    action_hint: str              # 建议动作人话
    reasons: List[str] = field(default_factory=list)
    components: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "score": self.score, "grade": self.grade, "risk_level": self.risk_level,
            "value_at_risk": self.value_at_risk, "action": self.action,
            "action_hint": self.action_hint, "reasons": list(self.reasons),
            "components": dict(self.components),
        }


def _recency_score(days: float) -> float:
    """沉默天数 → 0..1（今天≈1，越久越低）。分段确定性，不依赖浮点拟合。"""
    if days == float("inf"):
        return 0.0
    if days <= 1:
        return 1.0
    if days <= 3:
        return 0.85
    if days <= 7:
        return 0.6
    if days <= 14:
        return 0.35
    if days <= 30:
        return 0.15
    return 0.05


def _trend_score(cur: float, prev: Optional[float]) -> tuple:
    """亲密度环比 → (0..1, delta)。无基准 → 中性 0.6。"""
    if prev is None:
        return 0.6, None
    delta = cur - prev
    if delta >= 5:
        return 1.0, delta
    if delta >= 0:
        return 0.7, delta
    if delta >= -5:
        return 0.5, delta
    if delta >= -15:
        return 0.3, delta
    return 0.1, delta


def _mutuality_score(tin: int, tout: int) -> float:
    """对话对称性：min/max。无数据 → 中性 0.6。单向（一方为 0）→ 低。"""
    if tin <= 0 and tout <= 0:
        return 0.6
    hi = max(tin, tout)
    lo = min(tin, tout)
    if hi <= 0:
        return 0.6
    return lo / hi


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def score_contact_health(sig: ContactHealthSignals) -> HealthCard:
    """把一组关系信号融成单人健康卡（纯函数，确定性）。"""
    recency = _recency_score(sig.days_since_last_msg)
    intimacy_norm = max(0.0, min(1.0, sig.intimacy_score / 100.0))
    trend, delta = _trend_score(sig.intimacy_score, sig.prev_intimacy_score)
    mutuality = _mutuality_score(sig.turn_count_in, sig.turn_count_out)

    health = (
        recency * _W_RECENCY
        + intimacy_norm * _W_INTIMACY
        + trend * _W_TREND
        + mutuality * _W_MUTUALITY
    ) * 100.0
    health = round(min(100.0, max(0.0, health)), 1)

    if health >= 70:
        risk = "healthy"
    elif health >= 50:
        risk = "watch"
    elif health >= 30:
        risk = "at_risk"
    else:
        risk = "critical"

    value_at_risk = (
        sig.intimacy_score >= _VALUE_INTIMACY
        and sig.days_since_last_msg >= _VALUE_SILENT_DAYS
        and sig.days_since_last_msg != float("inf")
    ) or (
        sig.funnel_stage in _BONDED_STAGES
        and sig.days_since_last_msg >= _VALUE_SILENT_DAYS
    )

    reasons: List[str] = []
    d = sig.days_since_last_msg
    if d == float("inf"):
        reasons.append("从无消息往来")
    elif d >= 30:
        reasons.append(f"已沉默 {int(d)} 天（严重）")
    elif d >= 7:
        reasons.append(f"已沉默 {int(d)} 天")
    elif d >= 3:
        reasons.append(f"{int(d)} 天未联系")
    if delta is not None and delta <= -5:
        reasons.append(f"亲密度下滑 {abs(round(delta, 1))} 分")
    if mutuality < 0.4 and (sig.turn_count_in + sig.turn_count_out) > 0:
        reasons.append("对话偏单向（唱独角戏）")
    if value_at_risk:
        reasons.append("高价值关系，正在流失")
    if sig.pending_care > 0:
        reasons.append(f"已排 {sig.pending_care} 条关怀待发")
    if not reasons:
        reasons.append("关系活跃健康")

    # 建议动作。关键：`value_at_risk`（高价值关系正在流失）即便健康分被历史亲密度/对称性
    # 撑在 watch，也应进入「需干预」分支——这正是最该抢救的一类，不能被均值掩盖。
    needs_intervention = value_at_risk or risk in ("at_risk", "critical")
    if needs_intervention and sig.pending_care > 0:
        action, hint = "care_pending", "已排主动关怀，待到点发出"
    elif needs_intervention and sig.intimacy_score >= 40 \
            and not sig.has_recent_reactivation:
        action, hint = "reactivate", "建议主动唤醒（引用旧事重启对话）"
    elif needs_intervention:
        action, hint = "schedule_care", "建议安排一次主动关怀"
    elif risk == "watch" and (delta is not None and delta < 0):
        action, hint = "deepen", "关系降温，建议主动加温互动"
    elif sig.funnel_stage in _BONDED_STAGES and sig.intimacy_score >= 70:
        action, hint = "maintain", "关系稳固，保持节奏即可"
    else:
        action, hint = "none", "无需干预"

    return HealthCard(
        score=health, grade=_grade(health), risk_level=risk,
        value_at_risk=bool(value_at_risk), action=action, action_hint=hint,
        reasons=reasons,
        components={
            "recency": round(recency, 3),
            "intimacy_norm": round(intimacy_norm, 3),
            "trend": round(trend, 3),
            "mutuality": round(mutuality, 3),
            "intimacy_delta": (round(delta, 1) if delta is not None else None),
            "days_since_last_msg": (None if d == float("inf") else round(d, 1)),
        },
    )


__all__ = ["ContactHealthSignals", "HealthCard", "score_contact_health"]
