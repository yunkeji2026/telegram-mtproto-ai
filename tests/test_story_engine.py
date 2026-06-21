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
        "on_complete": {"memory": "我们一起喝过一次咖啡"},
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
            "warm": {"directive": "结局：开心约好下次。", "memory": "我们约好下次再一起散步"},
            "cool": {"directive": "结局：礼貌道别。", "memory": "我们一起散过一次步"},
        },
    },
    "paid_trip": {
        "title": "海边之旅",
        "require_unlock": "story_ch1",
        "min_bond_level": 0,
        "beats": [{"id": "go", "directive": "场景：一起去海边。"}],
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
    mem = ""
    seen_beats = [st["beat_index"]]
    for _ in range(6):
        st, finished, mem = advance_state(st, SCENARIOS, advance_turns=2)
        if finished:
            break
        seen_beats.append(st["beat_index"])
    assert finished is True
    assert st is None
    assert seen_beats == [0, 0, 1, 1, 2, 2]
    # 末 beat 之后无分支 → on_complete 兜底回写
    assert mem == "我们一起喝过一次咖啡"


def test_advance_single_turn_per_beat():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    st, fin, _ = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 1
    st, fin, _ = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 2
    st, fin, mem = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and st is None
    assert mem == "我们一起喝过一次咖啡"


def test_advance_invalid_is_safe():
    assert advance_state(None, SCENARIOS) == (None, True, "")
    assert advance_state({"scenario_id": "gone"}, SCENARIOS) == (None, True, "")


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
    # 结局段演绎满 advance_turns → 收场回写 warm 记忆
    st, fin, mem = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and st is None
    assert mem == "我们约好下次再一起散步"


def test_branch_routes_to_cool_ending():
    st = start_scenario("branch_date", SCENARIOS, bond_level=0)
    st, _, _ = advance_state(st, SCENARIOS, advance_turns=1)
    st, fin, _ = advance_state(st, SCENARIOS, user_message="算了我有点忙", advance_turns=1)
    assert st["ending_id"] == "cool"
    st, fin, mem = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and mem == "我们一起散过一次步"


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
