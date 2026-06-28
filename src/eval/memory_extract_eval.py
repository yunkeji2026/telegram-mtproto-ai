"""陪伴记忆**抽取**质量评测（源头质量：召回 + 误抽 / 精确率守护）。

口径：记忆系统的质量上限在「抽取」这一步——抽漏 = 该记的没记，抽错 = 把句子片段/
噪声当事实写进长期记忆污染人设。本模块给定一条消息 + 期望/禁止子串，跑**真实抽取器**：
  - ``expect``：该消息应被抽出的事实子串（任一抽取结果含之 → 召回命中）；
  - ``forbid``：不应被抽出的子串（出现 → 记一次误抽）。
输出召回率 + 误抽数 + passed（召回达标 **且** 误抽不超阈）。

设计（与 faq/translation/memory-recall 评测一致）：
  - 核心 ``evaluate_fact_extraction(extract_fn, samples)`` 与抽取器实现解耦，``extract_fn``
    签名 ``(text, reply) -> List[str]``：
    * 启发式：``heuristic_extract_fn``（纯函数、零依赖、可复现）→ CI 常驻门禁；
    * LLM：``build_llm_extract_fn`` 包 ``ai_client.extract_memory_bullets`` → 度量真实抽取，
      缺 key/不可用时优雅返回 None（门禁跳过）。
  - **不改任何生产默认**：本模块是抽取器的质量标尺/回归网，不触发任何写回。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .dataset import ExtractSample, load_extract_samples

# (text, reply) -> 抽出的事实串列表
ExtractFn = Callable[[str, str], List[str]]


def heuristic_extract_fn(text: str, reply: str = "") -> List[str]:
    """启发式抽取器适配为统一签名（忽略 reply）。"""
    from src.utils.memory_heuristic import extract_heuristic_facts

    return extract_heuristic_facts(text)


def _facts_contain(facts: List[str], sub: str) -> bool:
    return any(sub in (f or "") for f in (facts or []))


def evaluate_fact_extraction(
    extract_fn: ExtractFn,
    samples: Optional[List[ExtractSample]] = None,
    *,
    recall_target: float = 0.8,
    max_false_positive: int = 0,
) -> Dict[str, Any]:
    """跑抽取评测；返回逐样本明细 + 召回率 + 误抽数 + passed。

    召回率 = 命中的 expect 子串数 / 全部 expect 子串数（按子串粒度，跨样本汇总）。
    误抽数 = 全部 forbid 子串里被抽出的个数（精确率/防污染守护）。
    passed = 召回率 ≥ recall_target **且** 误抽数 ≤ max_false_positive。
    """
    rows = samples if samples is not None else load_extract_samples()
    results: List[Dict[str, Any]] = []
    total_expect = 0
    found_expect = 0
    fp_total = 0
    for s in rows:
        facts = extract_fn(s.text, s.reply) or []
        missing = [e for e in s.expect if not _facts_contain(facts, e)]
        fp_hits = [f for f in s.forbid if _facts_contain(facts, f)]
        total_expect += len(s.expect)
        found_expect += len(s.expect) - len(missing)
        fp_total += len(fp_hits)
        results.append({
            "text": s.text,
            "facts": facts,
            "missing": missing,
            "false_positives": fp_hits,
            "note": s.note,
        })
    recall = round(found_expect / total_expect, 3) if total_expect else 1.0
    return {
        "results": results,
        "summary": {
            "samples": len(rows),
            "expect_total": total_expect,
            "expect_found": found_expect,
            "recall": recall,
            "false_positives": fp_total,
        },
        "recall_target": recall_target,
        "max_false_positive": max_false_positive,
        "passed": recall >= recall_target and fp_total <= max_false_positive,
    }


def format_extract_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 记忆抽取质量报告 ===",
        f"样本: {m['samples']}  召回: {m['recall']:.2%} "
        f"({m['expect_found']}/{m['expect_total']})  "
        f"误抽: {m['false_positives']}  "
        f"目标: 召回≥{report['recall_target']:.0%}/误抽≤{report['max_false_positive']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    miss = [r for r in report["results"] if r["missing"]]
    if miss:
        lines.append(f"漏抽 {len(miss)} 例:")
        for r in miss[:20]:
            lines.append(f"  - {r['text']}  缺={r['missing']}")
    fps = [r for r in report["results"] if r["false_positives"]]
    if fps:
        lines.append(f"误抽 {len(fps)} 例:")
        for r in fps[:20]:
            lines.append(f"  - {r['text']}  误={r['false_positives']}  全部={r['facts']}")
    return "\n".join(lines)


def build_llm_extract_fn(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[ExtractFn]:
    """包 ``ai_client.extract_memory_bullets`` 成同步 extract_fn；探针失败 → None（门禁跳过）。

    注意 extract_memory_bullets 要求 user/assistant 双方均 ≥2 字符，故空 reply 时补一句
    中性占位回复，避免被早退过滤掉。
    """
    try:
        import asyncio

        cfg = config
        if cfg is None:
            import yaml
            with open("config/config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        from src.ai.ai_client import AIClient

        class _Cfg:
            config = cfg
            config_path = "config/config.yaml"

            def get_ai_config(self):
                return (cfg or {}).get("ai", {})

        client = AIClient(_Cfg())

        async def _probe():
            if hasattr(client, "initialize"):
                try:
                    await client.initialize()
                except Exception:
                    pass
            return await client.extract_memory_bullets(
                "我叫小明，住在大阪", "好的小明，我记住啦")

        try:
            _ = asyncio.run(_probe())
        except Exception:
            return None

        def _extract(text: str, reply: str = "") -> List[str]:
            r = reply or "嗯嗯，我记下了"
            try:
                return list(asyncio.run(client.extract_memory_bullets(text, r)) or [])
            except Exception:
                return []

        return _extract
    except Exception:
        return None


__all__ = [
    "ExtractFn",
    "heuristic_extract_fn",
    "evaluate_fact_extraction",
    "format_extract_report",
    "build_llm_extract_fn",
]
