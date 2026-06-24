"""Stage T：通用画像采集框架（profile_collect）纯函数单测。"""

from src.utils.profile_collect import (
    PROFILE_SLOTS,
    ask_directive,
    is_collectable,
    select_missing_slot,
    should_ask_profile_slot,
)

_DAY = 86400.0
_NOW = 1_700_000_000.0


# ── ask_directive ───────────────────────────────────────────────────────

def test_directive_birthday_has_keyword():
    d = ask_directive("birthday")
    assert "生日" in d


def test_directive_name_has_keyword():
    d = ask_directive("name")
    assert "称呼" in d or "叫" in d


def test_directive_warming_adds_restraint():
    base = ask_directive("name")
    warm = ask_directive("name", stage="warming")
    assert len(warm) > len(base)
    initial = ask_directive("name", stage="initial")
    assert initial == warm  # initial 与 warming 同样克制


def test_directive_unknown_slot_empty():
    assert ask_directive("city") == ""
    assert ask_directive("") == ""


def test_is_collectable():
    assert is_collectable("birthday")
    assert is_collectable("NAME")  # 大小写无关
    assert not is_collectable("city")


# ── should_ask_profile_slot ─────────────────────────────────────────────

def _ask(**kw):
    base = dict(
        opener_mode="gentle_checkin", intimacy=60.0, min_intimacy=45.0,
        slot_known=False, last_ask_ts=0.0, now=_NOW, cooldown_days=30.0)
    base.update(kw)
    return should_ask_profile_slot(**base)


def test_ask_happy_path():
    assert _ask() is True


def test_ask_only_on_gentle_checkin():
    assert _ask(opener_mode="follow_up") is False
    assert _ask(opener_mode="") is False


def test_ask_skips_when_known():
    assert _ask(slot_known=True) is False


def test_ask_requires_min_intimacy():
    assert _ask(intimacy=40.0) is False
    assert _ask(intimacy=45.0) is True  # 等于阈值即可


def test_ask_respects_cooldown():
    recent = _NOW - 10 * _DAY  # 距上次 10 天 < 30 天冷却
    assert _ask(last_ask_ts=recent) is False
    old = _NOW - 31 * _DAY
    assert _ask(last_ask_ts=old) is True


def test_ask_first_time_no_cooldown_block():
    assert _ask(last_ask_ts=0.0) is True


def test_ask_bad_intimacy_safe():
    assert _ask(intimacy="x") is False


# ── select_missing_slot ─────────────────────────────────────────────────

def test_select_first_missing():
    assert select_missing_slot(
        [("birthday", True), ("name", False)]) == "name"


def test_select_priority_order():
    assert select_missing_slot(
        [("birthday", False), ("name", False)]) == "birthday"


def test_select_all_known_none():
    assert select_missing_slot(
        [("birthday", True), ("name", True)]) is None


def test_select_empty_none():
    assert select_missing_slot([]) is None


def test_profile_slots_ordered():
    assert PROFILE_SLOTS[0] == "birthday"
    assert "name" in PROFILE_SLOTS
