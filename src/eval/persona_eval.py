"""人设一致性评测（陪聊"真人感"的最后一道确定性防线）。

口径：LLM 偶尔漏出客服腔（"有什么可以帮您"）或自曝 AI 身份（"作为一个人工智能"），
一次就击穿情感陪聊的沉浸感。``persona_guard`` 在回复后做确定性体检。本模块量化它：
  - **召回**（最重要）：该抓的违规（客服腔 / AI 自曝）是否都抓到——漏一个=事故；
  - **精确/误伤**：合规陪聊（含"我才不是AI啦"这类否定句）不得被误判违规；
  - **sanitize 保内容**：剥离违规句后不得返回空串、且应保留干净句子。

纯函数、离线可复现 → CI 常驻门禁（与抽取/翻译/记忆门禁同构）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import PersonaSample, load_persona_samples


def _persona_of(sample: PersonaSample) -> Dict[str, Any]:
    """把样本的禁用配置组装成 persona dict（喂给 persona_guard）。"""
    return {
        "speaking": {"forbidden_phrases": list(sample.forbidden)},
        "identity": {"deny_ai": bool(sample.deny_ai)},
    }


def evaluate_persona_consistency(
    samples: Optional[List[PersonaSample]] = None,
    *,
    recall_target: float = 1.0,
    max_false_positive: int = 0,
) -> Dict[str, Any]:
    """评测守卫的违规召回 + 误伤；返回逐样本明细 + 指标 + passed。

    passed = 违规召回 ≥ recall_target **且** 误伤（合规被判违规）≤ max_false_positive。
    （安全侧默认 recall_target=1.0：种子违规必须全抓；误伤默认零容忍。）
    """
    from src.utils.persona_guard import find_violations, sanitize

    rows = samples if samples is not None else load_persona_samples()
    tp = fp = fn = tn = 0
    missed: List[Dict[str, Any]] = []        # 漏抓的违规
    false_hits: List[Dict[str, Any]] = []    # 误伤的合规
    sanitize_bugs: List[Dict[str, Any]] = []
    for s in rows:
        persona = _persona_of(s)
        violations = find_violations(s.reply, persona)
        flagged = bool(violations)
        if s.expect_violation:
            if flagged:
                tp += 1
                # 违规样本：sanitize 不得返回空串
                cleaned, _ = sanitize(s.reply, persona)
                if not (cleaned or "").strip():
                    sanitize_bugs.append({"reply": s.reply, "issue": "sanitize 空串"})
            else:
                fn += 1
                missed.append({"reply": s.reply, "note": s.note})
        else:
            if flagged:
                fp += 1
                false_hits.append({"reply": s.reply, "violations": violations, "note": s.note})
            else:
                tn += 1

    pos = tp + fn
    recall = round(tp / pos, 3) if pos else 1.0
    precision = round(tp / (tp + fp), 3) if (tp + fp) else 1.0
    return {
        "summary": {
            "total": len(rows),
            "violations_expected": pos,
            "violations_caught": tp,
            "recall": recall,
            "precision": precision,
            "false_positives": fp,
            "sanitize_bugs": len(sanitize_bugs),
        },
        "missed": missed,
        "false_hits": false_hits,
        "sanitize_bugs": sanitize_bugs,
        "recall_target": recall_target,
        "max_false_positive": max_false_positive,
        "passed": (recall >= recall_target
                   and fp <= max_false_positive
                   and not sanitize_bugs),
    }


def format_persona_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 人设一致性报告 ===",
        f"样本: {m['total']}  违规召回: {m['recall']:.0%} "
        f"({m['violations_caught']}/{m['violations_expected']})  "
        f"精确: {m['precision']:.0%}  误伤: {m['false_positives']}  "
        f"sanitize异常: {m['sanitize_bugs']}  "
        f"目标: 召回≥{report['recall_target']:.0%}/误伤≤{report['max_false_positive']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["missed"]:
        lines.append(f"漏抓违规 {len(report['missed'])} 例:")
        for r in report["missed"][:20]:
            lines.append(f"  - {r['reply']}  ({r['note']})")
    if report["false_hits"]:
        lines.append(f"误伤合规 {len(report['false_hits'])} 例:")
        for r in report["false_hits"][:20]:
            lines.append(f"  - {r['reply']}  命中={r['violations']}")
    return "\n".join(lines)


__all__ = ["evaluate_persona_consistency", "format_persona_report"]
