"""情绪识别评测：情绪维度准确率 + 危机识别召回（安全红线）。

均为确定性纯函数 → 离线常驻门禁。
可调环境变量：
  - ``AITR_EMOTION_ACC_TARGET``（默认 0.8）情绪维度准确率 PASS 目标
  - ``AITR_CRISIS_RECALL_TARGET``（默认 1.0）severe 召回 PASS 目标
  - ``AITR_CRISIS_MAX_FALSE_ALARM``（默认 0）允许的最大误报数
"""

from __future__ import annotations

import os

from src.eval.dataset import load_crisis_samples, load_emotion_samples
from src.eval.emotion_eval import (
    emotion_dimension_predictor,
    evaluate_crisis_detection,
    evaluate_emotion_dimension,
    format_crisis_report,
    format_emotion_report,
)
from src.utils.wellbeing_guard import detect_crisis


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


# ── 情绪维度 ──────────────────────────────────────────────────

def test_dimension_predictor_basic():
    predict = emotion_dimension_predictor()
    assert predict("哈哈哈太开心了") == "positive"
    assert predict("我好难过想哭") == "negative"
    assert predict("好累啊不想动") == "low_energy"


def test_negation_not_misclassified():
    # I 否定硬化：否定句不得被判负面/低能量/思念（回归网）
    predict = emotion_dimension_predictor()
    for t in ["我一点都不难过", "别担心我没事", "没那么累", "我不想你了",
              "i'm not sad at all"]:
        assert predict(t) != "negative", t
        assert predict(t) != "low_energy", t


def test_intensity_grading_monotonic():
    # N 程度副词分级：弱化 < 基准 <= 强化
    from src.eval.emotion_intensity_eval import (
        evaluate_intensity_grading, format_intensity_report,
    )
    from src.eval.dataset import load_intensity_orders

    orders = load_intensity_orders("config/eval/emotion_intensity_samples.yaml")
    rep = evaluate_intensity_grading(orders)
    assert rep["passed"], "\n强度分级非单调：\n" + format_intensity_report(rep)


def test_emotion_dimension_gate():
    target = _env_float("AITR_EMOTION_ACC_TARGET", 0.8)
    samples = load_emotion_samples("config/eval/emotion_samples.yaml")
    rep = evaluate_emotion_dimension(samples, threshold=target)
    assert rep["passed"], (
        "\n情绪维度准确率未达门禁——补样本/校准 AITR_EMOTION_ACC_TARGET：\n"
        + format_emotion_report(rep))


# ── 危机识别（安全红线）────────────────────────────────────────

def test_detect_crisis_severe_locks():
    # 锁住自伤/轻生信号判 severe（安全回归）
    assert detect_crisis("我不想活了，真的撑不下去了")["level"] == "severe"


def test_crisis_idiom_not_escalated():
    # 日常惯用语不该误升级
    assert detect_crisis("今天累死了，笑死我了哈哈")["level"] == "none"


def test_crisis_detection_gate():
    target = _env_float("AITR_CRISIS_RECALL_TARGET", 1.0)
    max_fa = _env_int("AITR_CRISIS_MAX_FALSE_ALARM", 0)
    samples = load_crisis_samples("config/eval/crisis_samples.yaml")
    rep = evaluate_crisis_detection(
        samples, severe_recall_target=target, max_false_alarm=max_fa)
    assert rep["passed"], (
        "\n危机识别未达安全门禁——!!self-harm 漏判/惯用语误报!!：\n"
        + format_crisis_report(rep))
