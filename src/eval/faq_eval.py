"""FAQ 自动解决率评测。

「自动解决」定义（可换）：KB 检索对该问题返回至少一条条目，且最高分 ≥ 阈值
→ 视为系统能自助回答、无需人工。阈值需按各 KB 校准（BM25 分数未归一化）。

设计与意图评测一致：核心吃任意 ``resolve_fn(question) -> bool``，
``kb_search_resolver`` 只是首个适配；单测可注入 fake KB，不依赖真实知识库。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .dataset import FaqSample, load_faq_samples
from .metrics import resolve_rate


def kb_search_resolver(
    kb_store: Any, *, score_threshold: float = 1.0, lang: str = "zh",
) -> Callable[[str], bool]:
    """基于 KB 检索的解决判定器：top 条目分数 ≥ 阈值即视为可自动解决。

    kb_store 需有 ``search(query, top_k, lang) -> {"entries": [{"_score": ...}]}``。
    检索异常 → 视为未解决（保守）。
    """
    def _resolve(question: str) -> bool:
        q = str(question or "").strip()
        if not q:
            return False
        try:
            res = kb_store.search(q, top_k=3, lang=lang) or {}
        except Exception:
            return False
        entries = res.get("entries") or []
        if not entries:
            return False
        try:
            top = float(entries[0].get("_score") or 0.0)
        except (TypeError, ValueError):
            top = 0.0
        return top >= float(score_threshold)

    return _resolve


def evaluate_faq(
    resolve_fn: Callable[[str], bool],
    samples: Optional[List[FaqSample]] = None,
    *,
    threshold: float = 0.50,
) -> Dict[str, Any]:
    """评测 FAQ 自动解决率。

    threshold：解决率 PASS 阈值（蓝图目标 50%）。
    返回：rate 指标 + passed + unresolved（未解决问题清单，便于补 KB）。
    """
    rows = samples if samples is not None else load_faq_samples()
    flags: List[bool] = []
    unresolved: List[str] = []
    for s in rows:
        ok = bool(resolve_fn(s.question))
        flags.append(ok)
        if not ok:
            unresolved.append(s.question)
    metrics = resolve_rate(flags)
    return {
        "metrics": metrics,
        "threshold": threshold,
        "passed": metrics["resolve_rate"] >= threshold,
        "unresolved": unresolved,
    }


def format_faq_report(report: Dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "=== FAQ 自动解决率报告 ===",
        f"样本数: {m['total']}  解决: {m['resolved']}  "
        f"解决率: {m['resolve_rate']:.2%}  阈值: {report['threshold']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["unresolved"]:
        lines.append("")
        lines.append(f"未解决 {len(report['unresolved'])} 例（建议补 KB）:")
        for q in report["unresolved"][:20]:
            lines.append(f"  - {q}")
    return "\n".join(lines)
