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


# ── prefer_category（Phase ④：优先回访剧情共享经历） ──────────────────────

def _cat_fact(content, category, hits=1, tier="raw"):
    f = _fact(content, hits=hits, tier=tier)
    f["category"] = category
    return f


def test_prefer_category_lifts_story_memory():
    facts = [
        _cat_fact("在备考", "llm", hits=20, tier="stable"),       # 高分但非偏好类目
        _cat_fact("我们一起看过星空", "story", hits=1, tier="raw"),
    ]
    # 不偏好 → 选高分的备考
    assert select_proactive_topic(facts, silent_hours=48)["fact"] == "在备考"
    # 偏好 story → 共享经历领先一档，被优先回访
    out = select_proactive_topic(facts, silent_hours=48, prefer_category="story")
    assert out["fact"] == "我们一起看过星空"


def test_prefer_category_no_match_falls_back_to_score():
    facts = [_cat_fact("在备考", "llm", hits=5), _cat_fact("养了猫", "heuristic", hits=2)]
    # 无 story 类目 → 退回普通排序（高 hits 优先）
    out = select_proactive_topic(facts, silent_hours=48, prefer_category="story")
    assert out["fact"] == "在备考"


def test_prefer_category_default_unchanged():
    facts = [_cat_fact("我们一起看过星空", "story", hits=1),
             _cat_fact("在备考", "llm", hits=9)]
    # 默认不传 prefer_category → 行为等同旧版（按分数选）
    out = select_proactive_topic(facts, silent_hours=48)
    assert out["fact"] == "在备考"


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


class _StubCtxStore:
    """极简 ContextStore：按 key 返回预置的持久化 user_context。"""

    def __init__(self, by_key=None):
        self._by_key = by_key or {}

    def get(self, user_id):
        return self._by_key.get(str(user_id), {})


import logging as _logging  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

_SMcls = (
    __import__("src.skills.skill_manager", fromlist=["SkillManager"]).SkillManager
)


class _SM:
    build_proactive_opener = _SMcls.build_proactive_opener
    build_ritual_opener = _SMcls.build_ritual_opener
    build_milestone_opener = _SMcls.build_milestone_opener
    resolve_birthday = _SMcls.resolve_birthday
    _proactive_story_invite = _SMcls._proactive_story_invite
    _proactive_story_teaser = _SMcls._proactive_story_teaser
    _story_progress_from_context = staticmethod(_SMcls._story_progress_from_context)
    _story_cfg = _SMcls._story_cfg
    _story_scenarios = _SMcls._story_scenarios
    _story_bonus_cap = _SMcls._story_bonus_cap
    _scenario_title = staticmethod(_SMcls._scenario_title)
    _proactive_emotion_gate = _SMcls._proactive_emotion_gate
    _proactive_crisis_window_days = _SMcls._proactive_crisis_window_days

    def __init__(self, store, *, story_cfg=None, context=None, crisis_latest=None):
        self._episodic_store = store
        self.logger = _logging.getLogger("test_proactive")
        _comp = {}
        if story_cfg is not None:
            _comp["story"] = story_cfg
        self.config = _NS(config=({"companion": _comp} if _comp else {}))
        self._context_store = _StubCtxStore(context)
        self._crisis_latest = crisis_latest
        self._crisis_store = object() if crisis_latest is not None else None

    def crisis_summary_for_user(self, key, *, limit=5):
        return {"latest": self._crisis_latest}


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


# ── Phase ④续⁵ 主动剧情邀约 ─────────────────────────────────────────────

_STORY_CFG = {
    "enabled": True,
    "max_intimacy_bonus": 12,
    "scenarios": {
        "coffee_date": {"title": "初次咖啡约会", "min_bond_level": 2,
                        "beats": [{"id": "a", "directive": "x"}]},
        "starry_night": {"title": "星空下的约定", "min_bond_level": 3,
                         "require_unlock": "all_story",
                         "beats": [{"id": "a", "directive": "x"}]},
    },
}


def test_proactive_invites_available_free_story():
    # intimacy 50 → bond level 2 ≥ coffee_date 门槛；未完成 → 邀约
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable")]),
             story_cfg=_STORY_CFG, context={"u1": {}})
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == "story_invite"
    assert out["fact"] == "初次咖啡约会"
    assert out["scenario_id"] == "coffee_date"
    assert "初次咖啡约会" in out["directive"]
    assert out["silent_hours"] == 48.0


def test_proactive_invite_skips_completed_falls_back_to_memory():
    # 已完成 coffee_date（starry_night 付费被排除）→ 无可邀约 → 回落记忆话题
    ctx = {"u1": {"companion_relationship": {"u1": {"story_done": ["coffee_date"]}}}}
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_STORY_CFG, context=ctx)
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == MODE_FOLLOW_UP
    assert out["fact"] == "在学吉他"


def test_proactive_invite_respects_bond_level():
    # intimacy 10 → bond level 不足 coffee_date(2) → 无邀约 → 回落记忆
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable")]),
             story_cfg=_STORY_CFG, context={"u1": {}})
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=10.0)
    assert out["mode"] == MODE_FOLLOW_UP


def test_proactive_invite_effective_intimacy_unlocks():
    # 基础 intimacy 38 略低，但剧情累计加成把 effective 顶过 level2 门槛 → 可邀约
    ctx = {"u1": {"companion_relationship": {"u1": {"story_bonus": 10}}}}
    sm = _SM(_StubStore([_fact("x")]), story_cfg=_STORY_CFG, context=ctx)
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=38.0)
    assert out["mode"] == "story_invite"


def test_proactive_invite_disabled_flag():
    cfg = dict(_STORY_CFG, proactive_invite=False)
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable")]),
             story_cfg=cfg, context={"u1": {}})
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == MODE_FOLLOW_UP


def test_proactive_invite_disabled_when_story_off():
    cfg = dict(_STORY_CFG, enabled=False)
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable")]),
             story_cfg=cfg, context={"u1": {}})
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == MODE_FOLLOW_UP


# ── Phase ④续⁶ 个性化召回（续作邀约引用前传共同经历） ────────────────────

_SEQUEL_CFG = {
    "enabled": True,
    "max_intimacy_bonus": 12,
    "scenarios": {
        "coffee_date": {
            "title": "初次咖啡约会", "min_bond_level": 2,
            "beats": [{"id": "a", "directive": "x"}],
            "endings": {"warm": {"directive": "y", "memory": "我们约好下次再一起喝咖啡"}},
        },
        "starry_seq": {
            "title": "星空下的约定", "min_bond_level": 2,
            "requires_story": [{"scenario": "coffee_date", "ending": "warm"}],
            "beats": [{"id": "a", "directive": "x"}],
        },
    },
}


def test_proactive_gate_blocks_all_on_recent_severe():
    import time as _t
    crisis = {"level": "severe", "created_at": _t.time() - 86400}  # 1 天前
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_STORY_CFG, context={"u1": {}}, crisis_latest=crisis)
    # 近期 severe 危机 → 完全不主动（连记忆问候都不发），但带危机升级信号
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == ""
    assert out.get("blocked") == "crisis_severe"  # Phase ④续⁸：交派发层转关怀


def test_proactive_gate_soft_suppresses_invite_keeps_memory():
    import time as _t
    crisis = {"level": "elevated", "created_at": _t.time() - 86400}
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_STORY_CFG, context={"u1": {}}, crisis_latest=crisis)
    # elevated → 抑制剧情邀约，但温和记忆问候仍可
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == MODE_FOLLOW_UP
    assert out["fact"] == "在学吉他"


def test_proactive_gate_none_when_crisis_stale():
    import time as _t
    crisis = {"level": "severe", "created_at": _t.time() - 40 * 86400}  # 40 天前
    sm = _SM(_StubStore([_fact("x")]),
             story_cfg=_STORY_CFG, context={"u1": {}}, crisis_latest=crisis)
    # 窗口外（默认 14 天）→ 不抑制 → 仍可邀约
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == "story_invite"


def test_proactive_gate_soft_on_negative_last_emotion_no_crisis():
    """Phase ④续⁹：无危机事件，但末条情绪为中文负面（焦虑）→ soft 抑邀约、留记忆问候。"""
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_STORY_CFG, context={"u1": {}})
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, last_emotion="焦虑")
    assert out["mode"] == MODE_FOLLOW_UP
    assert out["fact"] == "在学吉他"


def test_proactive_gate_none_on_positive_last_emotion():
    """末条情绪为正面/中性（感谢）→ 不抑制 → 仍可剧情邀约。"""
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable")]),
             story_cfg=_STORY_CFG, context={"u1": {}})
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, last_emotion="感谢")
    assert out["mode"] == "story_invite"


def test_proactive_sequel_invite_references_prerequisite():
    # 已以 warm 结局完成 coffee_date → 续作 starry_seq 解锁 → 邀约话术回忆前传
    ctx = {"u1": {"companion_relationship": {"u1": {
        "story_done": ["coffee_date"],
        "story_outcomes": {"coffee_date": "warm"},
    }}}}
    sm = _SM(_StubStore([_fact("x")]), story_cfg=_SEQUEL_CFG, context=ctx)
    out = sm.build_proactive_opener("u1", silent_hours=72, intimacy=50.0)
    assert out["mode"] == "story_invite"
    assert out["scenario_id"] == "starry_seq"
    # 个性化：directive 同时带上前传标题 + 那次结局回写的共享经历
    assert "初次咖啡约会" in out["directive"]
    assert "我们约好下次再一起喝咖啡" in out["directive"]
    assert "续作" in out["directive"]


# ── Stage 2 付费解锁预告 _proactive_story_teaser ──────────────────────────

import pytest as _pytest  # noqa: E402
from src.utils import companion_context as _cc  # noqa: E402

# coffee_date 免费(bond2)；beach_trip 付费仅差 story_ch1（bond2 已满足）。
_TEASER_CFG = {
    "enabled": True,
    "max_intimacy_bonus": 12,
    "paid_teaser": True,
    "scenarios": {
        "coffee_date": {"title": "初次咖啡约会", "min_bond_level": 2,
                        "beats": [{"id": "a", "directive": "x"}]},
        "beach_trip": {"title": "海边之旅", "min_bond_level": 2,
                       "require_unlock": "story_ch1",
                       "beats": [{"id": "a", "directive": "x"}]},
    },
}

# coffee_date 已完成的 context → 免费邀约用尽 → 走付费预告分支。
_DONE_FREE_CTX = {"u1": {"companion_relationship": {"u1": {"story_done": ["coffee_date"]}}}}


@_pytest.fixture
def _free_ent():
    """注册一个「免费、无任何 grant」的权益 resolver；用例结束后清空。"""
    _cc.set_relationship_providers(
        entitlement_resolver=lambda ck: {"tier": "free", "grants": [], "unlocked": []})
    try:
        yield
    finally:
        _cc.reset_relationship_providers()


def test_proactive_teaser_when_only_paid_left(_free_ent):
    # 免费 coffee_date 已完成 → 无免费可邀约 → 付费预告 beach_trip（只差 story_ch1）
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_TEASER_CFG, context=_DONE_FREE_CTX)
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "story_teaser"
    assert out["scenario_id"] == "beach_trip"
    assert out["feature"] == "story_ch1"
    assert "海边之旅" in out["directive"]
    assert out["silent_hours"] == 48.0


def test_proactive_free_invite_beats_teaser(_free_ent):
    # coffee_date 未完成且免费可邀约 → 免费邀约优先于付费预告
    sm = _SM(_StubStore([_fact("x")]), story_cfg=_TEASER_CFG, context={"u1": {}})
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == "story_invite"
    assert out["scenario_id"] == "coffee_date"


def test_proactive_teaser_excludes_already_unlocked():
    # 已解锁 story_ch1 → beach_trip 变 available（非 need_unlock）→ 不预告 → 回落记忆
    _cc.set_relationship_providers(
        entitlement_resolver=lambda ck: {"grants": ["story_ch1"], "unlocked": []})
    try:
        sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
                 story_cfg=_TEASER_CFG, context=_DONE_FREE_CTX)
        out = sm.build_proactive_opener(
            "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1")
        assert out["mode"] == MODE_FOLLOW_UP
        assert out["fact"] == "在学吉他"
    finally:
        _cc.reset_relationship_providers()


def test_proactive_teaser_disabled_flag(_free_ent):
    # paid_teaser=False → 不预告 → 回落记忆
    cfg = dict(_TEASER_CFG, paid_teaser=False)
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=cfg, context=_DONE_FREE_CTX)
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == MODE_FOLLOW_UP


def test_proactive_teaser_requires_contact_key(_free_ent):
    # 无 contact_key → 无法解析端用户权益 → 不预告 → 回落记忆
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_TEASER_CFG, context=_DONE_FREE_CTX)
    out = sm.build_proactive_opener("u1", silent_hours=48, intimacy=50.0)
    assert out["mode"] == MODE_FOLLOW_UP


def test_proactive_teaser_no_resolver_registered():
    # 未注册 resolver（变现未就绪）→ resolve_entitlement 返回 None → 不空推 → 回落记忆
    _cc.reset_relationship_providers()
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_TEASER_CFG, context=_DONE_FREE_CTX)
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1")
    assert out["mode"] == MODE_FOLLOW_UP


def test_proactive_teaser_suppressed_on_soft_emotion(_free_ent):
    # 末条负面情绪 → soft 闸 → 连付费预告都抑制（不在低谷推销）→ 仅记忆问候
    sm = _SM(_StubStore([_fact("在学吉他", tier="stable", hits=3)]),
             story_cfg=_TEASER_CFG, context=_DONE_FREE_CTX)
    out = sm.build_proactive_opener(
        "u1", silent_hours=48, intimacy=50.0, contact_key="tg:acc:u1",
        last_emotion="焦虑")
    assert out["mode"] == MODE_FOLLOW_UP


# ── Stage L：每日仪式问候 opener（build_ritual_opener）─────────────────────

def test_ritual_opener_morning_with_memory_hook():
    sm = _SM(_StubStore([_fact("在备考", tier="stable", hits=3)]))
    out = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0)
    assert out["mode"] == "ritual_morning"
    assert "早安" in out["directive"]
    assert out["fact"] == "在备考"
    assert "在备考" in out["directive"]  # 自然轻提一句记忆钩子


def test_ritual_opener_night_basic():
    sm = _SM(_StubStore([]))
    out = sm.build_ritual_opener("night", memory_key="u1", intimacy=50.0)
    assert out["mode"] == "ritual_night"
    assert "晚安" in out["directive"]
    assert out["fact"] == ""


def test_ritual_opener_invalid_slot():
    sm = _SM(_StubStore([_fact("x")]))
    assert sm.build_ritual_opener("noon", memory_key="u1")["mode"] == ""


def test_ritual_opener_blocked_on_recent_severe():
    import time as _t
    crisis = {"level": "severe", "created_at": _t.time() - 86400}
    sm = _SM(_StubStore([_fact("在备考", tier="stable")]),
             context={"u1": {}}, crisis_latest=crisis)
    out = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0)
    # 近期 severe → 不发欢快早安，带升级信号
    assert out["mode"] == ""
    assert out.get("blocked") == "crisis_severe"


def test_ritual_opener_soft_drops_memory_hook():
    sm = _SM(_StubStore([_fact("在备考", tier="stable", hits=3)]))
    out = sm.build_ritual_opener(
        "morning", memory_key="u1", intimacy=50.0, last_emotion="焦虑")
    # soft 档：仍问候但克制、不带记忆钩子
    assert out["mode"] == "ritual_morning"
    assert out["fact"] == ""
    assert "在备考" not in out["directive"]


def test_ritual_opener_new_relationship_restrained():
    sm = _SM(_StubStore([]))
    out = sm.build_ritual_opener(
        "morning", memory_key="u1", intimacy=20.0, stage="warming")
    assert "点到为止" in out["directive"]


# ── Stage P：纪念日·节日仪式 opener（build_milestone_opener）─────────────────

def test_milestone_anniversary_with_memory_hook():
    sm = _SM(_StubStore([_fact("在备考", tier="stable", hits=3)]))
    out = sm.build_milestone_opener(
        event_type="anniversary", days=100, memory_key="u1", intimacy=60.0)
    assert out["mode"] == "milestone_anniversary"
    assert "100" in out["directive"]
    assert out["fact"] == "在备考"
    assert "在备考" in out["directive"]


def test_milestone_holiday_basic():
    sm = _SM(_StubStore([]))
    out = sm.build_milestone_opener(
        event_type="holiday", event_label="圣诞节", memory_key="u1", intimacy=60.0)
    assert out["mode"] == "milestone_holiday"
    assert "圣诞节" in out["directive"]
    assert out["fact"] == ""


def test_milestone_invalid_event_type():
    sm = _SM(_StubStore([_fact("x")]))
    assert sm.build_milestone_opener(event_type="wedding", memory_key="u1")["mode"] == ""


def test_milestone_blocked_on_recent_severe():
    import time as _t
    crisis = {"level": "severe", "created_at": _t.time() - 86400}
    sm = _SM(_StubStore([_fact("在备考", tier="stable")]),
             context={"u1": {}}, crisis_latest=crisis)
    out = sm.build_milestone_opener(
        event_type="anniversary", days=100, memory_key="u1", intimacy=60.0)
    assert out["mode"] == ""
    assert out.get("blocked") == "crisis_severe"


def test_milestone_soft_drops_memory_hook():
    sm = _SM(_StubStore([_fact("在备考", tier="stable", hits=3)]))
    out = sm.build_milestone_opener(
        event_type="holiday", event_label="元旦", memory_key="u1",
        intimacy=60.0, last_emotion="焦虑")
    assert out["mode"] == "milestone_holiday"
    assert out["fact"] == ""
    assert "在备考" not in out["directive"]


def test_milestone_new_relationship_restrained():
    sm = _SM(_StubStore([]))
    out = sm.build_milestone_opener(
        event_type="anniversary", days=30, memory_key="u1",
        intimacy=35.0, stage="warming")
    assert "点到为止" in out["directive"]


def test_milestone_birthday_basic():
    sm = _SM(_StubStore([]))
    out = sm.build_milestone_opener(
        event_type="birthday", memory_key="u1", intimacy=60.0)
    assert out["mode"] == "milestone_birthday"
    assert "生日" in out["directive"]


def test_milestone_birthday_with_memory_hook():
    sm = _SM(_StubStore([_fact("在备考", tier="stable", hits=3)]))
    out = sm.build_milestone_opener(
        event_type="birthday", memory_key="u1", intimacy=60.0)
    assert out["fact"] == "在备考"
    assert "在备考" in out["directive"]


def test_resolve_birthday_from_memory():
    sm = _SM(_StubStore([_fact("随便聊聊"), _fact("我生日是3月5日")]))
    assert sm.resolve_birthday("u1") == (3, 5)


def test_resolve_birthday_none_when_absent():
    sm = _SM(_StubStore([_fact("喜欢猫"), _fact("在备考")]))
    assert sm.resolve_birthday("u1") is None
