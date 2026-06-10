"""P34 — 对话质检评分引擎（QAScorer）。

纯规则评分，零 LLM 消耗，可在会话关闭/归档时同步调用。

评分维度（满分 100，加权平均）：
  response_speed   (30%) — 坐席首响时间（越快越高）
  resolution        (25%) — 是否以出站消息结束（问题关闭率代理指标）
  message_quality   (25%) — 出站消息平均长度（避免单字回复）
  risk_control      (20%) — 会话内检测到的风险信号数（越少越高）

最终分数存入 conversation_meta.qa_score（JSON）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional


# 响应时间评分映射（秒 → 分）
_SPEED_THRESHOLDS = [
    (60,   100),   # ≤ 1分钟：满分
    (180,   90),   # ≤ 3分钟
    (300,   80),   # ≤ 5分钟
    (600,   65),   # ≤ 10分钟
    (1800,  45),   # ≤ 30分钟
    (3600,  30),   # ≤ 60分钟
    (float("inf"), 15),  # > 1小时
]

# 维度权重（加权求和 = 100）
_WEIGHTS = {
    "response_speed":   0.30,
    "resolution":       0.25,
    "message_quality":  0.25,
    "risk_control":     0.20,
}

# 风险关键词（复用 P30 模式集，轻量内联版）
_RISK_KW = {
    "price_negotiation": ["便宜", "优惠", "打折", "折扣", "降价", "discount", "cheaper"],
    "complaint":         ["投诉", "举报", "太差", "垃圾", "不好", "complaint", "terrible", "awful"],
    "churn_intent":      ["退款", "退货", "取消", "不买了", "算了", "refund", "cancel", "quit"],
    "escalation_intent": ["报警", "律师", "起诉", "媒体", "曝光", "sue", "lawyer"],
    "urgency":           ["马上", "立刻", "赶紧", "asap", "urgent"],
}


class QAScorer:
    """P34：对话质检评分器。"""

    # ── 公开接口 ─────────────────────────────────────────────────────────────

    def score(
        self,
        messages: List[Dict[str, Any]],
        *,
        extra_risk_count: int = 0,
    ) -> Dict[str, Any]:
        """计算单次会话的质检评分。

        Args:
            messages: InboxStore 返回的消息列表（含 direction / text / ts 字段）
            extra_risk_count: 从外部（e.g. ai分析）传入的已知风险信号数

        Returns:
            {
                score: int,            # 0-100 综合分
                grade: str,            # A/B/C/D/F
                breakdown: {...},      # 各维度原始分
                avg_response_sec: int, # 平均响应秒数（-1=无法计算）
                message_count: int,
                outbound_count: int,
                risk_signal_count: int,
                computed_at: float,
            }
        """
        if not messages:
            return self._empty_result()

        msgs = sorted(messages, key=lambda m: float(m.get("ts") or 0))
        inbound = [m for m in msgs if m.get("direction") in ("in", "inbound")]
        outbound = [m for m in msgs if m.get("direction") in ("out", "outbound")]

        breakdown: Dict[str, int] = {}

        # ── 维度 1：响应速度 ─────────────────────────────────────────────────
        response_gaps: List[float] = self._compute_response_gaps(msgs)
        if response_gaps:
            avg_gap = sum(response_gaps) / len(response_gaps)
            speed_score = self._speed_to_score(avg_gap)
            avg_response_sec = int(avg_gap)
        else:
            speed_score = 50  # 无法判断，给中间分
            avg_response_sec = -1
        breakdown["response_speed"] = speed_score

        # ── 维度 2：解决率（末条为出站 = 已回复） ───────────────────────────
        last_msg = msgs[-1] if msgs else {}
        last_dir = last_msg.get("direction", "")
        if last_dir in ("out", "outbound"):
            resolution_score = 95
        elif not outbound:
            resolution_score = 20   # 没有任何出站消息（未被处理）
        else:
            resolution_score = 55   # 有出站但末条为入站（对话还在进行）
        breakdown["resolution"] = resolution_score

        # ── 维度 3：消息质量（出站消息平均字数） ────────────────────────────
        if outbound:
            avg_out_len = sum(len(str(m.get("text") or "")) for m in outbound) / len(outbound)
            quality_score = self._quality_to_score(avg_out_len)
        else:
            quality_score = 20
        breakdown["message_quality"] = quality_score

        # ── 维度 4：风险管控（低风险信号 = 高分） ───────────────────────────
        risk_count = extra_risk_count + self._count_risk_signals(msgs)
        if risk_count == 0:
            risk_score = 100
        elif risk_count == 1:
            risk_score = 75
        elif risk_count == 2:
            risk_score = 50
        else:
            risk_score = 25
        breakdown["risk_control"] = risk_score

        # ── 综合加权 ─────────────────────────────────────────────────────────
        total = sum(breakdown[k] * _WEIGHTS[k] for k in _WEIGHTS)
        total = max(0, min(100, round(total)))

        return {
            "score": total,
            "grade": self._score_to_grade(total),
            "breakdown": breakdown,
            "avg_response_sec": avg_response_sec,
            "message_count": len(msgs),
            "outbound_count": len(outbound),
            "risk_signal_count": risk_count,
            "computed_at": time.time(),
        }

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _compute_response_gaps(self, msgs: List[Dict[str, Any]]) -> List[float]:
        """计算每条出站消息距离其前一条入站消息的时间差（秒）。"""
        gaps: List[float] = []
        last_inbound_ts: Optional[float] = None
        for m in msgs:
            direction = m.get("direction", "")
            ts = float(m.get("ts") or 0)
            if direction in ("in", "inbound"):
                last_inbound_ts = ts
            elif direction in ("out", "outbound") and last_inbound_ts is not None:
                gap = max(0.0, ts - last_inbound_ts)
                if gap <= 86400:  # 超过 24h 的间隔不计（可能是新会话接续）
                    gaps.append(gap)
                last_inbound_ts = None  # 重置，避免同一入站消息被多次计算
        return gaps

    @staticmethod
    def _speed_to_score(avg_gap_sec: float) -> int:
        for threshold, score in _SPEED_THRESHOLDS:
            if avg_gap_sec <= threshold:
                return score
        return 15

    @staticmethod
    def _quality_to_score(avg_char_len: float) -> int:
        if avg_char_len >= 80:
            return 100
        elif avg_char_len >= 40:
            return 85
        elif avg_char_len >= 20:
            return 65
        elif avg_char_len >= 8:
            return 45
        return 25

    @staticmethod
    def _count_risk_signals(msgs: List[Dict[str, Any]]) -> int:
        """统计会话消息中出现的不同风险信号类型数。"""
        combined = " ".join(
            str(m.get("text") or "").lower()
            for m in msgs
            if m.get("direction") in ("in", "inbound")
        )
        return sum(
            1 for kws in _RISK_KW.values()
            if any(kw in combined for kw in kws)
        )

    @staticmethod
    def _score_to_grade(score: int) -> str:
        if score >= 90:
            return "A"
        elif score >= 75:
            return "B"
        elif score >= 60:
            return "C"
        elif score >= 45:
            return "D"
        return "F"

    @staticmethod
    def _empty_result() -> Dict[str, Any]:
        return {
            "score": 0,
            "grade": "N/A",
            "breakdown": {k: 0 for k in _WEIGHTS},
            "avg_response_sec": -1,
            "message_count": 0,
            "outbound_count": 0,
            "risk_signal_count": 0,
            "computed_at": time.time(),
        }


# ── 便捷函数 ──────────────────────────────────────────────────────────────────

def compute_qa_score(
    messages: List[Dict[str, Any]],
    extra_risk_count: int = 0,
) -> Dict[str, Any]:
    """模块级便捷函数，直接调用 QAScorer.score()。"""
    return QAScorer().score(messages, extra_risk_count=extra_risk_count)
