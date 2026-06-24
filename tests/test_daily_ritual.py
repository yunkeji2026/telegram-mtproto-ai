"""Stage L 每日仪式感主动问候：daily_ritual 纯函数 + 循环集成。"""

from __future__ import annotations

import time

import pytest

from src.integrations.companion_proactive import (
    CompanionProactiveLoop,
    JsonCooldownStore,
)
from src.utils.daily_ritual import (
    MORNING,
    NIGHT,
    current_slot,
    infer_active_hour,
    plan_daily_rituals,
    window_hours,
)

_H = 3600.0


def _at(hour, *, minute=0, day=19, month=6, year=2026):
    """构造本地时区某天某小时的时间戳（localtime(ts).tm_hour == hour）。"""
    return time.mktime(time.struct_time(
        (year, month, day, hour, minute, 0, 0, 0, -1)))


def _daykey(ts):
    return time.strftime("%Y%m%d", time.localtime(ts))


def _conv(cid, *, intimacy=40.0, last_ts=None, archived=False,
          memory_key="u:1", stage="steady", now=None):
    if last_ts is None:
        last_ts = (now or time.time()) - 48 * _H
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "acc1",
        "chat_key": "123", "last_ts": last_ts, "last_direction": "out",
        "archived": archived, "memory_key": memory_key, "stage": stage,
        "intimacy": intimacy, "last_emotion": "",
    }


def _opener(*, slot, memory_key, stage, intimacy, last_emotion="", contact_key=""):
    return {"mode": f"ritual_{slot}", "directive": f"{slot} 问候", "fact": ""}


def _opener_block(**_kw):
    return {"mode": "", "directive": "", "blocked": "crisis_severe"}


# ── window_hours ─────────────────────────────────────────────────────────────

def test_window_hours_basic():
    assert window_hours((7, 10), default_start=7, default_end=10) == [7, 8, 9]
    assert window_hours((21, 24), default_start=21, default_end=24) == [21, 22, 23]


def test_window_hours_wrap_midnight():
    assert window_hours((23, 2), default_start=7, default_end=10) == [23, 0, 1]


def test_window_hours_bad_falls_back_to_default():
    assert window_hours(None, default_start=7, default_end=10) == [7, 8, 9]
    assert window_hours(("x",), default_start=21, default_end=24) == [21, 22, 23]


# ── current_slot ─────────────────────────────────────────────────────────────

def test_current_slot_morning_night_neither():
    assert current_slot(8, morning_window=(7, 10), night_window=(21, 24)) == MORNING
    assert current_slot(22, morning_window=(7, 10), night_window=(21, 24)) == NIGHT
    assert current_slot(14, morning_window=(7, 10), night_window=(21, 24)) is None


# ── infer_active_hour ────────────────────────────────────────────────────────

def test_infer_active_hour_morning_picks_modal_earliest_on_tie():
    # 7 与 8 各两次（并列）→ 晨档取最早 7
    assert infer_active_hour([7, 7, 8, 8, 14], MORNING) == 7


def test_infer_active_hour_night_picks_modal_latest_on_tie():
    # 21 与 23 各两次（并列）→ 晚档取最晚 23
    assert infer_active_hour([21, 21, 23, 23, 9], NIGHT) == 23


def test_infer_active_hour_no_samples_in_band():
    assert infer_active_hour([13, 14, 15], MORNING) is None
    assert infer_active_hour([], NIGHT) is None


# ── plan_daily_rituals：基础 ──────────────────────────────────────────────────

def test_morning_greeting_at_window_start_without_provider():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["slot"] == MORNING
    assert plans[0]["mode"] == "ritual_morning"
    assert plans[0]["ritual_key"] == f"c1:{_daykey(now)}:morning"


def test_night_greeting():
    now = _at(21)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["slot"] == NIGHT


def test_non_ritual_hour_returns_empty():
    now = _at(14)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert plans == []


# ── plan_daily_rituals：护栏 ──────────────────────────────────────────────────

def test_low_intimacy_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", intimacy=5.0, now=now)], ritual_sent={},
        opener_fn=_opener, now=now, min_intimacy=20.0)
    assert plans == []


def test_already_greeted_this_slot_today_skipped():
    now = _at(7)
    key = f"c1:{_daykey(now)}:morning"
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={key: now - 1 * _H},
        opener_fn=_opener, now=now)
    assert plans == []


def test_recent_interaction_skipped():
    now = _at(7)
    # 1 小时前刚聊过 → 不必道早安（gap < 3h）
    plans = plan_daily_rituals(
        [_conv("c1", last_ts=now - 1 * _H, now=now)], ritual_sent={},
        opener_fn=_opener, now=now, min_quiet_gap_hours=3.0)
    assert plans == []


def test_pending_care_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now,
        has_pending_care=lambda cid: cid == "c1")
    assert plans == []


def test_archived_and_blank_cid_skipped():
    now = _at(7)
    convs = [_conv("c1", archived=True, now=now), _conv("", now=now)]
    plans = plan_daily_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now)
    assert plans == []


def test_crisis_blocked_opener_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener_block, now=now)
    assert plans == []


# ── plan_daily_rituals：个性化择时 ───────────────────────────────────────────

def test_personalized_hour_fires_only_at_inferred_hour():
    # 历史活跃在 8 点 → 目标 8 点；当前 7 点（窗口内但非目标）→ 不发
    now7 = _at(7)
    plans7 = plan_daily_rituals(
        [_conv("c1", now=now7)], ritual_sent={}, opener_fn=_opener, now=now7,
        active_hours_provider=lambda cid: [8, 8, 8])
    assert plans7 == []
    # 当前 8 点 == 目标 → 发
    now8 = _at(8)
    plans8 = plan_daily_rituals(
        [_conv("c1", now=now8)], ritual_sent={}, opener_fn=_opener, now=now8,
        active_hours_provider=lambda cid: [8, 8, 8])
    assert len(plans8) == 1


def test_personalized_no_samples_falls_back_to_window_start():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now,
        active_hours_provider=lambda cid: [])
    assert len(plans) == 1  # 无历史 → 退回窗口起点 7 点，照常发


def test_personalized_inferred_outside_window_uses_window_start():
    # 推断活跃点 5 点（晨带内但不在问候窗口 [7,10)）→ 退回窗口起点 7
    now7 = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now7)], ritual_sent={}, opener_fn=_opener, now=now7,
        active_hours_provider=lambda cid: [5, 5, 5])
    assert len(plans) == 1


# ── plan_daily_rituals：排序/截断 ────────────────────────────────────────────

def test_max_per_tick_and_intimacy_desc():
    now = _at(7)
    convs = [
        _conv("c1", intimacy=30.0, now=now),
        _conv("c2", intimacy=80.0, now=now),
        _conv("c3", intimacy=50.0, now=now),
    ]
    plans = plan_daily_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now, max_per_tick=2)
    assert [p["conversation_id"] for p in plans] == ["c2", "c3"]


# ── 循环集成 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_sends_ritual_and_marks_ritual_cooldown(tmp_path):
    now = _at(7)
    sent = []

    async def _send(plan):
        sent.append(plan["conversation_id"])
        return True

    silence_cd = JsonCooldownStore(tmp_path / "cd.json")
    ritual_cd = JsonCooldownStore(tmp_path / "ritual.json")
    convs = [_conv("c1", now=now)]

    def _ritual_fn(cs, now_ts):
        return plan_daily_rituals(
            cs, ritual_sent=ritual_cd.snapshot(), opener_fn=_opener, now=now_ts)

    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=lambda **_k: {"mode": "", "directive": ""},  # 沉默路径不出
        send_fn=_send,
        cooldown_store=silence_cd,
        ritual_fn=_ritual_fn,
        ritual_cooldown=ritual_cd,
        now=lambda: now,
    )
    res = await loop.run_once()
    assert res == {"planned": 1, "sent": 1}
    assert sent == ["c1"]
    # 仪式冷却被记（每日每档键），沉默冷却未被记
    assert ritual_cd.snapshot().get(f"c1:{_daykey(now)}:morning") == now
    assert silence_cd.snapshot() == {}
    # 再跑一次：当日该档已发 → 不再发
    res2 = await loop.run_once()
    assert res2 == {"planned": 0, "sent": 0}


@pytest.mark.asyncio
async def test_loop_ritual_takes_priority_over_silence(tmp_path):
    """同一会话本 tick 既到仪式点又够沉默 → 只发一次（仪式优先）。"""
    now = _at(9)  # 9 点：非安静时段，沉默路径也会命中
    sent = []

    async def _send(plan):
        sent.append((plan["conversation_id"], plan["mode"]))
        return True

    silence_cd = JsonCooldownStore(tmp_path / "cd.json")
    ritual_cd = JsonCooldownStore(tmp_path / "ritual.json")
    convs = [_conv("c1", now=now)]

    def _silence_opener(*, memory_key, silent_hours, stage, intimacy, **_kw):
        return {"mode": "follow_up", "directive": "回访", "fact": "x"}

    def _ritual_fn(cs, now_ts):
        return plan_daily_rituals(
            cs, ritual_sent=ritual_cd.snapshot(), opener_fn=_opener, now=now_ts,
            morning_window=(9, 10))  # 让目标点 == 9

    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_silence_opener,
        send_fn=_send,
        cooldown_store=silence_cd,
        ritual_fn=_ritual_fn,
        ritual_cooldown=ritual_cd,
        now=lambda: now,
    )
    res = await loop.run_once()
    assert res["sent"] == 1
    assert sent == [("c1", "ritual_morning")]  # 仪式优先，不重复打扰
