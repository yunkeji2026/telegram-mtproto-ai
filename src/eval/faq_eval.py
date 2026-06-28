"""FAQ 自动解决率评测。

「自动解决」定义（可换）：KB 检索对该问题返回至少一条条目，且最高分 ≥ 阈值
→ 视为系统能自助回答、无需人工。阈值需按各 KB 校准（BM25 分数未归一化）。

设计与意图评测一致：核心吃任意 ``resolve_fn(question) -> bool``，
``kb_search_resolver`` 只是首个适配；单测可注入 fake KB，不依赖真实知识库。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .dataset import FaqSample, load_faq_samples
from .metrics import resolve_rate

# 默认 KB sqlite 候选路径（与 scripts/run_eval 一致；可被 AITR_KB_DB / 入参覆盖）
_DEFAULT_KB_PATHS: Tuple[str, ...] = (
    "config/knowledge_base.db", "data/knowledge_base.db",
)


def locate_kb_db(kb_db: str = "") -> Optional[str]:
    """定位可用的 KB sqlite：入参 > 环境 ``AITR_KB_DB`` > 默认候选。找不到返回 None。"""
    cands = [kb_db, os.environ.get("AITR_KB_DB", ""), *(_DEFAULT_KB_PATHS)]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def build_kb_resolver(
    kb_db: str = "", *, score_threshold: float = 1.0, lang: str = "zh",
) -> Tuple[Optional[Callable[[str], bool]], Any]:
    """定位 KB 并构造「解决判定器」。

    返回 ``(resolver, store)``；KB 不存在/构造失败返回 ``(None, None)``——
    供 CLI 与 CI 门禁共用同一套 KB 定位逻辑（单一事实源）。
    """
    path = locate_kb_db(kb_db)
    if not path:
        return None, None
    try:
        from pathlib import Path
        from src.utils.kb_store import KnowledgeBaseStore
        store = KnowledgeBaseStore(Path(path))
        return kb_search_resolver(
            store, score_threshold=score_threshold, lang=lang), store
    except Exception:
        return None, None


def kb_enabled_count(store: Any) -> int:
    """KB 已启用条目数（判 KB 是否「真备货」的信号）；取不到时保守返回 0。

    ``KnowledgeBaseStore.stats()`` 用键 ``enabled_entries``；兼容 ``enabled`` 别名。
    """
    try:
        s = store.stats() or {}
    except Exception:
        return 0
    val = s.get("enabled_entries", s.get("enabled", 0))
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


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
