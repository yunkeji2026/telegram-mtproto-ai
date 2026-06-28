"""人设一致性评测 + 守卫违规召回回归（陪聊"真人感"最后防线）。

门禁：persona_guard 纯函数 → 离线常驻。违规须全召回（漏一个=沉浸感事故），合规零误伤。
可调环境变量：
  - ``AITR_PERSONA_RECALL_TARGET``（默认 1.0）违规召回 PASS 目标
  - ``AITR_PERSONA_MAX_FP``（默认 0）允许的最大误伤数
"""

from __future__ import annotations

import os

from src.eval.dataset import PersonaSample, load_persona_samples
from src.eval.persona_eval import (
    evaluate_persona_consistency,
    format_persona_report,
)
from src.utils.persona_guard import find_violations, sanitize


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


# ── 守卫违规召回回归（锁住 colloquial「我是个机器人」修复 + 否定句不误伤）──

def test_guard_catches_colloquial_self_id():
    p = {"identity": {"deny_ai": True}}
    assert find_violations("我就是个聊天机器人啦。", p)        # 口语「个」
    assert find_violations("作为一个人工智能，我没感情", p)
    assert find_violations("As an AI, I cannot.", p)


def test_guard_ignores_negation():
    p = {"identity": {"deny_ai": True}}
    # 否定句不算露馅
    assert not find_violations("我才不是什么AI呢，我是真人啦！", p)


def test_sanitize_keeps_clean_sentences():
    p = {"identity": {"deny_ai": True}}
    cleaned, hits = sanitize("作为一个AI我得说。不过我也挺想你的。", p)
    assert hits and "想你" in cleaned and cleaned.strip()


# ── 评测核心（纯函数）──────────────────────────────────────────

def test_evaluate_counts_recall_and_fp():
    samples = [
        PersonaSample("有什么可以帮您", forbidden=["有什么可以帮您"], expect_violation=True),
        PersonaSample("作为一个AI", deny_ai=True, expect_violation=True),
        PersonaSample("今天好开心呀", forbidden=["有什么可以帮您"], expect_violation=False),
    ]
    rep = evaluate_persona_consistency(samples)
    assert rep["summary"]["recall"] == 1.0
    assert rep["summary"]["false_positives"] == 0
    assert rep["passed"] is True


def test_evaluate_flags_false_positive():
    # 合规回复含禁用短语子串 → 应记误伤
    samples = [PersonaSample("为您服务真开心", forbidden=["为您服务"], expect_violation=False)]
    rep = evaluate_persona_consistency(samples)
    assert rep["summary"]["false_positives"] == 1
    assert rep["passed"] is False


def test_report_smoke():
    rep = evaluate_persona_consistency()
    assert "人设一致性报告" in format_persona_report(rep)


# ── 常驻门禁（从 YAML 样本）────────────────────────────────────

def test_persona_consistency_gate():
    target = _env_float("AITR_PERSONA_RECALL_TARGET", 1.0)
    max_fp = _env_int("AITR_PERSONA_MAX_FP", 0)
    samples = load_persona_samples("config/eval/persona_samples.yaml")
    rep = evaluate_persona_consistency(
        samples, recall_target=target, max_false_positive=max_fp)
    assert rep["passed"], (
        "\n人设守卫未达门禁——修 persona_guard/补样本：\n"
        + format_persona_report(rep))
