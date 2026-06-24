"""Stage Q：生日抽取与当日判定（src.utils.birthday）——纯函数确定性测试。"""

from __future__ import annotations

import time

from src.utils.birthday import (
    birthday_fact_text,
    birthday_from_turn,
    extract_birthday,
    is_birthday_today,
    should_ask_birthday,
)


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


# ── should_ask_birthday（Stage R）─────────────────────────────────────────

_NOW = 1_700_000_000.0


def _ask(**over):
    base = dict(opener_mode="gentle_checkin", intimacy=60.0, min_intimacy=45.0,
                birthday_known=False, last_ask_ts=0.0, now=_NOW, cooldown_days=30.0)
    base.update(over)
    return should_ask_birthday(**base)


def test_ask_basic_true():
    assert _ask() is True


def test_ask_only_on_gentle_checkin():
    assert _ask(opener_mode="follow_up") is False
    assert _ask(opener_mode="ask_birthday") is False


def test_ask_skip_when_birthday_known():
    assert _ask(birthday_known=True) is False


def test_ask_skip_when_relationship_shallow():
    assert _ask(intimacy=30.0) is False


def test_ask_skip_within_cooldown():
    assert _ask(last_ask_ts=_NOW - 5 * 86400) is False  # 5 天前问过，30 天冷却内


def test_ask_ok_after_cooldown():
    assert _ask(last_ask_ts=_NOW - 40 * 86400) is True  # 40 天前问过，已过冷却


def test_ask_bad_intimacy_safe():
    assert _ask(intimacy="x") is False


# ── birthday_from_turn / birthday_fact_text（Stage S）─────────────────────

def test_turn_from_user_msg():
    assert birthday_from_turn("我生日是3月5日", "好的~") == (3, 5)


def test_turn_from_ai_confirm_reply():
    # 用户回裸日期（无关键词，路1不命中）；AI 回复确认（含关键词+日期）→ 路2命中
    assert birthday_from_turn("3月5号", "记住啦，你3月5号生日！") == (3, 5)


def test_turn_ask_reply_no_false_positive():
    # AI 的「提问」回复无日期 → 不会把用户无关日期误当生日
    assert birthday_from_turn("3月5号有个会", "顺便问下你生日哪天呀？") is None


def test_turn_none_when_no_birthday():
    assert birthday_from_turn("今天天气真好", "是呀~") is None


def test_fact_text_roundtrips():
    txt = birthday_fact_text(3, 5)
    assert "生日" in txt
    assert extract_birthday(txt) == (3, 5)  # 规范文案能被复解析（resolve_birthday 可用）


# ── _capture_birthday_fact（Stage S，集成）────────────────────────────────

import logging as _logging  # noqa: E402

from src.skills.skill_manager import SkillManager as _SMcls  # noqa: E402
from src.utils.episodic_memory_store import EpisodicMemoryStore  # noqa: E402


class _CapSM:
    _episodic_storage_key = _SMcls._episodic_storage_key
    resolve_birthday = _SMcls.resolve_birthday
    _capture_birthday_fact = _SMcls._capture_birthday_fact

    def __init__(self, store):
        self._episodic_store = store
        self._memory_cfg = {"scope": "user"}
        self._cpi = None
        self.logger = _logging.getLogger("test_capture")

    async def _episodic_patch_embedding(self, rid, fact):
        return None


async def test_capture_writes_birthday_fact(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    sm = _CapSM(store)
    await sm._capture_birthday_fact("u1", "我生日是3月5日", "好的~", chat_id="u1")
    assert sm.resolve_birthday("u1") == (3, 5)


async def test_capture_idempotent_same_birthday(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    sm = _CapSM(store)
    await sm._capture_birthday_fact("u1", "我生日是3月5日", "", chat_id="u1")
    await sm._capture_birthday_fact("u1", "对，3月5日生日", "", chat_id="u1")
    rows = [r for r in store.list_rows(prefix="u1", limit=50)
            if "生日" in str(r.get("content") or "")]
    assert len(rows) == 1  # 相同生日不重复落库


async def test_capture_noop_when_no_birthday(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    sm = _CapSM(store)
    await sm._capture_birthday_fact("u1", "今天天气好", "是呀", chat_id="u1")
    assert sm.resolve_birthday("u1") is None
