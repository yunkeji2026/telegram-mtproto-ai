"""决策信号纯函数单测：真实运营指标 → 该不该爬一档的建议。"""

from __future__ import annotations

from src.companion.readiness_signals import (
    DEFAULTS, delivery_signal, proactive_signal, readiness_signals,
    text_autosend_signal,
)

TH = dict(DEFAULTS)


# ── 文本质量 ───────────────────────────────────────────────────────────────

def test_text_insufficient_sample():
    s = text_autosend_signal({"total": 5, "auto_pass_rate": 0.9}, TH)
    assert s["verdict"] == "insufficient" and "样本不足" in s["advice"]


def test_text_healthy():
    s = text_autosend_signal(
        {"total": 100, "auto_pass_rate": 0.7, "edit_rate": 0.2,
         "reject_rate": 0.1, "high_risk_rate": 0.1}, TH)
    assert s["verdict"] == "healthy" and "可灰度扩大真发" in s["advice"]


def test_text_caution_high_reject():
    s = text_autosend_signal(
        {"total": 100, "auto_pass_rate": 0.7, "edit_rate": 0.2,
         "reject_rate": 0.4, "high_risk_rate": 0.1}, TH)
    assert s["verdict"] == "caution" and "弃用率偏高" in s["advice"]


def test_text_caution_high_edit():
    s = text_autosend_signal(
        {"total": 100, "auto_pass_rate": 0.7, "edit_rate": 0.8,
         "reject_rate": 0.1, "high_risk_rate": 0.1}, TH)
    assert s["verdict"] == "caution" and "改写率偏高" in s["advice"]


def test_text_unavailable():
    assert text_autosend_signal(None, TH)["verdict"] == "unavailable"


# ── 主动触达 ───────────────────────────────────────────────────────────────

def test_proactive_insufficient():
    qo = {"care": {"feedback": {"like": 2, "dislike": 1}},
          "reactivation": {"feedback": {"like": 1, "dislike": 0}}}
    s = proactive_signal(qo, TH)
    assert s["verdict"] == "insufficient"


def test_proactive_healthy():
    qo = {"care": {"feedback": {"like": 8, "dislike": 1}},
          "reactivation": {"feedback": {"like": 5, "dislike": 1}}}
    s = proactive_signal(qo, TH)  # 13/15 ≈ 86.7% ≥ 75%
    assert s["verdict"] == "healthy" and "可转真发" in s["advice"]


def test_proactive_caution_low_like():
    qo = {"care": {"feedback": {"like": 3, "dislike": 7}},
          "reactivation": {"feedback": {"like": 1, "dislike": 2}}}
    s = proactive_signal(qo, TH)  # 4/13 ≈ 30%
    assert s["verdict"] == "caution" and "继续 dry_run" in s["advice"]


# ── 真发投递 ───────────────────────────────────────────────────────────────

def test_delivery_idle():
    assert delivery_signal({"autosend": 0, "autosend_failed": 0}, TH)["verdict"] == "idle"


def test_delivery_unknown_when_none():
    assert delivery_signal({"autosend": None, "autosend_failed": None}, TH)["verdict"] == "unknown"


def test_delivery_healthy():
    s = delivery_signal({"autosend": 100, "autosend_failed": 2}, TH)
    assert s["verdict"] == "healthy"


def test_delivery_failing():
    s = delivery_signal({"autosend": 80, "autosend_failed": 20}, TH)
    assert s["verdict"] == "failing" and "失败率" in s["advice"]


# ── 聚合 + 头条 ────────────────────────────────────────────────────────────

def test_readiness_headline_picks_worst():
    rep = readiness_signals(
        text_quality={"total": 100, "auto_pass_rate": 0.7, "edit_rate": 0.2,
                      "reject_rate": 0.1, "high_risk_rate": 0.1},   # healthy
        proactive_quality={"care": {"feedback": {"like": 1, "dislike": 9}},
                           "reactivation": {"feedback": {"like": 0, "dislike": 1}}},  # caution
        delivery={"autosend": 80, "autosend_failed": 20})            # failing
    assert len(rep["signals"]) == 3
    # failing 优先级最高 → 头条
    assert rep["headline"]["verdict"] == "failing"
    assert rep["headline"]["key"] == "delivery_health"


def test_readiness_threshold_override():
    rep = readiness_signals(
        text_quality={"total": 10, "auto_pass_rate": 0.9, "edit_rate": 0.1,
                      "reject_rate": 0.05, "high_risk_rate": 0.0},
        thresholds={"text_min_n": 5})  # 降低门槛 → 不再 insufficient
    txt = next(s for s in rep["signals"] if s["key"] == "l2_autosend_deliver")
    assert txt["verdict"] == "healthy"
