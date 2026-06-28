"""陪伴记忆召回质量评测 + 管线机制自测。

口径：真实 EpisodicMemoryStore 端到端跑 get_bullets_for_prompt，看期望事实是否进 top-k。
对比关键词 vs 向量融合，量化「开向量」收益。

门禁/对比策略（对齐 FAQ/翻译门禁）：
  - 纯函数 + 确定性嵌入的机制自测**始终运行**（离线可复现）。
  - 真实语义召回对比需 ai_client.embed → 不可用则 skip。

可调环境变量：
  - ``AITR_MEM_RECALL_TARGET``（默认 0.6）召回率 PASS 目标
  - ``AITR_MEM_TOPK``（默认 5）召回 top-k
"""

from __future__ import annotations

import os

import pytest

from src.eval.dataset import MemoryScenario, load_memory_scenarios
from src.eval.memory_eval import (
    build_real_embed_fn,
    compare_recall,
    deterministic_embed,
    evaluate_memory_recall,
    evaluate_semantic_dedup,
    format_dedup_report,
    format_recall_report,
    recall_hit,
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# ── 纯函数 ──────────────────────────────────────────────────────

def test_deterministic_embed_stable_and_normalized():
    a = deterministic_embed("用户喜欢喝拿铁")
    b = deterministic_embed("用户喜欢喝拿铁")
    assert a == b                      # 可复现
    assert len(a) >= 8                 # 满足 get_bullets 的维度门槛
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6      # L2 归一


def test_recall_hit_semantics():
    assert recall_hit("- 用户在准备面试\n- 用户养了狗", ["面试"]) is True
    assert recall_hit("- 用户养了狗", ["面试"]) is False
    assert recall_hit("anything", []) is False        # 无期望=不算命中


# ── 召回管线机制（真实 store + 确定性嵌入，离线可复现）────────────────

def _kw_scenario():
    # query 与目标事实字面重叠（关键词即可召回），用于验证管线本身
    return [MemoryScenario(
        facts=["用户在准备周五的面试", "用户喜欢拿铁", "用户养了狗", "用户住大阪"],
        query="面试准备得怎么样",
        expected=["面试"])]


def test_keyword_recall_pipeline_hits():
    rep = evaluate_memory_recall(_kw_scenario(), use_vector=False, top_k=5)
    assert rep["summary"]["recall"] == 1.0
    assert rep["strategy"] == "keyword"


def test_vector_recall_pipeline_runs_with_deterministic_embed():
    rep = evaluate_memory_recall(
        _kw_scenario(), embed_fn=deterministic_embed, use_vector=True, top_k=5)
    # 字面重叠场景下向量融合至少不劣于关键词（管线打通）
    assert rep["summary"]["recall"] == 1.0
    assert rep["strategy"] == "vector"


def test_compare_recall_shape():
    cmp = compare_recall(_kw_scenario(), embed_fn=deterministic_embed, top_k=5)
    assert set(cmp) == {"keyword", "vector", "delta_recall"}
    assert isinstance(cmp["delta_recall"], float)


def test_top1_excludes_distractor():
    # top_k=1 时，目标必须排在所有干扰项之前才命中（验证排序真生效）
    sc = [MemoryScenario(
        facts=["用户喜欢拿铁", "用户养了狗", "用户在准备面试", "用户住大阪"],
        query="面试准备得怎么样",
        expected=["面试"])]
    rep = evaluate_memory_recall(sc, use_vector=False, top_k=1)
    assert rep["summary"]["recall"] == 1.0


def test_report_format_smoke():
    rep = evaluate_memory_recall(_kw_scenario(), use_vector=False)
    out = format_recall_report(rep)
    assert "记忆召回报告" in out


# ── 语义去重（merge_near_duplicates）──────────────────────────────

def test_semantic_dedup_harness_shape():
    # 用确定性嵌入验证 harness 不崩 + 返回结构正确（语义判定留给真实嵌入门禁）
    rep = evaluate_semantic_dedup(embed_fn=deterministic_embed, threshold=0.9)
    s = rep["summary"]
    assert set(s) >= {"facts", "expected_merged", "merged", "remaining",
                      "min_remaining", "dedup_recall", "over_merged"}
    assert s["expected_merged"] == 3        # (3-1)+(2-1)
    assert isinstance(rep["passed"], bool)
    assert "记忆语义去重报告" in format_dedup_report(rep)


def test_semantic_dedup_gate_with_real_embeddings():
    embed_fn = build_real_embed_fn()
    if embed_fn is None:
        pytest.skip("真实嵌入不可用（配 embedding 端点或 AITR_EMBED_LOCAL=1）；语义去重门禁跳过")
    thr = _env_float("AITR_DEDUP_THRESHOLD", 0.7)
    target = _env_float("AITR_DEDUP_RECALL_TARGET", 0.6)
    rep = evaluate_semantic_dedup(
        embed_fn=embed_fn, threshold=thr, dedup_recall_target=target)
    assert rep["passed"], (
        "\n语义去重未达门禁——校准 AITR_DEDUP_THRESHOLD/补样本：\n"
        + format_dedup_report(rep))


# ── 实景：真实嵌入语义召回门禁（不可用则跳过）──────────────────────

def test_memory_recall_gate_with_real_embeddings():
    embed_fn = build_real_embed_fn()
    if embed_fn is None:
        pytest.skip("ai_client.embed 不可用（无 key/未配 embedding）；记忆召回门禁跳过")
    target = _env_float("AITR_MEM_RECALL_TARGET", 0.6)
    # top_k 须 < 每场景事实数（5 时 4 事实全返回 → 召回恒 100% 不鉴别）；3 才真考排序
    top_k = _env_int("AITR_MEM_TOPK", 3)
    scenarios = load_memory_scenarios("config/eval/memory_samples.yaml")
    rep = evaluate_memory_recall(
        scenarios, embed_fn=embed_fn, use_vector=True, top_k=top_k, recall_target=target)
    assert rep["passed"], (
        "\n记忆向量召回未达门禁——补记忆样本/校准 AITR_MEM_RECALL_TARGET：\n"
        + format_recall_report(rep))
