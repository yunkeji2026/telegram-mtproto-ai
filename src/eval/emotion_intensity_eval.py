"""情绪强度分级评测（N）——程度副词应让 intensity 单调可分。

`analyze_emotion` 对情绪词加程度副词缩放（「有点累」<「累」<「非常累/累死了」），
强度（→arousal/valence/记忆显著性 salience）随程度单调变化。本模块以三元组
（弱化 / 基准 / 强化）验证单调性：weak < base ≤ strong 且 weak < strong。

纯函数、离线可复现 → CI 常驻门禁。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import IntensityOrder, load_intensity_orders


def _intensity(text: str) -> float:
    from src.utils.emotional_context import analyze_emotion

    return float(analyze_emotion(text).get("primary_intensity", 0.0) or 0.0)


def evaluate_intensity_grading(
    orders: Optional[List[IntensityOrder]] = None,
) -> Dict[str, Any]:
    """跑强度分级；返回逐组明细 + 单调率 + passed（全部单调才 PASS）。"""
    rows = orders if orders is not None else load_intensity_orders()
    ok_count = 0
    results: List[Dict[str, Any]] = []
    for o in rows:
        iw, ib, is_ = _intensity(o.weak), _intensity(o.base), _intensity(o.strong)
        monotonic = (iw < ib <= is_) and (iw < is_)
        if monotonic:
            ok_count += 1
        results.append({"weak": o.weak, "base": o.base, "strong": o.strong,
                        "i_weak": round(iw, 3), "i_base": round(ib, 3),
                        "i_strong": round(is_, 3), "monotonic": monotonic,
                        "note": o.note})
    n = len(rows)
    rate = round(ok_count / n, 3) if n else 0.0
    return {
        "results": results,
        "summary": {"total": n, "monotonic_rate": rate},
        "passed": rate >= 1.0,
    }


def format_intensity_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 情绪强度分级报告（程度副词）===",
        f"组数: {m['total']}  单调率: {m['monotonic_rate']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    for r in report["results"]:
        flag = "" if r["monotonic"] else "  <<不单调"
        lines.append(
            f"  - {r['weak']}({r['i_weak']}) < {r['base']}({r['i_base']}) "
            f"<= {r['strong']}({r['i_strong']}){flag}")
    return "\n".join(lines)


__all__ = ["evaluate_intensity_grading", "format_intensity_report"]
