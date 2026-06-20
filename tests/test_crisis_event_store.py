"""R9 危机事件落库/审计：CrisisEventStore + SkillManager 审计接线 + R8 等级清零修复。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.utils.crisis_event_store import CrisisEventStore


@pytest.fixture
def store(tmp_path):
    s = CrisisEventStore(tmp_path / "bot.db")
    yield s
    s.close()


# ── store 基础 ──────────────────────────────────────────────────────────

def test_record_and_list(store):
    rid = store.record(
        user_id="u1", level="severe", category="self_harm",
        streak=2, escalated=True, safety_override=True, excerpt="我不想活了",
    )
    assert rid is not None
    rows = store.list_recent()
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "u1" and r["level"] == "severe"
    assert r["escalated"] is True and r["safety_override"] is True
    assert r["handled"] is False


def test_excerpt_truncated(store):
    store.record(user_id="u1", level="elevated", excerpt="x" * 500)
    assert len(store.list_recent()[0]["excerpt"]) == 120


def test_only_unhandled_and_mark(store):
    a = store.record(user_id="u1", level="severe")
    store.record(user_id="u2", level="elevated")
    assert store.count() == 2
    assert store.count(only_unhandled=True) == 2
    assert store.mark_handled(a, handled_by="agent7", note="已电话联系") is True
    assert store.count(only_unhandled=True) == 1
    handled = [r for r in store.list_recent() if r["id"] == a][0]
    assert handled["handled"] is True and handled["handled_by"] == "agent7"


def test_user_prefix_filter(store):
    store.record(user_id="-100_1", level="severe")
    store.record(user_id="-200_2", level="severe")
    assert len(store.list_recent(user_prefix="-100")) == 1


def test_mark_handled_missing_returns_false(store):
    assert store.mark_handled(999) is False


# ── SkillManager 审计接线 ───────────────────────────────────────────────

def _sm(store, wellbeing_cfg):
    from src.skills.skill_manager import SkillManager
    sm = SkillManager.__new__(SkillManager)
    sm.config = SimpleNamespace(config={"companion": {"wellbeing": wellbeing_cfg}})
    sm._crisis_escalation_cooldown = {}
    sm._crisis_store = store
    return sm


def test_audit_records_when_enabled(store):
    sm = _sm(store, {"enabled": True, "crisis_audit": True})
    ctx = {"_wellbeing_crisis_level": "severe", "last_message": "我不想活了"}
    sm._maybe_escalate_crisis(user_id="u1", chat_id=1, user_context=ctx, log_prefix="")
    assert store.count() == 1
    assert store.list_recent()[0]["level"] == "severe"


def test_audit_off_records_nothing(store):
    sm = _sm(store, {"enabled": True, "crisis_audit": False})
    ctx = {"_wellbeing_crisis_level": "severe"}
    sm._maybe_escalate_crisis(user_id="u1", chat_id=1, user_context=ctx, log_prefix="")
    assert store.count() == 0


def test_audit_skips_non_crisis(store):
    sm = _sm(store, {"enabled": True, "crisis_audit": True})
    ctx = {"_wellbeing_crisis_level": "none"}
    sm._maybe_escalate_crisis(user_id="u1", chat_id=1, user_context=ctx, log_prefix="")
    assert store.count() == 0


def test_audit_captures_escalation_and_override(store):
    sm = _sm(store, {
        "enabled": True, "crisis_audit": True,
        "crisis_escalation": True, "escalate_after": 1,
    })
    ctx = {
        "_wellbeing_crisis_level": "severe",
        "_wellbeing_safety_override": True,
    }
    sm._maybe_escalate_crisis(user_id="u1", chat_id=1, user_context=ctx, log_prefix="")
    r = store.list_recent()[0]
    assert r["escalated"] is True
    assert r["safety_override"] is True
    # override 本轮信号已被读后清零
    assert "_wellbeing_safety_override" not in ctx


# ── R8 修复：平静轮危机等级清零（emotional_context 每轮回写 none）─────────

def test_calm_turn_resets_crisis_level():
    from src.utils.emotional_context import build_emotional_context_block
    ctx: dict = {}
    build_emotional_context_block("我不想活了", ctx)
    assert ctx.get("_wellbeing_crisis_level") == "severe"
    # 下一轮平静消息 → 等级回 none（不再粘住）
    build_emotional_context_block("今天天气真好我们去玩吧", ctx)
    assert ctx.get("_wellbeing_crisis_level") == "none"
