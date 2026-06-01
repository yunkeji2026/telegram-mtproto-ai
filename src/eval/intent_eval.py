"""意图评测：用任意 predict_fn 跑标注数据集 → 结构化报告。"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .dataset import IntentSample, load_intent_samples
from .metrics import multiclass_metrics


def evaluate_intent(
    predict_fn: Callable[[str], str],
    samples: Optional[List[IntentSample]] = None,
    *,
    threshold: float = 0.85,
) -> Dict[str, Any]:
    """对 predict_fn 在样本集上评测。

    返回：metrics（见 multiclass_metrics）+ passed（accuracy>=threshold）+
    errors（误判明细，便于定位）。samples 为空则用内置种子集。
    """
    rows = samples if samples is not None else load_intent_samples()
    pairs: List[tuple] = []
    errors: List[Dict[str, str]] = []
    for s in rows:
        pred = str(predict_fn(s.text))
        pairs.append((pred, s.intent))
        if pred != s.intent:
            errors.append({"text": s.text, "expected": s.intent, "predicted": pred})

    metrics = multiclass_metrics(pairs)
    return {
        "metrics": metrics,
        "threshold": threshold,
        "passed": metrics["accuracy"] >= threshold,
        "errors": errors,
    }


def format_report(report: Dict[str, Any]) -> str:
    """把报告渲染为人读文本（CLI 用）。"""
    m = report["metrics"]
    lines = [
        "=== 意图评测报告 ===",
        f"样本数: {m['total']}  正确: {m['correct']}  "
        f"准确率: {m['accuracy']:.2%}  macro-F1: {m['macro_f1']:.4f}",
        f"阈值: {report['threshold']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
        "",
        "每标签 P/R/F1（support）:",
    ]
    for lab, v in sorted(m["per_label"].items()):
        if v["support"]:
            lines.append(
                f"  {lab:<10} P={v['precision']:.2f} R={v['recall']:.2f} "
                f"F1={v['f1']:.2f} (n={v['support']})"
            )
    if report["errors"]:
        lines.append("")
        lines.append(f"误判 {len(report['errors'])} 例:")
        for e in report["errors"][:20]:
            lines.append(f"  「{e['text'][:30]}」 期望={e['expected']} 实际={e['predicted']}")
    return "\n".join(lines)
