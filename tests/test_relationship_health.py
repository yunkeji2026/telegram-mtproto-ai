"""Phase P1：单人关系健康度打分器单测（纯函数，确定性）。

覆盖：recency/trend/mutuality 分段、健康/观察/风险/危机分级、value_at_risk 判定、
建议动作优先级（care_pending > reactivate > schedule_care > deepen > maintain > none）、
原因文案、grade 带与 digest 一致。
"""
from src.contacts.relationship_health import (
    ContactHealthSignals,
    score_contact_health,
)


def test_active_high_intimacy_is_healthy():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=85, days_since_last_msg=0.5, prev_intimacy_score=82,
        funnel_stage="BONDED", turn_count_in=40, turn_count_out=38,
    ))
    assert c.risk_level == "healthy"
    assert c.grade in ("A", "B")
    assert c.value_at_risk is False
    assert c.action in ("maintain", "none")


def test_high_intimacy_long_silence_is_value_at_risk():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=78, days_since_last_msg=20, prev_intimacy_score=80,
        funnel_stage="BONDED", turn_count_in=30, turn_count_out=30,
    ))
    assert c.value_at_risk is True
    assert c.risk_level in ("at_risk", "critical", "watch")
    # 高亲密 + 无近期唤醒 → 建议唤醒
    assert c.action == "reactivate"
    assert any("沉默" in r for r in c.reasons)
    assert any("高价值" in r for r in c.reasons)


def test_never_messaged_is_critical():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=0, days_since_last_msg=float("inf"),
    ))
    assert c.risk_level == "critical"
    assert c.grade == "D"
    assert c.value_at_risk is False
    assert any("从无消息" in r for r in c.reasons)
    assert c.components["days_since_last_msg"] is None


def test_pending_care_takes_priority_action():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=60, days_since_last_msg=10, prev_intimacy_score=62,
        funnel_stage="LINE_ENGAGED", turn_count_in=20, turn_count_out=18,
        pending_care=2,
    ))
    assert c.action == "care_pending"
    assert any("已排" in r for r in c.reasons)


def test_recent_reactivation_does_not_recommend_reactivate():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=55, days_since_last_msg=12, prev_intimacy_score=58,
        funnel_stage="LINE_ENGAGED", turn_count_in=15, turn_count_out=14,
        has_recent_reactivation=True,
    ))
    # cooldown 内不再建议唤醒 → 转 schedule_care
    assert c.action == "schedule_care"


def test_declining_watch_recommends_deepen():
    # 沉默不久但亲密度明显下滑 → watch + deepen
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=58, days_since_last_msg=4, prev_intimacy_score=66,
        funnel_stage="WARMING", turn_count_in=12, turn_count_out=11,
    ))
    assert c.risk_level == "watch"
    assert c.action == "deepen"
    assert any("下滑" in r for r in c.reasons)


def test_one_sided_conversation_flagged():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=40, days_since_last_msg=2, prev_intimacy_score=40,
        turn_count_in=2, turn_count_out=30,
    ))
    assert any("单向" in r for r in c.reasons)
    assert c.components["mutuality"] < 0.4


def test_trend_neutral_when_no_baseline():
    c = score_contact_health(ContactHealthSignals(
        intimacy_score=50, days_since_last_msg=1, prev_intimacy_score=None,
        turn_count_in=10, turn_count_out=10,
    ))
    assert c.components["trend"] == 0.6
    assert c.components["intimacy_delta"] is None


def test_score_monotonic_in_silence():
    base = dict(intimacy_score=60, prev_intimacy_score=60,
                turn_count_in=20, turn_count_out=20)
    fresh = score_contact_health(ContactHealthSignals(days_since_last_msg=1, **base))
    week = score_contact_health(ContactHealthSignals(days_since_last_msg=8, **base))
    month = score_contact_health(ContactHealthSignals(days_since_last_msg=40, **base))
    assert fresh.score > week.score > month.score


def test_grade_bands_match_digest():
    # 边界：80→A, 65→B, 45→C, else D（与 /api/relations/digest 一致）
    from src.contacts.relationship_health import _grade
    assert _grade(80) == "A" and _grade(79.9) == "B"
    assert _grade(65) == "B" and _grade(64.9) == "C"
    assert _grade(45) == "C" and _grade(44.9) == "D"
