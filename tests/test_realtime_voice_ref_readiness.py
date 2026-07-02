"""参考音就绪度聚合 + 叠加到 realtime_voice 信号。"""

from __future__ import annotations

from src.companion.realtime_voice_ref_readiness import (
    apply_ref_to_rtv_verdict,
    summarize_voice_ref_rows,
)


def test_summarize_empty():
    s = summarize_voice_ref_rows([])
    assert s["persona_count"] == 0 and s["worst_grade"] == "none"


def test_summarize_worst_grade_red():
    rows = [
        {"persona_id": "a", "has_reference": True,
         "health": {"grade": "green", "issues": []}},
        {"persona_id": "b", "has_reference": True,
         "health": {"grade": "red", "issues": ["录音过短"]}},
    ]
    s = summarize_voice_ref_rows(rows)
    assert s["with_reference"] == 2 and s["worst_grade"] == "red"
    assert "录音过短" in s["sample_issues"]


def test_apply_ref_no_reference_downgrades_healthy():
    v, a = apply_ref_to_rtv_verdict(
        "healthy", "ok",
        {"persona_count": 2, "with_reference": 0, "worst_grade": "none"})
    assert v == "caution" and "参考音" in a


def test_apply_ref_red_keeps_failing():
    v, a = apply_ref_to_rtv_verdict(
        "failing", "host bad",
        {"persona_count": 1, "with_reference": 1, "worst_grade": "red",
         "sample_issues": ["削波破音"]})
    assert v == "failing" and "host bad" in a
