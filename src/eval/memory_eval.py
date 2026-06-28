"""陪伴记忆召回质量评测（关键词 vs 向量融合，量化「开向量」的收益）。

口径：给定一组事实（含干扰项）+ 一句查询 + 期望被召回的事实子串，用**真实**
``EpisodicMemoryStore.get_bullets_for_prompt`` 端到端跑召回，看期望事实是否进 top-k；
对比「纯关键词」与「向量融合」两策略的召回率，输出 delta（开向量的净收益）。

设计（与 faq/translation 评测一致）：
  - 核心 ``evaluate_memory_recall`` 跑真实 store（临时 sqlite），``embed_fn`` 注入：
    * 单测/离线：``deterministic_embed``（零依赖、可复现）→ 验证管线机制（非语义天花板）。
    * 实景：``build_real_embed_fn`` 包 ``ai_client.embed`` → 度量真实语义召回收益。
  - **不改任何生产默认**：本模块是「该不该开向量」的数据依据，开启仍走 capability 看板治理。

注意：确定性本地嵌入只捕获**字面重叠**，故离线下 vector≈keyword；要看向量对**语义/改写**
查询的真实增益，必须用 ai_client 真实嵌入跑（缺则门禁优雅跳过）。
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .dataset import MemoryScenario, load_memory_scenarios

EmbedFn = Callable[[str], Optional[List[float]]]
_EVAL_USER = "memory_eval_user"


def deterministic_embed(text: str, dim: int = 64) -> List[float]:
    """字符 bigram 哈希 → L2 归一向量。零依赖、可复现（验证管线，非语义天花板）。"""
    t = (text or "").strip().lower()
    vec = [0.0] * dim
    grams = [t[i:i + 2] for i in range(len(t) - 1)] if len(t) >= 2 else (list(t) or [" "])
    for g in grams:
        hh = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[hh % dim] += 1.0 if ((hh >> 8) & 1) else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 1e-9 else vec


def recall_hit(bullets: str, expected: List[str]) -> bool:
    """期望子串全部出现在召回 bullets 中即视为命中（保守 AND）。"""
    text = bullets or ""
    exp = [str(e) for e in (expected or []) if str(e)]
    if not exp:
        return False
    return all(e in text for e in exp)


def _run_one(
    scenario: MemoryScenario, *,
    embed_fn: Optional[EmbedFn], top_k: int, use_vector: bool,
) -> Dict[str, Any]:
    """单场景端到端跑真实 EpisodicMemoryStore 召回。返回 {hit, bullets}。"""
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    from src.utils.episodic_vector import vec_to_blob

    tmp_dir = tempfile.mkdtemp(prefix="mem_eval_")
    db = Path(tmp_dir) / "epi_eval.db"
    store = EpisodicMemoryStore(db)
    try:
        for f in scenario.facts:
            blob = None
            if use_vector and embed_fn is not None:
                v = embed_fn(f)
                if v:
                    blob = vec_to_blob(v)
            store.add_fact(_EVAL_USER, f, "eval", embedding_blob=blob)
        qe = embed_fn(scenario.query) if (use_vector and embed_fn is not None) else None
        bullets = store.get_bullets_for_prompt(
            _EVAL_USER, max_items=top_k, query_text=scenario.query,
            rerank_keywords=True, query_embedding=qe, use_vector_fusion=use_vector,
        )
        return {"hit": recall_hit(bullets, scenario.expected), "bullets": bullets}
    finally:
        try:
            store.close()
        except Exception:
            pass
        try:
            os.remove(db)
            os.rmdir(tmp_dir)
        except Exception:
            pass


def evaluate_memory_recall(
    scenarios: Optional[List[MemoryScenario]] = None,
    *,
    embed_fn: Optional[EmbedFn] = None,
    top_k: int = 5,
    use_vector: bool = False,
    recall_target: float = 0.6,
) -> Dict[str, Any]:
    """跑召回评测；返回逐场景命中 + 召回率 + passed。"""
    rows = scenarios if scenarios is not None else load_memory_scenarios()
    results: List[Dict[str, Any]] = []
    hits = 0
    for sc in rows:
        out = _run_one(sc, embed_fn=embed_fn, top_k=top_k, use_vector=use_vector)
        hits += 1 if out["hit"] else 0
        results.append({"query": sc.query, "expected": sc.expected,
                        "hit": out["hit"], "note": sc.note})
    n = len(results)
    recall = round(hits / n, 3) if n else 0.0
    return {
        "strategy": "vector" if use_vector else "keyword",
        "results": results,
        "summary": {"total": n, "hits": hits, "recall": recall},
        "recall_target": recall_target,
        "passed": recall >= recall_target,
    }


def compare_recall(
    scenarios: Optional[List[MemoryScenario]] = None,
    *, embed_fn: EmbedFn, top_k: int = 5,
) -> Dict[str, Any]:
    """关键词 vs 向量融合对比；delta_recall>0 即开向量有净收益。"""
    rows = scenarios if scenarios is not None else load_memory_scenarios()
    kw = evaluate_memory_recall(rows, embed_fn=None, top_k=top_k, use_vector=False)
    vec = evaluate_memory_recall(rows, embed_fn=embed_fn, top_k=top_k, use_vector=True)
    return {
        "keyword": kw, "vector": vec,
        "delta_recall": round(vec["summary"]["recall"] - kw["summary"]["recall"], 3),
    }


def evaluate_semantic_dedup(
    *,
    embed_fn: EmbedFn,
    dup_groups: Optional[List[List[str]]] = None,
    distinct: Optional[List[str]] = None,
    threshold: float = 0.7,
    dedup_recall_target: float = 0.6,
) -> Dict[str, Any]:
    """评测 R5 语义去重（``merge_near_duplicates``）：近义事实应并、异义事实不应误并。

    口径：把若干「同义改写组」+「互异事实」带**真实嵌入**写入临时 store，按 ``threshold``
    跑 ``merge_near_duplicates``，看：
      - 召回：每组同义改写是否被并（``merged`` ≈ 期望并掉条数 ∑(len(g)-1)）；
      - 精确：是否**过并**（把异义事实或跨组并掉 → 存活数低于 组数+异义数）。
    只在**真实语义嵌入**下有意义（确定性/哈希嵌入抓不到改写）。
    """
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    from src.utils.episodic_vector import vec_to_blob

    dup_groups = dup_groups or _SEED_DEDUP_GROUPS
    distinct = distinct if distinct is not None else _SEED_DEDUP_DISTINCT
    expected_merged = sum(max(0, len(g) - 1) for g in dup_groups)
    min_remaining = len(dup_groups) + len(distinct)   # 全并且零过并时的存活数
    all_facts: List[str] = [f for g in dup_groups for f in g] + list(distinct)

    tmp_dir = tempfile.mkdtemp(prefix="dedup_eval_")
    db = Path(tmp_dir) / "epi_dedup.db"
    store = EpisodicMemoryStore(db)
    try:
        for f in all_facts:
            v = embed_fn(f)
            blob = vec_to_blob(v) if v else None
            store.add_fact(_EVAL_USER, f, "eval", embedding_blob=blob)
        res = store.merge_near_duplicates(
            _EVAL_USER, threshold=threshold, min_raw=2)
        merged = int(res.get("merged", 0))
        remaining = store._count_tier(_EVAL_USER, "raw")  # noqa: SLF001（评测内省）
    finally:
        try:
            store.close()
        except Exception:
            pass
        try:
            os.remove(db)
            os.rmdir(tmp_dir)
        except Exception:
            pass

    dedup_recall = round(merged / expected_merged, 3) if expected_merged else 1.0
    over_merged = remaining < min_remaining
    return {
        "threshold": threshold,
        "summary": {
            "facts": len(all_facts),
            "expected_merged": expected_merged,
            "merged": merged,
            "remaining": remaining,
            "min_remaining": min_remaining,
            "dedup_recall": dedup_recall,
            "over_merged": over_merged,
        },
        "dedup_recall_target": dedup_recall_target,
        "passed": dedup_recall >= dedup_recall_target and not over_merged,
    }


def format_dedup_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    return (
        "=== 记忆语义去重报告 ===\n"
        f"事实: {m['facts']}  并掉: {m['merged']}/{m['expected_merged']} "
        f"(召回 {m['dedup_recall']:.0%})  存活: {m['remaining']}(≥{m['min_remaining']})  "
        f"过并: {'是' if m['over_merged'] else '否'}  阈值: {report['threshold']}  "
        f"目标: 召回≥{report['dedup_recall_target']:.0%}且不过并  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}"
    )


# 语义去重评测种子：每组为「同一件事的不同说法」（应并）；distinct 为互异事实（不应并）。
_SEED_DEDUP_GROUPS: List[List[str]] = [
    ["用户喜欢喝咖啡", "用户爱喝咖啡", "用户平时很爱喝咖啡"],
    ["用户养了一只猫", "用户家里有一只猫咪"],
]
_SEED_DEDUP_DISTINCT: List[str] = [
    "用户住在大阪", "用户在银行上班", "用户的生日是十月一号",
]


def format_recall_report(report: Dict[str, Any]) -> str:
    if "delta_recall" in report:           # compare 形态
        k = report["keyword"]["summary"]
        v = report["vector"]["summary"]
        return ("=== 记忆召回对比（关键词 vs 向量）===\n"
                f"关键词召回: {k['recall']:.2%} ({k['hits']}/{k['total']})  "
                f"向量召回: {v['recall']:.2%} ({v['hits']}/{v['total']})  "
                f"净收益 Δ: {report['delta_recall']:+.2%}")
    m = report["summary"]
    lines = [
        f"=== 记忆召回报告（{report['strategy']}）===",
        f"场景: {m['total']}  命中: {m['hits']}  召回率: {m['recall']:.2%}  "
        f"目标: {report['recall_target']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    miss = [r for r in report["results"] if not r["hit"]]
    if miss:
        lines.append(f"未召回 {len(miss)} 例:")
        for r in miss[:20]:
            lines.append(f"  - {r['query']}  期望={r['expected']}")
    return "\n".join(lines)


def build_real_embed_fn(config: Optional[Dict[str, Any]] = None) -> Optional[EmbedFn]:
    """返回首个可用真实 embed_fn；均不可用 → None（门禁跳过）。

    优先用 ``embedding_providers``（OpenAI 兼容端点 env/config + 本地 ST opt-in），
    解锁「DeepSeek 无 embedding 端点」下的召回/去重实跑；再回落到 ``ai_client.embed``
    （兼容 gemini / 既有 ai 配置）。
    """
    try:
        from .embedding_providers import build_embed_fn as _bld
        fn = _bld(config)
        if fn is not None:
            return fn
    except Exception:
        pass
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
            return await client.embed(["测试嵌入可用性"])

        v = asyncio.run(_probe())
        if not v or not v[0]:
            return None

        def _embed(text: str) -> Optional[List[float]]:
            try:
                vv = asyncio.run(client.embed([text]))
                return vv[0] if vv and vv[0] else None
            except Exception:
                return None

        return _embed
    except Exception:
        return None


__all__ = [
    "deterministic_embed", "recall_hit", "evaluate_memory_recall",
    "compare_recall", "format_recall_report", "build_real_embed_fn",
    "evaluate_semantic_dedup", "format_dedup_report",
]
