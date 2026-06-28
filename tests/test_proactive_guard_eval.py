"""主动护栏闭环评测（L，情绪安全闸门门禁）。

纯函数常驻。安全不变量：severe 窗口内必 block；窗口外正确退化；负面末条 → soft。
"""

from __future__ import annotations

from src.eval.dataset import ProactiveGuardScenario, load_proactive_guard_scenarios
from src.eval.proactive_guard_eval import (
    evaluate_proactive_guard,
    format_proactive_guard_report,
)


def test_severe_in_window_blocks():
    scn = [ProactiveGuardScenario("severe", 1, "平稳", "block")]
    rep = evaluate_proactive_guard(scn)
    assert rep["summary"]["severe_block_recall"] == 1.0
    assert rep["passed"]


def test_severe_out_of_window_degrades():
    # 窗口外 + 末条平稳 → 不抑制（已缓和）
    scn = [ProactiveGuardScenario("severe", 30, "平稳", "")]
    rep = evaluate_proactive_guard(scn)
    assert rep["passed"]


def test_negative_last_emotion_soft():
    scn = [ProactiveGuardScenario("", 0, "焦虑", "soft")]
    rep = evaluate_proactive_guard(scn)
    assert rep["passed"]


def test_under_suppress_fails():
    # 期望 block 却得空（人为错标）→ 指标应敏感 FAIL
    scn = [ProactiveGuardScenario("", 0, "happy", "block")]
    rep = evaluate_proactive_guard(scn)
    assert rep["passed"] is False


def test_intensity_grading_low_not_suppressed():
    # O：轻度负面（有点焦虑）不抑制，重度（很焦虑）才 soft，未知保守 soft
    from src.utils.wellbeing_guard import proactive_emotion_gate
    assert proactive_emotion_gate(None, now=0, last_emotion="焦虑",
                                  last_emotion_intensity=0.35) == ""
    assert proactive_emotion_gate(None, now=0, last_emotion="焦虑",
                                  last_emotion_intensity=0.8) == "soft"
    assert proactive_emotion_gate(None, now=0, last_emotion="焦虑") == "soft"


def test_intensity_does_not_weaken_crisis():
    # 强度分级绝不削弱危机：severe 窗口内仍 block，即便强度低
    from src.utils.wellbeing_guard import proactive_emotion_gate
    latest = {"level": "severe", "created_at": 900}  # 100s 前，窗口内
    assert proactive_emotion_gate(latest, now=1000, last_emotion="焦虑",
                                  last_emotion_intensity=0.1) == "block"


def test_proactive_guard_gate():
    scenarios = load_proactive_guard_scenarios(
        "config/eval/proactive_guard_samples.yaml")
    rep = evaluate_proactive_guard(scenarios)
    assert rep["passed"], (
        "\n主动护栏未达安全门禁：\n" + format_proactive_guard_report(rep))
