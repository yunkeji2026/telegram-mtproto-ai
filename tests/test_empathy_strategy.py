"""共情策略选择器单测（蒸馏版 STRIDE-ED / 主动倾听）。

覆盖：策略选择映射、关系阶段克制修饰、block 装配、与 emotional_context 的集成
（启用/禁用、与关系阶段/弧线联动），以及向后兼容（默认开、异常不抛）。
"""

from __future__ import annotations

from src.utils.companion_relationship import chat_storage_key
from src.utils.emotional_context import build_emotional_context_block
from src.utils.empathy_strategy import (
    STRATEGY_LABELS_ZH,
    build_strategy_block,
    select_strategy,
    strategy_directive,
)


# ── select_strategy 维度映射 ────────────────────────────────────────────

def test_select_negative_high_intensity_validates():
    assert select_strategy(dimension="negative", intensity=0.8) == "validate"


def test_select_negative_high_arousal_validates():
    assert select_strategy(dimension="negative", intensity=0.3, arousal=0.8) == "validate"


def test_select_negative_worsening_validates():
    assert select_strategy(dimension="negative", intensity=0.3, arc="worsening") == "validate"


def test_select_negative_mild_explores():
    assert select_strategy(dimension="negative", intensity=0.4, arousal=0.2) == "explore_needs"


def test_select_low_energy_accompanies():
    assert select_strategy(dimension="low_energy", intensity=0.6) == "accompany"


def test_select_positive_savors():
    assert select_strategy(dimension="positive", intensity=0.7) == "savor"


def test_select_curious():
    assert select_strategy(dimension="curious") == "curiosity"


def test_select_neutral_active_listen():
    assert select_strategy(dimension="neutral") == "active_listen"
    # 未知维度回退承接式倾听
    assert select_strategy(dimension="bogus") == "active_listen"


def test_select_handles_bad_numbers():
    # 非数字强度/激活不应抛
    assert select_strategy(dimension="negative", intensity="x", arousal=None) == "explore_needs"


# ── strategy_directive 关系阶段克制 ─────────────────────────────────────

def test_directive_early_stage_adds_restraint_for_deep():
    d = strategy_directive("explore_needs", stage="initial")
    assert "关系还偏新" in d
    d2 = strategy_directive("validate", stage="warming")
    assert "关系还偏新" in d2


def test_directive_late_stage_no_restraint():
    d = strategy_directive("explore_needs", stage="steady")
    assert "关系还偏新" not in d


def test_directive_shallow_strategy_no_restraint_even_early():
    # active_listen 非深挖型，新关系也不加克制修饰
    d = strategy_directive("active_listen", stage="initial")
    assert "关系还偏新" not in d


def test_directive_unknown_falls_back():
    d = strategy_directive("nonexistent")
    assert d == strategy_directive("active_listen")


# ── build_strategy_block ────────────────────────────────────────────────

def test_build_block_shape():
    emo = {"dimension": "negative", "primary_intensity": 0.9, "arousal": 0.5}
    block = build_strategy_block(emo, stage="intimate")
    assert block.startswith("【应对策略 · ")
    assert STRATEGY_LABELS_ZH["validate"] in block


def test_build_block_bad_input_returns_empty():
    assert build_strategy_block(None) == ""  # type: ignore[arg-type]
    assert build_strategy_block("nope") == ""  # type: ignore[arg-type]


# ── 集成：emotional_context 注入应对策略 ────────────────────────────────

def test_emotional_block_includes_strategy_by_default():
    block = build_emotional_context_block("好烦啊烦死了！", {"reply_count": 3}, "")
    assert "应对策略" in block


def test_emotional_block_strategy_can_be_disabled():
    block = build_emotional_context_block(
        "好烦啊烦死了！", {"reply_count": 3}, "", enable_strategy=False
    )
    assert "应对策略" not in block


def test_emotional_block_strategy_follows_stage_restraint():
    # 负面 + 新关系（initial）→ validate/explore 带克制修饰
    ctx = {
        "reply_count": 2,
        "companion_relationship": {
            chat_storage_key(7): {"stage": "initial", "exchange_count": 2},
        },
    }
    block = build_emotional_context_block("有点难过", ctx, "", chat_id=7)
    assert "应对策略" in block
    assert "关系还偏新" in block


def test_emotional_block_positive_savor_no_restraint_when_steady():
    ctx = {
        "reply_count": 50,
        "companion_relationship": {
            chat_storage_key(9): {"stage": "steady", "exchange_count": 60},
        },
    }
    block = build_emotional_context_block("今天好开心哈哈", ctx, "", chat_id=9)
    assert STRATEGY_LABELS_ZH["savor"] in block
    assert "关系还偏新" not in block
