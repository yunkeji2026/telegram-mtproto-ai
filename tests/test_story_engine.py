"""Phase ③/④ 剧情/场景 roleplay 引擎单测。

覆盖：双 gate 准入（付费 require_unlock 走 entitlement_allows + 关系等级 min_bond_level）、
开启、确定性推进 beat、分支多结局路由、剧终收场 + 完成回写共享记忆、prompt 块组装、
非法输入安全降级。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.story_engine import (
    advance_state,
    build_story_prompt_block,
    current_directive,
    list_scenarios,
    scenario_available,
    scenario_locked_reason,
    select_story_invite,
    start_scenario,
)

SCENARIOS = {
    "coffee_date": {
        "title": "初次咖啡约会",
        "min_bond_level": 2,
        "beats": [
            {"id": "arrive", "directive": "场景：咖啡馆初次见面。"},
            {"id": "chat", "directive": "场景推进：聊起近况。"},
            {"id": "closing", "directive": "场景收尾：温柔道别。"},
        ],
        "on_complete": {"memory": "我们一起喝过一次咖啡", "intimacy_bonus": 2},
    },
    "branch_date": {
        "title": "分岔约会",
        "min_bond_level": 0,
        "beats": [
            {"id": "open", "directive": "场景：散步聊天。"},
            {
                "id": "ask",
                "directive": "要不要约下次？",
                "branch": [
                    {"keywords": ["好", "愿意", "想"], "ending": "warm"},
                    {"keywords": ["算了", "忙", "不"], "ending": "cool"},
                ],
                "default_ending": "warm",
            },
        ],
        "endings": {
            "warm": {"directive": "结局：开心约好下次。", "memory": "我们约好下次再一起散步",
                     "intimacy_bonus": 4},
            "cool": {"directive": "结局：礼貌道别。", "memory": "我们一起散过一次步"},
        },
    },
    "paid_trip": {
        "title": "海边之旅",
        "require_unlock": "story_ch1",
        "min_bond_level": 0,
        "beats": [{"id": "go", "directive": "场景：一起去海边。"}],
    },
    "sequel": {                       # 续作：需先以 warm 结局走过 branch_date
        "title": "续作",
        "min_bond_level": 0,
        "requires_story": [{"scenario": "branch_date", "ending": "warm"}],
        "require_unlock": "all_story",
        "beats": [{"id": "s", "directive": "续作场景。"}],
    },
    "sequel_any": {                   # 续作：完成过 branch_date 即可（任意结局）
        "title": "续作B",
        "requires_story": ["branch_date"],
        "beats": [{"id": "s", "directive": "续作B场景。"}],
    },
    "empty": {"title": "空场景", "beats": []},
}


# ── 准入 gate ─────────────────────────────────────────────────────

def test_locked_by_bond():
    assert scenario_locked_reason(SCENARIOS["coffee_date"], bond_level=1) == "need_bond:2"
    assert scenario_locked_reason(SCENARIOS["coffee_date"], bond_level=2) == ""


def test_locked_by_unlock():
    scn = SCENARIOS["paid_trip"]
    assert scenario_locked_reason(scn, entitlement={"grants": (), "unlocked": ()}) \
        == "need_unlock:story_ch1"
    assert scenario_available(scn, entitlement={"grants": (), "unlocked": ("story_ch1",)})
    assert not scenario_available(scn, entitlement={"grants": ("all_story",), "unlocked": ()})


def test_bond_checked_before_unlock():
    scn = {"require_unlock": "story_ch1", "min_bond_level": 3,
           "beats": [{"directive": "x"}]}
    assert scenario_locked_reason(scn, bond_level=1) == "need_bond:3"


# ── 跨场景前置 requires_story ──────────────────────────────────────

def test_locked_by_missing_prerequisite():
    scn = SCENARIOS["sequel_any"]
    assert scenario_locked_reason(scn, completed={}) == "need_story:branch_date"
    assert scenario_available(scn, completed={"branch_date": ""})


def test_locked_by_wrong_ending():
    scn = SCENARIOS["sequel"]
    # 完成了 branch_date 但走的是 cool 结局 → 仍锁（需 warm）
    assert scenario_locked_reason(
        scn, completed={"branch_date": "cool"},
        entitlement={"grants": ("all_story",), "unlocked": ()},
    ) == "need_story:branch_date"
    # warm 结局 + 有 all_story → 放行
    assert scenario_available(
        scn, completed={"branch_date": "warm"},
        entitlement={"grants": ("all_story",), "unlocked": ()},
    )


def test_prereq_checked_before_unlock():
    scn = SCENARIOS["sequel"]
    # 缺前置 + 缺付费 → 优先报 need_story（更友好/可行动）
    assert scenario_locked_reason(scn, completed={}) == "need_story:branch_date"
    # 满足前置但缺付费 → 才报 need_unlock
    assert scenario_locked_reason(
        scn, completed={"branch_date": "warm"}) == "need_unlock:all_story"


# ── 主动剧情邀约 select_story_invite ───────────────────────────────

def test_invite_picks_first_available_free_unfinished():
    # bond2：coffee_date 达标且免费、未完成 → 首选（声明序第一个合格者）
    inv = select_story_invite(SCENARIOS, bond_level=2, completed={})
    assert inv["scenario_id"] == "coffee_date"
    assert inv["title"] == "初次咖啡约会"


def test_invite_skips_locked_by_bond():
    # bond0：coffee_date 锁（need bond2）→ 跳到 branch_date（bond0 免费）
    inv = select_story_invite(SCENARIOS, bond_level=0, completed={})
    assert inv["scenario_id"] == "branch_date"


def test_invite_excludes_paid_scenarios():
    # 只有付费场景候选时（高 bond 但都做完免费的）→ 付费不邀约 → None
    done = {"coffee_date": "", "branch_date": "warm", "sequel_any": ""}
    inv = select_story_invite(SCENARIOS, bond_level=5, completed=done)
    assert inv is None  # paid_trip / sequel 均付费，entitlement=None 不邀约


def test_invite_unlocks_sequel_after_prerequisite():
    # 免费续作 sequel_any 需完成 branch_date；完成后（且 coffee_date 已做）→ 邀约 sequel_any
    done = {"coffee_date": "", "branch_date": "warm"}
    inv = select_story_invite(SCENARIOS, bond_level=2, completed=done)
    assert inv["scenario_id"] == "sequel_any"


def test_invite_skips_completed_and_active():
    # coffee_date 完成 + branch_date 进行中 → 跳过两者 → 取下一个免费可邀约（sequel_any 需前置未满足）
    # 此处 branch_date 未完成（只是 active）→ 它不在 completed → 但被 active_id 跳过；
    # sequel_any 需 branch_date 完成（未完成）→ 锁；故 None
    inv = select_story_invite(
        SCENARIOS, bond_level=2, completed={"coffee_date": ""},
        active_id="branch_date")
    assert inv is None


def test_invite_none_for_empty_or_bad():
    assert select_story_invite(None, bond_level=5) is None
    assert select_story_invite({}, bond_level=5) is None


def test_start_blocked_by_prerequisite():
    assert start_scenario("sequel_any", SCENARIOS, completed={}) is None
    assert start_scenario(
        "sequel_any", SCENARIOS, completed={"branch_date": "cool"}) is not None


def test_list_scenarios_marks_availability():
    rows = list_scenarios(SCENARIOS, entitlement={"grants": (), "unlocked": ()}, bond_level=2)
    by = {r["id"]: r for r in rows}
    assert by["coffee_date"]["available"] is True
    assert by["paid_trip"]["available"] is False
    assert by["paid_trip"]["locked_reason"] == "need_unlock:story_ch1"
    assert by["coffee_date"]["beats"] == 3
    assert by["branch_date"]["endings"] == 2


# ── 开启 ──────────────────────────────────────────────────────────

def test_start_blocked_when_locked():
    assert start_scenario("coffee_date", SCENARIOS, bond_level=1) is None
    assert start_scenario("paid_trip", SCENARIOS, bond_level=0) is None


def test_start_ok():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2, now=1000.0)
    assert st["scenario_id"] == "coffee_date"
    assert st["beat_index"] == 0 and st["turns_in_beat"] == 0
    assert st["ending_id"] == ""
    assert st["started_at"] == 1000.0


def test_start_unknown_or_empty():
    assert start_scenario("nope", SCENARIOS) is None
    assert start_scenario("empty", SCENARIOS) is None


# ── 线性推进 + 完成回写 ────────────────────────────────────────────

def test_advance_progresses_beats_deterministically():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    finished = False
    payload = {}
    seen_beats = [st["beat_index"]]
    for _ in range(6):
        st, finished, payload = advance_state(st, SCENARIOS, advance_turns=2)
        if finished:
            break
        seen_beats.append(st["beat_index"])
    assert finished is True
    assert st is None
    assert seen_beats == [0, 0, 1, 1, 2, 2]
    # 末 beat 之后无分支 → on_complete 兜底结算（记忆 + 关系加成）
    assert payload["memory"] == "我们一起喝过一次咖啡"
    assert payload["intimacy_bonus"] == 2.0


def test_advance_single_turn_per_beat():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    st, fin, _ = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 1
    st, fin, _ = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 2
    st, fin, payload = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and st is None
    assert payload["memory"] == "我们一起喝过一次咖啡"


def test_advance_invalid_is_safe():
    assert advance_state(None, SCENARIOS) == (None, True, {})
    assert advance_state({"scenario_id": "gone"}, SCENARIOS) == (None, True, {})


# ── 分支多结局 ────────────────────────────────────────────────────

def test_branch_routes_to_warm_ending_and_writes_memory():
    st = start_scenario("branch_date", SCENARIOS, bond_level=0)
    # open beat（advance_turns=1 即推进到 ask 选择点）
    st, fin, _ = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 1
    # 在选择点回应「好呀」→ 路由 warm 结局段
    st, fin, _ = advance_state(st, SCENARIOS, user_message="好呀，我愿意", advance_turns=1)
    assert not fin and st["ending_id"] == "warm"
    assert current_directive(st, SCENARIOS) == "结局：开心约好下次。"
    # 结局段演绎满 advance_turns → 收场结算 warm 记忆 + 关系加成
    st, fin, payload = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and st is None
    assert payload["memory"] == "我们约好下次再一起散步"
    assert payload["intimacy_bonus"] == 4.0


def test_branch_routes_to_cool_ending():
    st = start_scenario("branch_date", SCENARIOS, bond_level=0)
    st, _, _ = advance_state(st, SCENARIOS, advance_turns=1)
    st, fin, _ = advance_state(st, SCENARIOS, user_message="算了我有点忙", advance_turns=1)
    assert st["ending_id"] == "cool"
    st, fin, payload = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and payload["memory"] == "我们一起散过一次步"
    # cool 结局未配 intimacy_bonus → 0
    assert payload["intimacy_bonus"] == 0.0


def test_branch_no_keyword_falls_back_to_default_ending():
    st = start_scenario("branch_date", SCENARIOS, bond_level=0)
    st, _, _ = advance_state(st, SCENARIOS, advance_turns=1)
    # 答非所问 → default_ending=warm
    st, fin, _ = advance_state(st, SCENARIOS, user_message="今天天气真好", advance_turns=1)
    assert st["ending_id"] == "warm"


# ── prompt 块 ─────────────────────────────────────────────────────

def test_current_directive_and_block():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    assert current_directive(st, SCENARIOS) == "场景：咖啡馆初次见面。"
    blk = build_story_prompt_block(st, SCENARIOS)
    assert blk.startswith("【剧情场景·初次咖啡约会】")
    assert "咖啡馆初次见面" in blk


def test_ending_block_marks_ending():
    st = start_scenario("branch_date", SCENARIOS, bond_level=0)
    st, _, _ = advance_state(st, SCENARIOS, advance_turns=1)
    st, _, _ = advance_state(st, SCENARIOS, user_message="好呀", advance_turns=1)
    blk = build_story_prompt_block(st, SCENARIOS)
    assert "·结局】" in blk
    assert "开心约好下次" in blk


def test_block_empty_when_no_story():
    assert build_story_prompt_block(None, SCENARIOS) == ""
    assert current_directive({"scenario_id": "coffee_date", "beat_index": 9},
                             SCENARIOS) == ""
