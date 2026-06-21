"""Phase ③ 剧情/场景 roleplay 引擎单测。

覆盖：双 gate 准入（付费 require_unlock 走 entitlement_allows + 关系等级 min_bond_level）、
开启、确定性推进 beat、剧终收场、prompt 块组装、非法输入安全降级。
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
    # 无权益 → 锁
    assert scenario_locked_reason(scn, entitlement={"grants": (), "unlocked": ()}) \
        == "need_unlock:story_ch1"
    # 一次性解锁 story_ch1 → 放行
    assert scenario_available(scn, entitlement={"grants": (), "unlocked": ("story_ch1",)})
    # 会员授予 all_story 不等于 story_ch1（feature 名需匹配）→ 仍锁
    assert not scenario_available(scn, entitlement={"grants": ("all_story",), "unlocked": ()})


def test_bond_checked_before_unlock():
    scn = {"require_unlock": "story_ch1", "min_bond_level": 3,
           "beats": [{"directive": "x"}]}
    # bond 不足优先报 need_bond（即便也缺解锁）
    assert scenario_locked_reason(scn, bond_level=1) == "need_bond:3"


def test_list_scenarios_marks_availability():
    rows = list_scenarios(SCENARIOS, entitlement={"grants": (), "unlocked": ()}, bond_level=2)
    by = {r["id"]: r for r in rows}
    assert by["coffee_date"]["available"] is True
    assert by["paid_trip"]["available"] is False
    assert by["paid_trip"]["locked_reason"] == "need_unlock:story_ch1"
    assert by["coffee_date"]["beats"] == 3


# ── 开启 ──────────────────────────────────────────────────────────

def test_start_blocked_when_locked():
    assert start_scenario("coffee_date", SCENARIOS, bond_level=1) is None
    assert start_scenario("paid_trip", SCENARIOS, bond_level=0) is None


def test_start_ok():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2, now=1000.0)
    assert st["scenario_id"] == "coffee_date"
    assert st["beat_index"] == 0 and st["turns_in_beat"] == 0
    assert st["started_at"] == 1000.0


def test_start_unknown_or_empty():
    assert start_scenario("nope", SCENARIOS) is None
    assert start_scenario("empty", SCENARIOS) is None


# ── 推进 ──────────────────────────────────────────────────────────

def test_advance_progresses_beats_deterministically():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    finished = False
    # advance_turns=2：每 2 轮推进一格；3 个 beat 共需 6 轮收场
    seen_beats = [st["beat_index"]]
    for _ in range(6):
        st, finished = advance_state(st, SCENARIOS, advance_turns=2)
        if finished:
            break
        seen_beats.append(st["beat_index"])
    assert finished is True
    assert st is None
    assert seen_beats == [0, 0, 1, 1, 2, 2]


def test_advance_single_turn_per_beat():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    st, fin = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 1
    st, fin = advance_state(st, SCENARIOS, advance_turns=1)
    assert not fin and st["beat_index"] == 2
    st, fin = advance_state(st, SCENARIOS, advance_turns=1)
    assert fin and st is None


def test_advance_invalid_is_safe():
    assert advance_state(None, SCENARIOS) == (None, True)
    assert advance_state({"scenario_id": "gone"}, SCENARIOS) == (None, True)


# ── prompt 块 ─────────────────────────────────────────────────────

def test_current_directive_and_block():
    st = start_scenario("coffee_date", SCENARIOS, bond_level=2)
    assert current_directive(st, SCENARIOS) == "场景：咖啡馆初次见面。"
    blk = build_story_prompt_block(st, SCENARIOS)
    assert blk.startswith("【剧情场景·初次咖啡约会】")
    assert "咖啡馆初次见面" in blk


def test_block_empty_when_no_story():
    assert build_story_prompt_block(None, SCENARIOS) == ""
    assert current_directive({"scenario_id": "coffee_date", "beat_index": 9},
                             SCENARIOS) == ""
