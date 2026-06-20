"""M9+ 知识库缺口飞轮 · 统一优先级排序（纯函数，零 IO，便于单测）。

现状：``KnowledgeStore.get_auto_suggestions`` 已能综合 miss_log / 弱命中 / 过载条目
产出建议，但优先级只有 high/medium 文字标签、来源分散。运营拿到一堆建议仍不知
**先做哪条**。

本模块把这些异构建议折算成**单一数值优先级**并排成一条可执行待办：
score = 来源权重 × (1 + 频次因子)。来源越「彻底答不上」（miss）权重越高。
纯函数：输入 get_auto_suggestions 的输出，不碰 DB。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

# 来源权重：miss（完全无答案）最痛 > weak（答了但不准）> overloaded（条目太泛需拆）。
_SOURCE_WEIGHT = {
    "miss": 3.0,
    "weak": 2.0,
    "weak_hit": 2.0,
    "overloaded": 1.0,
}
_DEFAULT_WEIGHT = 1.5


def _count_of(item: Dict[str, Any]) -> int:
    for k in ("count", "cnt", "frequency", "hits"):
        v = item.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 1


def gap_priority_score(item: Dict[str, Any]) -> float:
    """单条建议的数值优先级：来源权重 × (1 + ln(1+频次))，保留 2 位。

    频次用对数压缩，避免极高频单条碾压所有其它缺口（长尾也要被看见）。
    """
    source = str(item.get("source") or "").strip().lower()
    weight = _SOURCE_WEIGHT.get(source, _DEFAULT_WEIGHT)
    count = max(0, _count_of(item))
    return round(weight * (1.0 + math.log1p(count)), 2)


def _tier(score: float) -> str:
    if score >= 6.0:
        return "high"
    if score >= 3.5:
        return "medium"
    return "low"


def rank_kb_gaps(
    suggestions: List[Dict[str, Any]],
    *,
    top_k: int = 20,
) -> List[Dict[str, Any]]:
    """把异构建议折算成统一优先级并降序排成待办（纯函数）。

    每条补 ``priority_score``（数值）与 ``priority_tier``（high/medium/low），
    按分数降序、同分按频次降序，截断 top_k。原字段保留。
    """
    ranked: List[Dict[str, Any]] = []
    for s in suggestions or []:
        if not isinstance(s, dict):
            continue
        score = gap_priority_score(s)
        item = dict(s)
        item["priority_score"] = score
        item["priority_tier"] = _tier(score)
        ranked.append(item)
    ranked.sort(key=lambda x: (x["priority_score"], _count_of(x)), reverse=True)
    return ranked[: max(1, int(top_k))]


def gap_backlog_summary(ranked: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对排好序的待办做一句话汇总（看板/周报用）。"""
    tiers = {"high": 0, "medium": 0, "low": 0}
    for r in ranked or []:
        tiers[r.get("priority_tier", "low")] = tiers.get(r.get("priority_tier", "low"), 0) + 1
    total = len(ranked or [])
    top = ranked[0] if ranked else None
    return {
        "total": total,
        "tiers": tiers,
        "top_query": (top or {}).get("query", "") if top else "",
        "top_score": (top or {}).get("priority_score", 0.0) if top else 0.0,
    }
