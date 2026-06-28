"""译文在线置信度评测（确定性，零网络/零 LLM）。

口径：``translation_confidence`` 应把**好译文**评高分、**硬错**（空/未翻译/错语种）评低分，
从而支撑 ``EngineRouter`` 的置信度智能切换（主引擎低置信 → 自动换引擎择优）。

指标：以 ``threshold`` 二分（conf≥thr 判「可信」），算准确率；并报 good/bad 两组分数区间
（理想：好译文最低分 > 硬错最高分，即两组可分）。常驻门禁（纯函数）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import ConfidenceSample, load_confidence_samples


def evaluate_confidence(
    samples: Optional[List[ConfidenceSample]] = None,
    *,
    threshold: float = 0.5,
    acc_target: float = 0.9,
    require_separable: bool = True,
) -> Dict[str, Any]:
    """跑置信度评测；返回准确率 + good/bad 分数区间 + passed。

    passed = 准确率 ≥ acc_target（且 require_separable 时好译文最低分 > 硬错最高分）。
    """
    from src.ai.translation_confidence import translation_confidence

    rows = samples if samples is not None else load_confidence_samples()
    correct = 0
    good_scores: List[float] = []
    bad_scores: List[float] = []
    errors: List[Dict[str, Any]] = []
    for s in rows:
        conf = translation_confidence(s.source, s.translated, s.target_lang)
        pred_good = conf >= threshold
        if pred_good == s.good:
            correct += 1
        else:
            errors.append({"source": s.source, "translated": s.translated,
                           "target": s.target_lang, "conf": conf,
                           "good": s.good, "note": s.note})
        (good_scores if s.good else bad_scores).append(conf)

    n = len(rows)
    accuracy = round(correct / n, 3) if n else 0.0
    good_min = min(good_scores) if good_scores else 1.0
    bad_max = max(bad_scores) if bad_scores else 0.0
    separable = good_min > bad_max
    passed = accuracy >= acc_target and (separable or not require_separable)
    return {
        "summary": {
            "total": n,
            "accuracy": accuracy,
            "good_min": round(good_min, 3),
            "bad_max": round(bad_max, 3),
            "separable": separable,
        },
        "errors": errors,
        "threshold": threshold,
        "acc_target": acc_target,
        "passed": passed,
    }


def format_confidence_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 译文置信度评测报告 ===",
        f"样本: {m['total']}  准确率: {m['accuracy']:.0%}  "
        f"好译文最低分: {m['good_min']}  硬错最高分: {m['bad_max']}  "
        f"可分: {'是' if m['separable'] else '否'}  阈值: {report['threshold']}  "
        f"目标: 准确率≥{report['acc_target']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["errors"]:
        lines.append(f"误判 {len(report['errors'])} 例:")
        for e in report["errors"][:20]:
            lines.append(
                f"  - 「{e['source'][:14]}」→「{e['translated'][:18]}」({e['target']}) "
                f"conf={e['conf']} 期望好={e['good']}")
    return "\n".join(lines)


__all__ = ["evaluate_confidence", "format_confidence_report"]
