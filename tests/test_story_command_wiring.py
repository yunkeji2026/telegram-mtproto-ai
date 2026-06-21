"""Phase ③ skill_manager 剧情指令接线测试（轻量绑定，免全量 init）。

校验：列表/开始/结束指令短路逻辑、双 gate（关系等级 + 付费权益）拦截、开始成功后
story_state 落入 user_context 且能产出【剧情场景】prompt 块。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.story_engine import build_story_prompt_block

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager

_SCENARIOS = {
    "coffee_date": {
        "title": "初次咖啡约会",
        "min_bond_level": 2,
        "beats": [
            {"id": "arrive", "directive": "场景：咖啡馆初次见面。"},
            {"id": "chat", "directive": "场景推进：聊起近况。"},
        ],
    },
    "starry_night": {
        "title": "星空下的约定",
        "min_bond_level": 3,
        "require_unlock": "all_story",
        "beats": [{"id": "rooftop", "directive": "场景：天台看星空。"}],
    },
    "sequel": {
        "title": "咖啡续约",
        "min_bond_level": 2,
        "requires_story": [{"scenario": "coffee_date", "ending": "warm"}],
        "beats": [{"id": "s", "directive": "续作场景。"}],
    },
}


class _SM:
    # 绑定真实方法（不走 __init__）
    _story_cfg = _SMcls._story_cfg
    _story_scenarios = _SMcls._story_scenarios
    _story_state_root = _SMcls._story_state_root
    _get_story_state = _SMcls._get_story_state
    _set_story_state = _SMcls._set_story_state
    _bond_level_from_context = _SMcls._bond_level_from_context
    _effective_intimacy = _SMcls._effective_intimacy
    _story_bonus_cap = _SMcls._story_bonus_cap
    _apply_story_intimacy_bonus = _SMcls._apply_story_intimacy_bonus
    _record_story_completion = _SMcls._record_story_completion
    _match_scenario = _SMcls._match_scenario
    _scenario_title = staticmethod(_SMcls._scenario_title)
    _story_outcomes = _SMcls._story_outcomes
    _handle_story_command = _SMcls._handle_story_command
    _writeback_story_memory = _SMcls._writeback_story_memory
    _episodic_storage_key = _SMcls._episodic_storage_key

    def __init__(self, *, enabled=True):
        cfg = {"companion": {"story": {
            "enabled": enabled, "advance_turns": 3, "scenarios": _SCENARIOS,
        }}}
        self.config = SimpleNamespace(config=cfg)
        self.logger = logging.getLogger("test_story")
        self._episodic_store = None
        self._memory_cfg = {"scope": "user"}
        self._cpi = None


def _ctx(intimacy=None, entitlement=None):
    c = {}
    if intimacy is not None:
        c["intimacy_score"] = intimacy
    if entitlement is not None:
        c["entitlement"] = entitlement
    return c


def test_disabled_returns_none():
    sm = _SM(enabled=False)
    assert sm._handle_story_command("剧情列表", _ctx(), "chatA") is None


def test_list_shows_availability():
    sm = _SM()
    # intimacy 95 → bond level 4：关系达标，免费场景可玩
    out = sm._handle_story_command("剧情列表", _ctx(intimacy=95), "chatA")
    assert out and "初次咖啡约会" in out
    assert "发「开始剧情 初次咖啡约会」" in out
    # 付费专属：关系够深但无 all_story 权益 → 标注需解锁
    assert "星空下的约定（专属剧情，需解锁）" in out


def test_list_paid_locked_by_bond_when_relationship_shallow():
    sm = _SM()
    # intimacy 40 → level 2 < starry_night 的 min_bond_level 3 → 优先报「再熟一点」
    out = sm._handle_story_command("剧情列表", _ctx(intimacy=40), "chatA")
    assert "星空下的约定（我们再熟一点就能解锁）" in out


def test_start_free_scenario_sets_state():
    sm = _SM()
    ctx = _ctx(intimacy=40)
    # 开始成功返回 None（短路交给正常回复流程），state 落入 ctx
    assert sm._handle_story_command("开始剧情 初次咖啡约会", ctx, "chatA") is None
    state = sm._get_story_state(ctx, "chatA")
    assert state and state["scenario_id"] == "coffee_date"
    blk = build_story_prompt_block(state, _SCENARIOS)
    assert "初次咖啡约会" in blk and "咖啡馆初次见面" in blk


def test_start_blocked_by_bond():
    sm = _SM()
    # intimacy 10 → level 1，低于 coffee_date 的 min_bond_level 2
    out = sm._handle_story_command("开始剧情 初次咖啡约会", _ctx(intimacy=10), "chatA")
    assert out and "更熟" in out


def test_start_blocked_by_unlock():
    sm = _SM()
    # 关系够深(level4)但无 all_story 权益 → 锁
    out = sm._handle_story_command(
        "开始剧情 星空下的约定", _ctx(intimacy=95), "chatA")
    assert out and "专属剧情" in out
    # 给 all_story 权益 → 可进入
    ctx = _ctx(intimacy=95, entitlement={"grants": ("all_story",), "unlocked": ()})
    assert sm._handle_story_command("开始剧情 星空下的约定", ctx, "chatA") is None
    assert sm._get_story_state(ctx, "chatA")["scenario_id"] == "starry_night"


def test_stop_clears_state():
    sm = _SM()
    ctx = _ctx(intimacy=40)
    sm._handle_story_command("开始剧情 初次咖啡约会", ctx, "chatA")
    assert sm._get_story_state(ctx, "chatA")
    out = sm._handle_story_command("结束剧情", ctx, "chatA")
    assert out and "平常聊天" in out
    assert sm._get_story_state(ctx, "chatA") is None


def test_unknown_scenario():
    sm = _SM()
    out = sm._handle_story_command("开始剧情 不存在的故事", _ctx(intimacy=40), "chatA")
    assert out and "还不会" in out


def test_non_command_passthrough():
    sm = _SM()
    assert sm._handle_story_command("今天天气真好", _ctx(intimacy=40), "chatA") is None


# ── Phase ④ 完成回写共享记忆 ──────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.calls = []

    def add_fact(self, key, text, label, source="user_stated"):
        self.calls.append((key, text, label, source))
        return len(self.calls)


def test_writeback_writes_shared_memory_user_stated():
    sm = _SM()
    sm._episodic_store = _FakeStore()
    sm._writeback_story_memory("u1", "chatA", {"platform": "telegram"},
                               "我们约好了下次再一起喝咖啡")
    assert len(sm._episodic_store.calls) == 1
    key, text, label, source = sm._episodic_store.calls[0]
    assert text == "我们约好了下次再一起喝咖啡"
    assert label == "story"
    assert source == "user_stated"


def test_writeback_noop_without_store_or_memory():
    sm = _SM()
    # 无 store → 静默跳过
    sm._writeback_story_memory("u1", "chatA", {}, "x")
    # 有 store 但空记忆 → 不写
    sm._episodic_store = _FakeStore()
    sm._writeback_story_memory("u1", "chatA", {}, "   ")
    assert sm._episodic_store.calls == []


# ── Phase ④ 剧情→成长（intimacy 加成） ────────────────────────────

def test_story_bonus_accumulates_and_lifts_bond():
    sm = _SM()
    ctx = {"intimacy_score": 55.0}   # 基础亲密度
    base_bond = sm._bond_level_from_context(ctx, "chatA")
    # 完成两次剧情各 +4 → story_bonus=8，有效亲密度 63
    sm._apply_story_intimacy_bonus(ctx, "chatA", 4)
    sm._apply_story_intimacy_bonus(ctx, "chatA", 4)
    assert sm._effective_intimacy(ctx, "chatA") == 63.0
    assert sm._bond_level_from_context(ctx, "chatA") >= base_bond


def test_story_bonus_is_capped():
    sm = _SM()
    ctx = {"intimacy_score": 10.0}
    for _ in range(20):
        sm._apply_story_intimacy_bonus(ctx, "chatA", 4)
    from src.utils.companion_relationship import get_rel_state
    assert get_rel_state(ctx, "chatA")["story_bonus"] == sm._story_bonus_cap()


def test_story_bonus_per_chat_isolated():
    sm = _SM()
    ctx = {"intimacy_score": 50.0}
    sm._apply_story_intimacy_bonus(ctx, "chatA", 4)
    assert sm._effective_intimacy(ctx, "chatA") == 54.0
    # 另一会话无加成
    assert sm._effective_intimacy(ctx, "chatB") == 50.0


def test_effective_intimacy_none_base_stays_none():
    sm = _SM()
    ctx = {}   # 无基础信号 → 不臆造关系
    sm._apply_story_intimacy_bonus(ctx, "chatA", 4)
    assert sm._effective_intimacy(ctx, "chatA") is None


# ── Phase ④续 防刷 + 完成纪念点 ───────────────────────────────────

def test_first_completion_grants_bonus_and_milestone():
    sm = _SM()
    ctx = {"intimacy_score": 50.0}
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 4)
    assert sm._effective_intimacy(ctx, "chatA") == 54.0
    assert ctx["bond_fresh_milestone"] == "story:一起经历了《初次咖啡约会》"
    from src.utils.companion_relationship import get_rel_state
    assert "coffee_date" in get_rel_state(ctx, "chatA")["story_done"]


def test_replay_completion_no_bonus_no_milestone():
    sm = _SM()
    ctx = {"intimacy_score": 50.0}
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 4)
    eff_after_first = sm._effective_intimacy(ctx, "chatA")
    ctx.pop("bond_fresh_milestone", None)
    # 重复完成同一剧情 → 不再加成、不再置纪念点
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 4)
    assert sm._effective_intimacy(ctx, "chatA") == eff_after_first
    assert "bond_fresh_milestone" not in ctx


def test_milestone_label_resolves_story_code():
    from src.contacts.relationship_level import build_bond_level_block
    blk = build_bond_level_block(
        60.0, fresh_milestone="story:一起经历了《初次咖啡约会》")
    assert "一起经历了《初次咖啡约会》" in blk


# ── Phase ④续³ 跨场景因果（requires_story） ───────────────────────

def test_completion_records_outcome_ending():
    sm = _SM()
    ctx = {"intimacy_score": 50.0}
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 4,
                                ending="warm")
    assert sm._story_outcomes(ctx, "chatA") == {"coffee_date": "warm"}


def test_sequel_locked_until_prerequisite_then_unlocks():
    sm = _SM()
    ctx = _ctx(intimacy=40)   # bond level 2，满足 sequel 的 min_bond_level
    # 没经历过前传 → 开始 sequel 被前置 gate 挡，提示先经历前传
    out = sm._handle_story_command("开始剧情 咖啡续约", ctx, "chatA")
    assert out and "先一起经历" in out and "初次咖啡约会" in out
    # 以 warm 结局完成 coffee_date → 解锁 sequel
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 0,
                                ending="warm")
    assert sm._handle_story_command("开始剧情 咖啡续约", ctx, "chatA") is None
    assert sm._get_story_state(ctx, "chatA")["scenario_id"] == "sequel"


def test_sequel_wrong_ending_stays_locked():
    sm = _SM()
    ctx = _ctx(intimacy=40)
    # cool 结局完成前传 → sequel 仍锁（需 warm）
    sm._record_story_completion(ctx, "chatA", "coffee_date", "初次咖啡约会", 0,
                                ending="cool")
    out = sm._handle_story_command("开始剧情 咖啡续约", ctx, "chatA")
    assert out and "先一起经历" in out


def test_story_list_shows_prerequisite_hint():
    sm = _SM()
    out = sm._handle_story_command("剧情列表", _ctx(intimacy=40), "chatA")
    assert "咖啡续约（经历过《初次咖啡约会》后解锁）" in out
