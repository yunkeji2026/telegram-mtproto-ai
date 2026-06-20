"""P1 主动话题发起：select_proactive_topic / build_proactive_topic_block 选择逻辑。

覆盖：沉默闸门、只回访高置信事实（排除 ai_inferred / stale）、稳定层/复发/新鲜度排序、
无记忆退化温和问候、长别离柔和重连、新关系克制修饰、block 装配，以及 SkillManager 接线。
"""

from __future__ import annotations

import time

from src.utils.proactive_topic import (
    MODE_FOLLOW_UP,
    MODE_GENTLE_CHECKIN,
    MODE_NONE,
    build_proactive_topic_block,
    select_proactive_topic,
)


def _fact(content, source="user_stated", tier="raw", hits=1, last_seen=None):
    return {
        "content": content, "source": source, "tier": tier, "hits": hits,
        "last_seen": last_seen if last_seen is not None else time.time(),
    }


# ── 沉默闸门 ────────────────────────────────────────────────────────────

def test_silent_too_short_no_topic():
    out = select_proactive_topic([_fact("在备考")], silent_hours=2)
    assert out["mode"] == MODE_NONE


def test_silent_enough_triggers():
    out = select_proactive_topic([_fact("在备考")], silent_hours=48)
    assert out["mode"] == MODE_FOLLOW_UP


def test_invalid_silent_hours_safe():
    out = select_proactive_topic([_fact("x")], silent_hours="bad")  # type: ignore[arg-type]
    assert out["mode"] == MODE_NONE


# ── 只回访高置信事实 ────────────────────────────────────────────────────

def test_excludes_ai_inferred():
    facts = [_fact("可能在创业", source="ai_inferred", hits=9)]
    out = select_proactive_topic(facts, silent_hours=48)
    # 唯一事实是 AI 推断 → 不回访它，退化温和问候
    assert out["mode"] == MODE_GENTLE_CHECKIN
    assert out["fact"] == ""


def test_excludes_stale():
    facts = [_fact("住旧城", tier="stale", hits=9)]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["mode"] == MODE_GENTLE_CHECKIN


def test_prefers_user_stated_over_inferred():
    facts = [
        _fact("可能喜欢猫", source="ai_inferred", tier="stable", hits=20),
        _fact("在准备考研", source="user_stated", tier="raw", hits=1),
    ]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["mode"] == MODE_FOLLOW_UP
    assert out["fact"] == "在准备考研"  # 不选高分但属推断的


def test_missing_source_treated_as_user_stated():
    facts = [{"content": "养了只狗", "tier": "raw", "hits": 2}]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "养了只狗"


# ── 排序：稳定 > 复发 > 新鲜 ────────────────────────────────────────────

def test_prefers_stable_tier():
    facts = [
        _fact("普通事A", tier="raw", hits=5),
        _fact("核心事B", tier="stable", hits=1),
    ]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "核心事B"


def test_prefers_more_hits_within_same_tier():
    facts = [
        _fact("提过一次", tier="raw", hits=1),
        _fact("常提起", tier="raw", hits=8),
    ]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "常提起"


def test_prefers_recent_on_tie():
    old = _fact("旧事", tier="raw", hits=3, last_seen=time.time() - 1e6)
    new = _fact("近事", tier="raw", hits=3, last_seen=time.time())
    out = select_proactive_topic([old, new], silent_hours=48)
    assert out["fact"] == "近事"


# ── 退化 / 长别离 / 克制 ────────────────────────────────────────────────

def test_no_facts_gentle_checkin():
    out = select_proactive_topic([], silent_hours=72)
    assert out["mode"] == MODE_GENTLE_CHECKIN
    assert "最近" in out["directive"]


def test_long_absence_flag_and_soft_directive():
    out = select_proactive_topic([_fact("在备考")], silent_hours=20 * 24)
    assert out["long_absence"] is True
    assert "久违" in out["directive"]


def test_short_silence_followup_not_long_absence():
    out = select_proactive_topic([_fact("在备考")], silent_hours=48)
    assert out["long_absence"] is False
    assert "久违" not in out["directive"]


def test_new_relationship_restraint_note():
    out = select_proactive_topic(
        [_fact("在备考")], silent_hours=48, stage="warming",
    )
    assert "关系还偏新" in out["directive"]


def test_steady_relationship_no_restraint():
    out = select_proactive_topic(
        [_fact("在备考")], silent_hours=48, stage="steady",
    )
    assert "关系还偏新" not in out["directive"]


# ── P1b: 背景事实 context_facts ────────────────────────────────────────

def test_context_facts_excludes_chosen_and_ranks():
    facts = [
        _fact("在备考", tier="stable", hits=5),     # 选中（最高分）
        _fact("养了只猫", tier="raw", hits=4),
        _fact("下月搬家", tier="raw", hits=2),
    ]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "在备考"
    # 背景含其余高置信事实、不含选中项、按优先级排序
    assert out["context_facts"] == ["养了只猫", "下月搬家"]


def test_context_facts_limit():
    facts = [_fact(f"事{i}", hits=10 - i) for i in range(5)]
    out = select_proactive_topic(facts, silent_hours=48, max_context_facts=2)
    assert len(out["context_facts"]) == 2


def test_context_facts_zero_disables():
    facts = [_fact("a", hits=3), _fact("b", hits=2)]
    out = select_proactive_topic(facts, silent_hours=48, max_context_facts=0)
    assert out["context_facts"] == []


def test_context_facts_excludes_ai_inferred():
    facts = [
        _fact("在备考", hits=5),
        _fact("可能在创业", source="ai_inferred", tier="stable", hits=20),
    ]
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "在备考"
    assert out["context_facts"] == []  # 推断项不入背景


def test_context_facts_empty_on_gentle_checkin():
    out = select_proactive_topic([], silent_hours=72)
    assert out["mode"] == MODE_GENTLE_CHECKIN
    assert out["context_facts"] == []


# ── block 装配 ──────────────────────────────────────────────────────────

def test_block_empty_when_no_topic():
    assert build_proactive_topic_block([_fact("x")], silent_hours=1) == ""


def test_block_wraps_directive():
    block = build_proactive_topic_block([_fact("在备考")], silent_hours=48)
    assert block.startswith("【主动话题】")
    assert "在备考" in block


# ── SkillManager 接线 ───────────────────────────────────────────────────

class _StubStore:
    def __init__(self, rows):
        self._rows = rows

    def list_rows(self, *, prefix="", limit=50, source=""):
        return list(self._rows)


class _SM:
    build_proactive_opener = (
        __import__("src.skills.skill_manager", fromlist=["SkillManager"])
        .SkillManager.build_proactive_opener
    )

    def __init__(self, store):
        self._episodic_store = store


def test_skill_manager_opener_from_store():
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]))
    out = sm.build_proactive_opener("u1", silent_hours=48)
    assert out["mode"] == MODE_FOLLOW_UP
    assert out["fact"] == "在学吉他"


def test_skill_manager_opener_no_store():
    sm = _SM(None)
    assert sm.build_proactive_opener("u1", silent_hours=48)["mode"] == ""


def test_skill_manager_opener_blank_key():
    sm = _SM(_StubStore([_fact("x")]))
    assert sm.build_proactive_opener("", silent_hours=48)["mode"] == ""
