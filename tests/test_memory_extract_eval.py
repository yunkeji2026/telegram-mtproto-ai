"""陪伴记忆抽取质量评测 + 抽取器误抽守护回归。

口径：记忆质量上限在「抽取」——抽漏=该记没记，抽错=噪声污染长期记忆。
门禁/对比策略（对齐 FAQ/翻译/召回门禁）：
  - 启发式抽取器是纯函数 → 召回 + 误抽守护门禁**始终运行**（离线可复现）。
  - LLM 抽取（ai_client.extract_memory_bullets）→ 不可用则 skip。

可调环境变量：
  - ``AITR_EXTRACT_RECALL_TARGET``（默认 0.8）召回率 PASS 目标
  - ``AITR_EXTRACT_MAX_FP``（默认 0）允许的最大误抽数
"""

from __future__ import annotations

import os

import pytest

from src.eval.dataset import ExtractSample, load_extract_samples
from src.eval.memory_extract_eval import (
    build_llm_extract_fn,
    evaluate_fact_extraction,
    format_extract_report,
    heuristic_extract_fn,
)
from src.utils.memory_heuristic import extract_heuristic_facts


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


# ── 抽取器误抽守护（直接打 extract_heuristic_facts，锁回归）──────────

def test_heuristic_recall_self_name_kept():
    assert any("小明" in f for f in extract_heuristic_facts("我是小明，很高兴认识你"))
    assert any("阿强" in f for f in extract_heuristic_facts("以后叫我阿强就行"))


def test_heuristic_rejects_verb_fragments_as_name():
    # 「我是说真的 / 我是来… / 我是觉得…」不得被误归为自称
    for t, bad in [
        ("我是说真的，你别不信", "说真的"),
        ("我是来问个问题的", "来问个问题"),
        ("我是觉得这样不太好", "觉得"),
        ("叫我别走啊", "别走"),
    ]:
        facts = extract_heuristic_facts(t)
        assert not any(bad in f for f in facts), f"误抽: {t} -> {facts}"


# ── 评测核心（纯函数，离线可复现）──────────────────────────────

def test_evaluate_fact_extraction_recall_and_fp():
    rep = evaluate_fact_extraction(heuristic_extract_fn)
    assert rep["summary"]["recall"] == 1.0
    assert rep["summary"]["false_positives"] == 0
    assert rep["passed"] is True


def test_evaluate_counts_missing_as_recall_miss():
    # 启发式抽不出"职业"，故 expect 命中失败 → 召回 < 1
    samples = [ExtractSample("我在一家银行上班", expect=["银行"], note="职业")]
    rep = evaluate_fact_extraction(heuristic_extract_fn, samples)
    assert rep["summary"]["recall"] == 0.0
    assert rep["passed"] is False


def test_evaluate_counts_false_positive():
    # 用一个"乱抽"的假抽取器验证 forbid 计数
    def _bad(text: str, reply: str = ""):
        return ["用户自称：说真的"]
    samples = [ExtractSample("我是说真的", forbid=["说真的"])]
    rep = evaluate_fact_extraction(_bad, samples)
    assert rep["summary"]["false_positives"] == 1
    assert rep["passed"] is False


def test_report_format_smoke():
    rep = evaluate_fact_extraction(heuristic_extract_fn)
    out = format_extract_report(rep)
    assert "记忆抽取质量报告" in out


# ── 启发式抽取常驻门禁（离线，从 YAML 样本）──────────────────────

def test_heuristic_extraction_gate():
    target = _env_float("AITR_EXTRACT_RECALL_TARGET", 0.8)
    max_fp = _env_int("AITR_EXTRACT_MAX_FP", 0)
    samples = load_extract_samples("config/eval/memory_extract_samples.yaml")
    rep = evaluate_fact_extraction(
        heuristic_extract_fn, samples,
        recall_target=target, max_false_positive=max_fp)
    assert rep["passed"], (
        "\n启发式记忆抽取未达门禁——修抽取器/补样本/校准阈值：\n"
        + format_extract_report(rep))


# ── 实景：真实 LLM 抽取门禁（不可用则跳过）──────────────────────

def test_llm_extraction_gate():
    extract_fn = build_llm_extract_fn()
    if extract_fn is None:
        pytest.skip("ai_client.extract_memory_bullets 不可用（无 key）；LLM 抽取门禁跳过")
    target = _env_float("AITR_EXTRACT_RECALL_TARGET", 0.8)
    # LLM 召回口径放宽：LLM 抽取是概括/推断，表述与 expect 子串未必字面一致，
    # 只守"不崩 + 误抽不超阈"，召回目标可经 env 下调。
    samples = load_extract_samples("config/eval/memory_extract_samples.yaml")
    rep = evaluate_fact_extraction(
        extract_fn, samples,
        recall_target=_env_float("AITR_EXTRACT_LLM_RECALL_TARGET", 0.5),
        max_false_positive=_env_int("AITR_EXTRACT_LLM_MAX_FP", 2))
    assert rep["passed"], (
        "\nLLM 记忆抽取未达门禁：\n" + format_extract_report(rep))
