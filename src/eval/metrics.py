"""评测指标（纯函数，无 IO）。"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


def multiclass_metrics(pairs: Iterable[Tuple[str, str]]) -> Dict[str, Any]:
    """多分类指标。

    入参 pairs：``(predicted_label, gold_label)`` 序列。
    返回：accuracy、macro_f1、per_label(precision/recall/f1/support)、
    confusion((gold,pred)->count)、total/correct。
    """
    rows: List[Tuple[str, str]] = [(str(p), str(g)) for p, g in pairs]
    total = len(rows)
    if total == 0:
        return {"total": 0, "correct": 0, "accuracy": 0.0,
                "macro_f1": 0.0, "per_label": {}, "confusion": {}}

    correct = sum(1 for p, g in rows if p == g)
    labels = sorted({g for _, g in rows} | {p for p, _ in rows})

    per_label: Dict[str, Dict[str, float]] = {}
    confusion: Dict[str, int] = {}
    for pred, gold in rows:
        confusion[f"{gold}->{pred}"] = confusion.get(f"{gold}->{pred}", 0) + 1

    f1s: List[float] = []
    for lab in labels:
        tp = sum(1 for p, g in rows if p == lab and g == lab)
        fp = sum(1 for p, g in rows if p == lab and g != lab)
        fn = sum(1 for p, g in rows if p != lab and g == lab)
        support = sum(1 for _, g in rows if g == lab)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        per_label[lab] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
        # 只把出现在 gold 里的标签计入 macro（避免预测器乱吐标签拉低/虚高）
        if support:
            f1s.append(f1)

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "per_label": per_label,
        "confusion": confusion,
    }


def resolve_rate(results: Iterable[bool]) -> Dict[str, Any]:
    """二分类解决率（用于 FAQ 自动解决率）。入参为「是否解决」布尔序列。"""
    rows = [bool(x) for x in results]
    total = len(rows)
    resolved = sum(1 for x in rows if x)
    return {
        "total": total,
        "resolved": resolved,
        "resolve_rate": round(resolved / total, 4) if total else 0.0,
    }
