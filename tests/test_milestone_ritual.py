"""Stage P：纪念日·节日仪式规划（milestone_ritual）——纯函数确定性测试。"""

from __future__ import annotations

import time

from src.utils.milestone_ritual import (
    DEFAULT_ANNIVERSARY_DAYS,
    days_known,
    due_anniversary,
    holiday_for_date,
    plan_milestone_rituals,
)


def _at(year, month, day, hour=10):
    """构造某地方时刻的 epoch（本测试用本地时区，与模块 localtime 一致）。"""
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


def _opener(**kw):
    et = kw.get("event_type")
    if et == "birthday":
        return {"mode": "milestone_birthday",
                "directive": "生日快乐", "fact": "", "context_facts": []}
    if et == "anniversary":
        return {"mode": "milestone_anniversary",
                "directive": f"认识第{kw.get('days')}天", "fact": "", "context_facts": []}
    if et == "holiday":
        return {"mode": "milestone_holiday",
                "directive": f"{kw.get('event_label')}快乐", "fact": "", "context_facts": []}
    return {"mode": "", "directive": ""}


def _conv(cid="telegram:default:1", *, intimacy=60.0, first_seen_ts=0.0, **extra):
    base = {
        "conversation_id": cid, "platform": "telegram", "account_id": "default",
        "chat_key": cid.split(":")[-1], "intimacy": intimacy,
        "first_seen_ts": first_seen_ts, "memory_key": "u1", "stage": "steady",
        "last_emotion": "",
    }
    base.update(extra)
    return base


# ── days_known / due_anniversary ─────────────────────────────────────────

def test_days_known_basic():
    now = _at(2026, 4, 11)
    first = now - 100 * 86400
    assert days_known(first, now) == 100


def test_days_known_invalid():
    assert days_known(0, 1000.0) == -1
    assert days_known("x", 1000.0) == -1
    assert days_known(2000.0, 1000.0) == -1  # 未来


def test_due_anniversary_hits_milestone():
    now = _at(2026, 4, 11)
    first = now - 100 * 86400
    assert due_anniversary(first, now) == 100


def test_due_anniversary_not_a_milestone():
    now = _at(2026, 4, 11)
    first = now - 99 * 86400
    assert due_anniversary(first, now) is None


def test_due_anniversary_custom_list():
    now = _at(2026, 4, 11)
    first = now - 50 * 86400
    assert due_anniversary(first, now, [50, 60]) == 50


# ── holiday_for_date ─────────────────────────────────────────────────────

def test_holiday_hit_default():
    assert holiday_for_date(_at(2026, 12, 25))[1] == "圣诞节"


def test_holiday_miss():
    assert holiday_for_date(_at(2026, 6, 3)) is None


def test_holiday_custom_calendar():
    out = holiday_for_date(_at(2026, 6, 1), {"06-01": "儿童节"})
    assert out == ("06-01", "儿童节")


# ── plan_milestone_rituals ───────────────────────────────────────────────

def test_plan_anniversary_fires():
    now = _at(2026, 4, 11, hour=10)
    first = now - 100 * 86400
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=first)], ritual_sent={}, opener_fn=_opener, now=now)
    assert len(plans) == 1
    p = plans[0]
    assert p["mode"] == "milestone_anniversary"
    assert p["event_type"] == "anniversary"
    assert p["ritual_key"] == "telegram:default:1:ms:anniversary:100"


def test_last_emotion_intensity_threaded_to_opener():
    # R：conv 的 last_emotion_intensity 透传进 milestone opener_fn
    seen = {}

    def _cap(**kw):
        seen["ei"] = kw.get("last_emotion_intensity")
        return {"mode": "milestone_anniversary", "directive": "x", "fact": ""}

    now = _at(2026, 4, 11, hour=10)
    conv = _conv(first_seen_ts=now - 100 * 86400, last_emotion_intensity=0.8)
    plan_milestone_rituals([conv], ritual_sent={}, opener_fn=_cap, now=now)
    assert seen.get("ei") == 0.8


def test_plan_holiday_fires():
    now = _at(2026, 12, 25, hour=10)
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["mode"] == "milestone_holiday"
    assert plans[0]["event_label"] == "圣诞节"
    assert plans[0]["ritual_key"] == "telegram:default:1:ms:holiday:2026:12-25"


def test_plan_anniversary_priority_over_holiday():
    # 认识 100 天恰逢圣诞 → 取纪念日（更高优先级）
    now = _at(2026, 12, 25, hour=10)
    first = now - 100 * 86400
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=first)], ritual_sent={}, opener_fn=_opener, now=now)
    assert plans[0]["event_type"] == "anniversary"


def test_plan_skips_wrong_hour():
    now = _at(2026, 12, 25, hour=15)
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_opener, now=now, greet_hour=10)
    assert plans == []


def test_plan_dedup_already_sent():
    now = _at(2026, 4, 11, hour=10)
    first = now - 100 * 86400
    key = "telegram:default:1:ms:anniversary:100"
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=first)], ritual_sent={key: now - 60},
        opener_fn=_opener, now=now)
    assert plans == []


def test_plan_low_intimacy_skipped():
    now = _at(2026, 12, 25, hour=10)
    plans = plan_milestone_rituals(
        [_conv(intimacy=10.0, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_opener, now=now, min_intimacy=30)
    assert plans == []


def test_plan_archived_and_blank_cid_skipped():
    now = _at(2026, 12, 25, hour=10)
    convs = [
        _conv(cid="telegram:default:a", archived=True, first_seen_ts=now - 5 * 86400),
        _conv(cid="", first_seen_ts=now - 5 * 86400),
    ]
    assert plan_milestone_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now) == []


def test_plan_no_event_no_plan():
    now = _at(2026, 6, 3, hour=10)  # 非节日
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 99 * 86400)], ritual_sent={},  # 非里程碑
        opener_fn=_opener, now=now)
    assert plans == []


def test_plan_pending_care_skipped():
    now = _at(2026, 12, 25, hour=10)
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_opener, now=now, has_pending_care=lambda cid: True)
    assert plans == []


def test_plan_blocked_opener_skipped():
    now = _at(2026, 12, 25, hour=10)

    def _blocked(**kw):
        return {"mode": "", "directive": "", "blocked": "crisis_severe"}

    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_blocked, now=now)
    assert plans == []


def test_plan_sorts_by_intimacy_and_caps():
    now = _at(2026, 12, 25, hour=10)
    convs = [
        _conv(cid=f"telegram:default:{i}", intimacy=float(i * 10),
              first_seen_ts=now - 5 * 86400)
        for i in range(1, 6)
    ]
    plans = plan_milestone_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now, max_per_tick=2)
    assert len(plans) == 2
    assert plans[0]["intimacy"] >= plans[1]["intimacy"]


def test_default_milestones_nonempty():
    assert 100 in DEFAULT_ANNIVERSARY_DAYS and 365 in DEFAULT_ANNIVERSARY_DAYS


# ── 生日（Stage Q）─────────────────────────────────────────────────────────

def test_plan_birthday_fires():
    now = _at(2026, 3, 5, hour=10)
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 50 * 86400)], ritual_sent={}, opener_fn=_opener,
        now=now, birthday_provider=lambda mk: (3, 5))
    assert len(plans) == 1
    assert plans[0]["mode"] == "milestone_birthday"
    assert plans[0]["event_type"] == "birthday"
    assert plans[0]["ritual_key"] == "telegram:default:1:ms:birthday:2026"


def test_plan_birthday_priority_over_anniversary_and_holiday():
    # 生日恰逢圣诞 + 认识 100 天 → 取生日（最高优先级）
    now = _at(2026, 12, 25, hour=10)
    first = now - 100 * 86400
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=first)], ritual_sent={}, opener_fn=_opener,
        now=now, birthday_provider=lambda mk: (12, 25))
    assert plans[0]["event_type"] == "birthday"


def test_plan_birthday_not_today_falls_through():
    # 生日不是今天 → 退回纪念日
    now = _at(2026, 4, 11, hour=10)
    first = now - 100 * 86400
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=first)], ritual_sent={}, opener_fn=_opener,
        now=now, birthday_provider=lambda mk: (8, 8))
    assert plans[0]["event_type"] == "anniversary"


def test_plan_birthday_provider_none_no_birthday():
    now = _at(2026, 3, 5, hour=10)
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 50 * 86400)], ritual_sent={}, opener_fn=_opener,
        now=now)  # 无 provider，非纪念日/节日 → 空
    assert plans == []


def test_plan_birthday_dedup():
    now = _at(2026, 3, 5, hour=10)
    key = "telegram:default:1:ms:birthday:2026"
    plans = plan_milestone_rituals(
        [_conv(first_seen_ts=now - 50 * 86400)], ritual_sent={key: now - 60},
        opener_fn=_opener, now=now, birthday_provider=lambda mk: (3, 5))
    assert plans == []
