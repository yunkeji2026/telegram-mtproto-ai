"""情绪识别质量评测（陪伴回复"接得对不对"的前置判断）。

两支：
  1) **情绪维度准确率**：``analyze_emotion`` 把消息归到 positive/negative/low_energy/
     curious/neutral。错判会让回复温度不匹配（对方难过你却嘻嘻哈哈）。多分类准确率 +
     每维 P/R/F1（复用 ``metrics.multiclass_metrics``）。
  2) **危机识别召回**（安全红线）：``detect_crisis`` 把消息分 none/elevated/severe。
     severe（自伤/轻生）**必须全召回**（漏一个=安全事故）；同时守"日常惯用语"
     （累死了/笑死了）不被误升级（误报率）。

均为确定性纯函数 → CI 常驻门禁。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import (
    CrisisSample, EmotionSample, load_crisis_samples, load_emotion_samples,
)
from .metrics import multiclass_metrics


def emotion_dimension_predictor():
    """返回 text → 情绪维度 的预测器（包 analyze_emotion）。"""
    from src.utils.emotional_context import analyze_emotion

    def _predict(text: str) -> str:
        return str(analyze_emotion(text).get("dimension", "neutral"))

    return _predict


def evaluate_emotion_dimension(
    samples: Optional[List[EmotionSample]] = None,
    *,
    threshold: float = 0.8,
) -> Dict[str, Any]:
    """情绪维度多分类评测；accuracy ≥ threshold 即 PASS。"""
    rows = samples if samples is not None else load_emotion_samples()
    predict = emotion_dimension_predictor()
    pairs: List[tuple] = []
    errors: List[Dict[str, str]] = []
    for s in rows:
        pred = predict(s.text)
        pairs.append((pred, s.dimension))
        if pred != s.dimension:
            errors.append({"text": s.text, "expected": s.dimension, "predicted": pred})
    metrics = multiclass_metrics(pairs)
    return {
        "metrics": metrics,
        "threshold": threshold,
        "passed": metrics["accuracy"] >= threshold,
        "errors": errors,
    }


def format_emotion_report(report: Dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "=== 情绪维度评测报告 ===",
        f"样本: {m['total']}  正确: {m['correct']}  准确率: {m['accuracy']:.2%}  "
        f"macro-F1: {m['macro_f1']:.3f}  阈值: {report['threshold']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    for lab, v in sorted(m["per_label"].items()):
        if v["support"]:
            lines.append(
                f"  {lab:<11} P={v['precision']:.2f} R={v['recall']:.2f} "
                f"F1={v['f1']:.2f} (n={v['support']})")
    if report["errors"]:
        lines.append(f"误判 {len(report['errors'])} 例:")
        for e in report["errors"][:20]:
            lines.append(f"  「{e['text'][:24]}」 期望={e['expected']} 实际={e['predicted']}")
    return "\n".join(lines)


def evaluate_crisis_detection(
    samples: Optional[List[CrisisSample]] = None,
    *,
    severe_recall_target: float = 1.0,
    max_false_alarm: int = 0,
) -> Dict[str, Any]:
    """危机识别评测：severe 召回（安全红线）+ none 误报。

    passed = severe 召回 ≥ severe_recall_target **且** 误报（none 被升级）≤ max_false_alarm。
    """
    from src.utils.wellbeing_guard import detect_crisis

    rows = samples if samples is not None else load_crisis_samples()
    severe_total = severe_caught = 0
    elevated_total = elevated_caught = 0
    false_alarm = 0
    missed: List[Dict[str, Any]] = []
    alarms: List[Dict[str, Any]] = []
    pairs: List[tuple] = []
    for s in rows:
        got = str(detect_crisis(s.text).get("level", "none"))
        gold = s.level
        pairs.append((got, gold))
        if gold == "severe":
            severe_total += 1
            if got == "severe":
                severe_caught += 1
            else:
                missed.append({"text": s.text, "got": got, "note": s.note})
        elif gold == "elevated":
            elevated_total += 1
            if got in ("elevated", "severe"):
                elevated_caught += 1
            else:
                missed.append({"text": s.text, "got": got, "note": s.note})
        else:  # gold == none
            if got != "none":
                false_alarm += 1
                alarms.append({"text": s.text, "got": got, "note": s.note})

    severe_recall = round(severe_caught / severe_total, 3) if severe_total else 1.0
    elevated_recall = round(elevated_caught / elevated_total, 3) if elevated_total else 1.0
    return {
        "metrics": multiclass_metrics(pairs),
        "summary": {
            "total": len(rows),
            "severe_total": severe_total,
            "severe_recall": severe_recall,
            "elevated_recall": elevated_recall,
            "false_alarm": false_alarm,
        },
        "missed": missed,
        "false_alarms": alarms,
        "severe_recall_target": severe_recall_target,
        "max_false_alarm": max_false_alarm,
        "passed": severe_recall >= severe_recall_target and false_alarm <= max_false_alarm,
    }


def format_crisis_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 危机识别报告（安全红线）===",
        f"样本: {m['total']}  severe召回: {m['severe_recall']:.0%} "
        f"(n={m['severe_total']})  elevated召回: {m['elevated_recall']:.0%}  "
        f"误报: {m['false_alarm']}  "
        f"目标: severe召回≥{report['severe_recall_target']:.0%}/误报≤{report['max_false_alarm']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["missed"]:
        lines.append(f"漏判危机 {len(report['missed'])} 例（!!安全风险!!）:")
        for r in report["missed"][:20]:
            lines.append(f"  - {r['text']}  实判={r['got']}")
    if report["false_alarms"]:
        lines.append(f"误报 {len(report['false_alarms'])} 例:")
        for r in report["false_alarms"][:20]:
            lines.append(f"  - {r['text']}  实判={r['got']}")
    return "\n".join(lines)


__all__ = [
    "emotion_dimension_predictor", "evaluate_emotion_dimension",
    "format_emotion_report", "evaluate_crisis_detection", "format_crisis_report",
]
