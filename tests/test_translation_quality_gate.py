"""翻译回译质量 CI 门禁 + 评测基建自测。

口径：源文 → 译文 → 回译，回译与原文相似度近似质量（无需参考译文）。

门禁策略（务实、不误伤，对齐 FAQ 门禁）：
  - **无确定性引擎**（未配 DeepL/Google key 或未列入 translation.engines.order）→ skip。
    刻意不用 AI 引擎做回译度量（要可复现 + 零 LLM 成本）。
  - **有确定性引擎** → 跑回译，合格率 ≥ 目标即 PASS，未达打印低分清单。

可调环境变量（回译相似度是相对指标，绝对阈值需按引擎/语对校准）：
  - ``AITR_XLATE_SAMPLE_THRESHOLD``（默认 0.5）单样本合格相似度阈
  - ``AITR_XLATE_PASS_TARGET``（默认 0.6）合格率 PASS 目标
"""

from __future__ import annotations

import os

import pytest

from src.eval.dataset import TransSample, load_translation_samples
from src.eval.translation_eval import (
    build_deterministic_evaluator,
    evaluate_translation_quality,
    format_translation_report,
    text_similarity,
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# ── 纯相似度（确定性、无引擎依赖）──────────────────────────────────

def test_similarity_identical_is_one():
    assert text_similarity("你今天过得怎么样", "你今天过得怎么样") == 1.0


def test_similarity_ignores_punctuation_and_case():
    assert text_similarity("Hello, World!", "hello world") == 1.0
    assert text_similarity("你好，在吗？", "你好在吗") == 1.0


def test_similarity_disjoint_is_low():
    assert text_similarity("苹果香蕉", "汽车飞机") < 0.3


def test_similarity_empty_edges():
    assert text_similarity("", "") == 1.0
    assert text_similarity("x", "") == 0.0


# ── 回译评测核心（注入 fake translate_fn）──────────────────────────

def _samples():
    return [TransSample("你今天过得怎么样", "en"),
            TransSample("记得按时吃饭", "ja")]


async def _perfect_translate(text, src, tgt):
    # 完美往返：tgt 阶段编码为 "<tgt>:原文"，回 src 阶段还原 → 回译==原文
    if text.startswith(f"{tgt}:"):
        return text  # 不应发生
    if "::" in text:
        return text.split("::", 1)[1]   # 回译还原
    return f"{tgt}::{text}"             # 正向编码


@pytest.mark.asyncio
async def test_evaluate_perfect_roundtrip_passes():
    rep = await evaluate_translation_quality(
        _perfect_translate, _samples(), per_sample_threshold=0.9, pass_target=1.0)
    assert rep["passed"] is True
    assert rep["summary"]["pass_rate"] == 1.0
    assert rep["summary"]["mean_score"] == 1.0


@pytest.mark.asyncio
async def test_evaluate_garbled_roundtrip_fails():
    async def _garble(text, src, tgt):
        return "完全不同的内容无关紧要"   # 回译永远跑题
    rep = await evaluate_translation_quality(
        _garble, _samples(), per_sample_threshold=0.5, pass_target=0.6)
    assert rep["passed"] is False
    assert rep["summary"]["pass_rate"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_forward_failure_marks_zero():
    async def _empty(text, src, tgt):
        return ""    # 引擎不可用 → 正向失败
    rep = await evaluate_translation_quality(_empty, _samples())
    assert all(not r["ok"] for r in rep["results"])
    assert rep["results"][0]["reason"] == "forward_failed"


@pytest.mark.asyncio
async def test_evaluate_uses_detect_fn_for_source():
    seen = []

    async def _tx(text, src, tgt):
        seen.append((text, src, tgt))
        return f"{tgt}::{text}" if "::" not in text else text.split("::", 1)[1]

    def _detect(_text):
        return "zh-CN"   # 归一化为 zh，并取 [0] 段

    await evaluate_translation_quality(
        _tx, [TransSample("你好", "en")], detect_fn=_detect)
    # 正向调用源语言应为检测归一化后的 zh
    assert seen[0][1] == "zh"


def test_report_format_smoke():
    rep = {"summary": {"total": 1, "passed_samples": 0, "pass_rate": 0.0,
                       "mean_score": 0.1}, "pass_target": 0.6, "passed": False,
           "results": [{"text": "你好", "target": "en", "score": 0.1,
                        "ok": False, "back": "xx"}]}
    out = format_translation_report(rep)
    assert "翻译回译质量报告" in out and "[FAIL]" in out


# ── 实景门禁（缺确定性引擎优雅跳过）────────────────────────────────

@pytest.mark.asyncio
async def test_translation_quality_gate():
    ev = build_deterministic_evaluator()
    if ev is None:
        pytest.skip("无确定性翻译引擎（未配 DeepL/Google key 或未列入 "
                    "translation.engines.order）；翻译质量门禁跳过")
    translate_fn, detect_fn = ev
    sample_th = _env_float("AITR_XLATE_SAMPLE_THRESHOLD", 0.5)
    target = _env_float("AITR_XLATE_PASS_TARGET", 0.6)
    samples = load_translation_samples("config/eval/translation_samples.yaml")
    report = await evaluate_translation_quality(
        translate_fn, samples, detect_fn=detect_fn,
        per_sample_threshold=sample_th, pass_target=target)
    assert report["passed"], (
        "\n翻译回译质量未达门禁——请校准引擎/术语或调阈值 "
        "AITR_XLATE_SAMPLE_THRESHOLD/AITR_XLATE_PASS_TARGET：\n"
        + format_translation_report(report))


def test_build_evaluator_none_when_no_deterministic_engine():
    # 仅 AI 引擎（默认 order）→ 无确定性引擎 → None（门禁据此跳过）
    assert build_deterministic_evaluator({"translation": {"engines": {"order": ["ai"]}}}) is None


def test_build_evaluator_none_when_deepl_key_absent():
    # 列了 deepl 但无 key → available=False → None
    cfg = {"translation": {"engines": {"order": ["deepl"], "deepl": {"api_key": ""}}}}
    assert build_deterministic_evaluator(cfg) is None
