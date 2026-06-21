"""Phase ② 关系成长系统（Bond Level）单测。

校验：等级/进度映射与 companion_relationship 规范阶段对齐（单一事实源、零漂移）、
里程碑达成判定、按等级解锁（含阶段 code 键 + 数字键 + 累计）、prompt 块克制触发。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.relationship_level import (
    bond_milestones,
    build_bond_level_block,
    compute_bond_level,
    level_unlocks,
)
from src.utils.companion_relationship import (
    INTIMACY_BAND_DEFAULTS,
    STAGE_ORDER,
    derive_stage_from_intimacy,
)


# ── compute_bond_level ────────────────────────────────────────────

def test_level_none_or_invalid():
    out = compute_bond_level(None)
    assert out["level"] == 0 and out["stage"] == ""
    assert compute_bond_level("abc")["level"] == 0


def test_levels_align_with_canonical_stages():
    """每个等级的 stage 必须与 derive_stage_from_intimacy 完全一致（不许另起阈值）。"""
    for score in (0, 10, 24.9, 25, 40, 54.9, 55, 70, 79.9, 80, 95, 100):
        out = compute_bond_level(score)
        assert out["stage"] == derive_stage_from_intimacy(score)
        assert out["level"] == STAGE_ORDER.index(out["stage"]) + 1


def test_initial_band_progress_and_next():
    out = compute_bond_level(0)
    assert out["level"] == 1 and out["stage"] == "initial"
    assert out["progress"] == 0.0
    assert out["next_stage"] == "warming"
    assert out["score_to_next"] == INTIMACY_BAND_DEFAULTS["to_warming"]
    assert out["is_max"] is False


def test_midband_progress():
    # warming 段 [25,55)，score=40 → 进度 (40-25)/30 = 0.5
    out = compute_bond_level(40)
    assert out["stage"] == "warming"
    assert out["progress"] == 0.5
    assert out["score_to_next"] == 15.0  # 55-40


def test_max_level_steady():
    out = compute_bond_level(90)
    assert out["stage"] == "steady"
    assert out["level"] == len(STAGE_ORDER)
    assert out["is_max"] is True
    assert out["score_to_next"] == 0.0
    assert out["next_stage"] == ""


def test_custom_bands_override():
    out = compute_bond_level(30, bands={"to_warming": 40})
    # 阈值抬到 40 → 30 仍是 initial
    assert out["stage"] == "initial"


# ── bond_milestones ───────────────────────────────────────────────

def test_milestones_tenure():
    ms = bond_milestones(days_known=35)
    codes = {m["code"] for m in ms}
    assert "relationship_week" in codes and "relationship_month" in codes
    assert "relationship_100d" not in codes


def test_milestones_talk_and_levelup():
    ms = bond_milestones(intimacy_score=60, turn_count_in=250)
    codes = {m["code"] for m in ms}
    assert "talked_50" in codes and "talked_200" in codes
    assert "talked_1000" not in codes
    # intimacy 60 = intimate（level3）→ 升级里程碑含 warming + intimate
    assert "reached_warming" in codes and "reached_intimate" in codes
    assert "reached_steady" not in codes


def test_milestones_empty_inputs():
    assert bond_milestones() == []


# ── level_unlocks ─────────────────────────────────────────────────

def test_unlocks_cumulative_numeric_keys():
    umap = {1: ["a"], 2: ["b"], 3: ["c", "d"], 4: ["e"]}
    assert level_unlocks(3, umap) == ["a", "b", "c", "d"]
    assert level_unlocks(1, umap) == ["a"]
    assert level_unlocks(0, umap) == []


def test_unlocks_stage_code_keys():
    umap = {"warming": ["voice_reply"], "intimate": ["exclusive_album"]}
    # level 2 = warming
    assert level_unlocks(2, umap) == ["voice_reply"]
    # level 3 = intimate → 累计
    assert set(level_unlocks(3, umap)) == {"voice_reply", "exclusive_album"}


def test_unlocks_dedup_and_empty():
    assert level_unlocks(4, {1: ["x"], 2: ["x", "y"]}) == ["x", "y"]
    assert level_unlocks(3, None) == []
    assert level_unlocks(3, {}) == []


# ── build_bond_level_block ────────────────────────────────────────

def test_block_silent_for_initial():
    assert build_bond_level_block(10) == ""


def test_block_fresh_milestone():
    blk = build_bond_level_block(60, fresh_milestone="relationship_month")
    assert "相识满月" in blk and blk.startswith("【关系进展】")


def test_block_depth_background_for_intimate():
    blk = build_bond_level_block(70, days_known=40)
    assert "【关系进展】" in blk and "40" in blk


def test_block_warming_no_milestone_is_silent():
    # warming（level2）但无 fresh_milestone 且非 intimate/steady → 不打扰
    assert build_bond_level_block(40) == ""
