"""M1 — CSAT 对话满意度自动评分（Customer Satisfaction Score 模拟）。

基于规则的零延迟评分，无需 LLM，毫秒级计算。
数据来源：conversation_meta（I1）+ recent_decisions（draft_audit_log）。

评分维度（满分 5.0）：
  基线       4.0   大多数会话正常结束
  情绪信号   -1.5 ~ +0.8   末次情绪权重最大
  情绪趋势   -0.5 ~ +0.3   上升/下降/稳定
  风险等级   0 ~ -1.2      最终风险越高扣分越多
  消息数量   0 ~ -0.6      会话越长越难，适当扣分
  决策质量   -0.3/次       出现 force_override 说明流程有摩擦

与 InboxStore 集成（M1 架构选择）：
  - calculate_csat() 纯函数，无 DB 依赖，易测试
  - InboxStore.update_conv_meta() 调用后记录 csat_score
  - DraftService.resolve() 结束时触发 CSAT 更新
  - agent_perf 聚合时返回 avg_csat 字段
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── 情绪 → 评分调整量 ──────────────────────────────────────────────────────

_EMOTION_ADJ: Dict[str, float] = {
    "愤怒": -1.5, "暴怒": -1.5,
    "失望": -1.0, "沮丧": -0.8,
    "不满": -0.8, "投诉": -0.8,
    "焦虑": -0.5, "担忧": -0.3, "紧张": -0.3,
    "困惑": -0.2,
    "平稳": 0.0,  "中性": 0.0,
    "好奇": 0.1,
    "满意": +0.5, "开心": +0.5,
    "感谢": +0.8, "高兴": +0.6,
}

_TREND_ADJ: Dict[str, float] = {
    "rising":  +0.3,
    "stable":   0.0,
    "falling": -0.5,
}

_RISK_ADJ: Dict[str, float] = {
    "low":      0.0,
    "medium":  -0.3,
    "high":    -0.7,
    "critical": -1.2,
}

_BASE_SCORE: float = 4.0
_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 5.0


def calculate_csat(
    conv_meta: Optional[Dict[str, Any]],
    recent_decisions: Optional[List[Dict[str, Any]]] = None,
) -> float:
    """计算对话 CSAT 评分（0–5 分，保留 1 位小数）。

    Args:
        conv_meta:        来自 InboxStore.get_conv_meta() 的字典
        recent_decisions: 来自 draft_audit_log 的最近决策列表

    Returns:
        float 在 [0.0, 5.0] 区间
    """
    if not conv_meta:
        return _BASE_SCORE  # 无数据 → 默认中位

    score = _BASE_SCORE

    # 1. 末次情绪（最强信号）
    last_emotion = str(conv_meta.get("last_emotion") or "")
    for key, adj in _EMOTION_ADJ.items():
        if key in last_emotion:
            score += adj
            break

    # 2. 情绪趋势（反映会话演变方向）
    trend = str(conv_meta.get("emotion_trend") or "stable")
    score += _TREND_ADJ.get(trend, 0.0)

    # 3. 最终风险等级（高风险 = 摩擦 = 不满意）
    risk = str(conv_meta.get("last_risk") or "low")
    score += _RISK_ADJ.get(risk, 0.0)

    # 4. 消息数量（更长的会话通常意味着未快速解决）
    msg_count = int(conv_meta.get("msg_count") or 0)
    if msg_count > 20:
        score -= 0.6
    elif msg_count > 10:
        score -= 0.3

    # 5. 决策质量（force_override 意味着正常流程无法处理）
    if recent_decisions:
        for d in (recent_decisions or []):
            if d.get("action") == "force_override":
                score -= 0.3

    return round(max(_SCORE_MIN, min(_SCORE_MAX, score)), 1)


def csat_to_stars(score: float) -> str:
    """将 CSAT 分数转为星级展示字符串（用于报告 / UI）。"""
    n = int(round(score))
    n = max(0, min(5, n))
    return "⭐" * n + "☆" * (5 - n)


def csat_label(score: float) -> str:
    """将 CSAT 分数转为文字标签。"""
    if score >= 4.5:
        return "非常满意"
    if score >= 3.5:
        return "满意"
    if score >= 2.5:
        return "一般"
    if score >= 1.5:
        return "不满意"
    return "非常不满意"
