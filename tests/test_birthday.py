"""Stage Q：生日抽取与当日判定（src.utils.birthday）——纯函数确定性测试。"""

from __future__ import annotations

import time

from src.utils.birthday import extract_birthday, is_birthday_today


def _at(year, month, day):
    return time.mktime((year, month, day, 10, 0, 0, 0, 0, -1))


# ── extract_birthday ─────────────────────────────────────────────────────

def test_extract_cn_month_day():
    assert extract_birthday("我生日是3月5日") == (3, 5)
    assert extract_birthday("生日：12月25号") == (12, 25)


def test_extract_cn_with_year():
    assert extract_birthday("出生于1995-03-05") == (3, 5)
    assert extract_birthday("生日 1990年7月8日") == (7, 8)


def test_extract_bare_md():
    assert extract_birthday("生日 03-05") == (3, 5)
    assert extract_birthday("生日是 7/8 哦") == (7, 8)


def test_extract_english():
    assert extract_birthday("my birthday is March 5") == (3, 5)
    assert extract_birthday("born on Dec 25") == (12, 25)


def test_no_keyword_no_extract():
    # 没有生日关键词 → 不解析（避免把闲聊日期误当生日）
    assert extract_birthday("3月5日开会") is None
    assert extract_birthday("我们1995-03-05认识的") is None


def test_invalid_date_rejected():
    assert extract_birthday("生日是13月40日") is None


def test_empty_and_none():
    assert extract_birthday("") is None
    assert extract_birthday(None) is None


# ── is_birthday_today ────────────────────────────────────────────────────

def test_is_birthday_today_match():
    assert is_birthday_today((3, 5), _at(2026, 3, 5)) is True


def test_is_birthday_today_no_match():
    assert is_birthday_today((3, 5), _at(2026, 3, 6)) is False


def test_leap_day_birthday_on_common_year():
    # 2/29 生日在平年（2026）顺延到 2/28 庆祝
    assert is_birthday_today((2, 29), _at(2026, 2, 28)) is True
    # 闰年（2028）则正日庆
    assert is_birthday_today((2, 29), _at(2028, 2, 29)) is True
    assert is_birthday_today((2, 29), _at(2028, 2, 28)) is False


def test_is_birthday_today_bad_input():
    assert is_birthday_today(None, _at(2026, 3, 5)) is False
    assert is_birthday_today((13, 40), _at(2026, 3, 5)) is False
    assert is_birthday_today((3,), _at(2026, 3, 5)) is False
