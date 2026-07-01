"""AI 回复质量退化评估（evaluate_ai_quality）纯函数单测（F1）。

锁定告警口径不变量：采纳/弃用率分级（warn/fail）、样本门（薄数据静默）、高危量环比
（绝对下限 + 需真增）、red 主导（任一 fail 即 red）、阈值覆盖与 None 忽略。
"""

from src.utils.ai_quality_alert import (
    calibrate_ai_quality, evaluate_ai_quality, sanitize_ai_quality_thresholds,
)


def _summary(**kw):
    base = {"reviewed": 0, "adopt_rate": 1.0, "reject_rate": 0.0, "high_risk": 0}
    base.update(kw)
    return base


def test_healthy_returns_green():
    cur = _summary(reviewed=50, adopt_rate=0.8, reject_rate=0.05, high_risk=0)
    res = evaluate_ai_quality(cur, {})
    assert res["light"] == "green" and res["problems"] == []


def test_adopt_low_warn_then_severe_fail():
    warn = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.30), {})  # < 0.40
    ids = {p["id"]: p["status"] for p in warn["problems"]}
    assert ids.get("adopt_rate_low") == "warn" and warn["light"] == "yellow"
    fail = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.10), {})  # < 0.20
    ids2 = {p["id"]: p["status"] for p in fail["problems"]}
    assert ids2.get("adopt_rate_low") == "fail" and fail["light"] == "red"


def test_reject_high_warn_then_severe_fail():
    warn = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.8, reject_rate=0.40), {})  # > 0.35
    assert {p["id"]: p["status"] for p in warn["problems"]}.get("reject_rate_high") == "warn"
    fail = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.8, reject_rate=0.70), {})  # > 0.60
    ids2 = {p["id"]: p["status"] for p in fail["problems"]}
    assert ids2.get("reject_rate_high") == "fail" and fail["light"] == "red"


def test_sample_gate_silences_rate_rules():
    # reviewed < min_samples(20) → 采纳/弃用率规则不触发，即便 adopt=0/reject=1
    res = evaluate_ai_quality(_summary(reviewed=5, adopt_rate=0.0, reject_rate=1.0), {})
    assert res["light"] == "green" and res["problems"] == []


def test_high_risk_spike_needs_floor_and_real_increase():
    # 环比 +6（12 vs 6）且 ≥ floor(5) → warn
    up = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.8, high_risk=12),
                             _summary(high_risk=6))
    assert any(p["id"] == "high_risk_spike" and p["status"] == "warn" for p in up["problems"])
    # 量高但环比无增（12 vs 12）→ 不报
    flat = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.8, high_risk=12),
                               _summary(high_risk=12))
    assert not any(p["id"] == "high_risk_spike" for p in flat["problems"])
    # 增量够但绝对量低于 floor(5)（4 vs 0）→ 不报
    tiny = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.8, high_risk=4),
                               _summary(high_risk=0))
    assert not any(p["id"] == "high_risk_spike" for p in tiny["problems"])


def test_red_dominates_when_any_fail():
    # adopt severe(fail) + high_risk spike(warn) 同现 → light=red
    res = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.10, high_risk=20),
                              _summary(high_risk=6))
    assert res["light"] == "red"
    assert {p["id"] for p in res["problems"]} >= {"adopt_rate_low", "high_risk_spike"}


def test_thresholds_override_and_none_ignored():
    # 抬高 adopt_min 到 0.9 → adopt 0.5 变越界（warn，未破 severe 默认 0.20）；None 值被忽略回落默认
    res = evaluate_ai_quality(_summary(reviewed=50, adopt_rate=0.5),
                              {}, {"adopt_min": 0.9, "adopt_severe": None})
    ids = {p["id"]: p["status"] for p in res["problems"]}
    assert ids.get("adopt_rate_low") == "warn"


# ---- calibrate_ai_quality（F2b 阈值校准回放）----

def _day(approved=0, edited=0, rejected=0, high_risk=0, autosend=0, blocked=0, day="d"):
    return {"day": day, "approved": approved, "edited": edited, "rejected": rejected,
            "reviewed": approved + edited + rejected, "high_risk": high_risk,
            "autosend": autosend, "blocked": blocked}


def test_calibrate_short_series_no_windows():
    # 序列比窗口短 → 无可评估窗口、零告警、分布空。
    out = calibrate_ai_quality([_day(approved=5)], window_days=2)
    assert out["evaluated_windows"] == 0
    assert out["alerts"] == 0 and out["days_in_alert"] == 0
    assert out["worst_light"] == "green"
    assert out["distribution"]["adopt_rate"]["n"] == 0


def test_calibrate_persistent_low_adopt_dedups_episodes():
    # 4 天持续低采纳（win=2 → 3 个窗口都 fail），但签名不变 → 去抖后只算 1 次告警。
    daily = [_day(approved=1, rejected=9, day="d%d" % i) for i in range(4)]  # 每窗 adopt=2/20=0.1
    out = calibrate_ai_quality(daily, window_days=2)
    assert out["evaluated_windows"] == 3
    assert out["days_in_alert"] == 3
    assert out["alerts"] == 1  # 去抖：持续同签名只报一次
    assert out["by_signal"].get("adopt_rate_low") == 3
    assert out["worst_light"] == "red"
    assert out["distribution"]["adopt_rate"]["median"] == 0.1
    assert out["distribution"]["adopt_rate"]["n"] == 3


def test_calibrate_alert_reset_recounts_new_episode():
    # 健康→坏→坏→健康→坏（win=1 每天一窗）：坏段被健康打断 → 两次独立告警。
    healthy = _day(approved=20, edited=3, rejected=2)   # adopt 0.8
    bad = _day(approved=2, edited=21, rejected=2)        # adopt 0.08（<0.20 fail），reject 0.08 不触发
    daily = [healthy, bad, bad, healthy, bad]
    out = calibrate_ai_quality(daily, window_days=1)
    assert out["evaluated_windows"] == 5
    assert out["days_in_alert"] == 3
    assert out["alerts"] == 2  # 中间被健康窗口重置 → 第二段重新计一次
    assert out["by_signal"].get("adopt_rate_low") == 3


def test_calibrate_points_enriched_per_window():
    # 每个评估窗口的 point 带 day/light/problem_ids + 窗口指标（供 UI 逐窗时间条+悬停详情）。
    bad = _day(approved=1, rejected=9, day="d0")   # adopt 0.1
    daily = [bad, _day(approved=1, rejected=9, day="d1")]
    out = calibrate_ai_quality(daily, window_days=2)
    assert len(out["points"]) == 1
    p = out["points"][0]
    assert p["day"] == "d1" and p["light"] == "red"
    assert "adopt_rate_low" in p["problem_ids"]
    assert p["adopt_rate"] == 0.1 and "reject_rate" in p and "high_risk" in p


def test_calibrate_healthy_series_silent():
    # 全健康 → 零告警、绿灯，但分布仍如实汇总窗口采纳率。
    daily = [_day(approved=24, rejected=1, day="d%d" % i) for i in range(5)]  # adopt 0.96
    out = calibrate_ai_quality(daily, window_days=2)
    assert out["alerts"] == 0 and out["days_in_alert"] == 0
    assert out["worst_light"] == "green"
    assert out["distribution"]["adopt_rate"]["n"] == 4  # 5 天 → 4 个窗口
    assert out["distribution"]["adopt_rate"]["median"] == 0.96


# ---- sanitize_ai_quality_thresholds（F2b++ 写 config overlay 前的清洗）----

def test_sanitize_whitelists_and_casts():
    # 白名单键强制类型（率→float / 计数→int），非阈值键（bogus/enabled）一律丢弃。
    out = sanitize_ai_quality_thresholds({
        "adopt_min": "0.5", "min_samples": "30", "high_risk_spike": 4,
        "bogus": 1, "enabled": True,
    })
    assert out == {"adopt_min": 0.5, "min_samples": 30, "high_risk_spike": 4}
    assert isinstance(out["adopt_min"], float) and isinstance(out["min_samples"], int)


def test_sanitize_drops_out_of_range_and_none():
    # 率越界（>1 / <0）、计数为负、None 一律丢，合法项保留。
    out = sanitize_ai_quality_thresholds({
        "adopt_min": 1.5, "reject_max": -0.1, "min_samples": -1,
        "adopt_severe": None, "reject_severe": 0.6,
    })
    assert out == {"reject_severe": 0.6}


def test_sanitize_non_dict_and_unparsable():
    assert sanitize_ai_quality_thresholds(None) == {}
    assert sanitize_ai_quality_thresholds("x") == {}
    assert sanitize_ai_quality_thresholds({"adopt_min": "abc"}) == {}
