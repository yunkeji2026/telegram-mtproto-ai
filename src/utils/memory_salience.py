"""记忆显著性 + 时间衰减重排（REMT-lite）纯函数。

对标 2026 REMT（Realtime Editable Memory Topology）：记忆检索不应只看语义相似度，
而应让**情绪显著性**与**时间衰减**共同参与排序——情绪浓的、近期的记忆更该被想起。

本模块把该思想蒸馏为**确定性纯函数**（无 IO/不调 LLM/不触网）：
- ``salience_score(text)``：用 emotional_context.analyze_emotion 把一条事实的情绪浓度
  折成 0-1（|valence| + arousal + intensity 加权）；中性事实低分、情绪浓事实高分。
- ``recency_factor(created_at)``：指数半衰（默认 30 天半衰期）→ 越新越接近 1。
- ``blend_rank(base, salience, recency)``：在既有相关度 base 上做**温和加权**叠加，
  权重小（默认 salience 0.15 / recency 0.10），确保强相关记忆不被情绪/新鲜度盖过。

设计取舍：作为既有向量/关键词检索之上的**可选重排层**（默认关），开启前后非重排
路径行为完全不变；权重可配，便于 A/B 与回退。
"""

from __future__ import annotations

import math
import time
from typing import Optional

# 默认混合权重（base 相关度为主，显著性/新鲜度为辅）
DEFAULT_SALIENCE_WEIGHT = 0.15
DEFAULT_RECENCY_WEIGHT = 0.10
DEFAULT_HALF_LIFE_DAYS = 30.0

_DAY_SECONDS = 86400.0


def salience_score(text: str) -> float:
    """把一条记忆文本的情绪浓度折成 0-1。无情绪/空文本 → 接近 0。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    try:
        from src.utils.emotional_context import analyze_emotion
        emo = analyze_emotion(t)
    except Exception:
        return 0.0
    try:
        valence = abs(float(emo.get("valence", 0.0) or 0.0))
        arousal = float(emo.get("arousal", 0.0) or 0.0)
        intensity = float(emo.get("primary_intensity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    score = 0.5 * valence + 0.3 * arousal + 0.2 * intensity
    return max(0.0, min(1.0, score))


def recency_factor(
    created_at: Optional[float],
    now: Optional[float] = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """指数半衰的新鲜度 0-1（越新越接近 1）。created_at 缺失 → 0.5（中性）。"""
    if created_at in (None, 0, 0.0):
        return 0.5
    try:
        ca = float(created_at)
    except (TypeError, ValueError):
        return 0.5
    n = float(now) if now is not None else time.time()
    age_days = max(0.0, (n - ca) / _DAY_SECONDS)
    hl = float(half_life_days) if half_life_days and half_life_days > 0 else DEFAULT_HALF_LIFE_DAYS
    return float(2.0 ** (-age_days / hl))


def blend_rank(
    base: float,
    salience: float,
    recency: float,
    *,
    salience_weight: float = DEFAULT_SALIENCE_WEIGHT,
    recency_weight: float = DEFAULT_RECENCY_WEIGHT,
) -> float:
    """在相关度 base（0-1）上温和叠加显著性与新鲜度。返回排序用分值（可 >1）。"""
    try:
        b = float(base)
        s = float(salience)
        r = float(recency)
        sw = float(salience_weight)
        rw = float(recency_weight)
    except (TypeError, ValueError):
        return float(base) if isinstance(base, (int, float)) else 0.0
    return b + sw * s + rw * r


__all__ = [
    "DEFAULT_SALIENCE_WEIGHT",
    "DEFAULT_RECENCY_WEIGHT",
    "DEFAULT_HALF_LIFE_DAYS",
    "salience_score",
    "recency_factor",
    "blend_rank",
]
