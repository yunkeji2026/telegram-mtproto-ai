"""Q2 关系热度信号归一：关系阶段（companion）与情感上下文「关系温度」同源。

验证 ``emotional_context`` 的温度档位优先由 companion 关系阶段映射得到，
并在无阶段时回退到 reply_count 启发式（向后兼容）。
"""

from __future__ import annotations

from src.utils.companion_relationship import chat_storage_key
from src.utils.emotional_context import (
    _STAGE_TO_WARMTH,
    _WARMTH_GUIDANCE,
    build_emotional_context_block,
    compute_warmth_level,
    lookup_companion_stage,
    warmth_from_stage,
)


# ── warmth_from_stage ───────────────────────────────────────────────────

def test_warmth_from_stage_maps_all_four_stages():
    expect = {
        "initial": "stranger",
        "warming": "acquaintance",
        "intimate": "familiar",
        "steady": "close",
    }
    for stage, label in expect.items():
        out = warmth_from_stage(stage)
        assert out is not None
        assert out["warmth_label"] == label
        assert out["tone_guidance"] == _WARMTH_GUIDANCE[label]


def test_warmth_from_stage_unknown_returns_none():
    assert warmth_from_stage(None) is None
    assert warmth_from_stage("") is None
    assert warmth_from_stage("  ") is None
    assert warmth_from_stage("bogus") is None


def test_stage_map_is_one_to_one_with_guidance():
    # 每个阶段映射到的档位都必须有对应文案
    for label in _STAGE_TO_WARMTH.values():
        assert label in _WARMTH_GUIDANCE


# ── compute_warmth_level 文案与 stage 路径同源 ──────────────────────────

def test_compute_warmth_level_uses_shared_guidance():
    # 任意输入下，返回的语气文案都应与共享字典对应档位字面一致
    out = compute_warmth_level(exchange_count=0, days_known=0.0)
    assert out["tone_guidance"] == _WARMTH_GUIDANCE[out["warmth_label"]]


def test_compute_and_stage_paths_share_text_for_same_label():
    # 同一档位（close），两条路径给出的语气指导必须字面相同
    heur = compute_warmth_level(exchange_count=200, days_known=365.0, avg_valence=1.0)
    stage_based = warmth_from_stage("steady")
    assert heur["warmth_label"] == "close"
    assert stage_based["warmth_label"] == "close"
    assert heur["tone_guidance"] == stage_based["tone_guidance"]


# ── lookup_companion_stage ──────────────────────────────────────────────

def test_lookup_by_chat_id_key():
    ctx = {
        "companion_relationship": {
            chat_storage_key(12345): {"stage": "intimate", "exchange_count": 20},
        }
    }
    assert lookup_companion_stage(ctx, 12345) == "intimate"


def test_lookup_single_entry_fallback_when_chat_id_missing():
    ctx = {
        "companion_relationship": {
            chat_storage_key(999): {"stage": "steady", "exchange_count": 50},
        }
    }
    # 传入对不上的 chat_id，但只有一条会话 → 回退使用该条
    assert lookup_companion_stage(ctx, None) == "steady"
    assert lookup_companion_stage(ctx, 123) == "steady"


def test_lookup_multi_entry_no_match_returns_empty():
    ctx = {
        "companion_relationship": {
            chat_storage_key(1): {"stage": "warming"},
            chat_storage_key(2): {"stage": "intimate"},
        }
    }
    assert lookup_companion_stage(ctx, 999) == ""


def test_lookup_absent_returns_empty():
    assert lookup_companion_stage({}, 1) == ""
    assert lookup_companion_stage({"companion_relationship": {}}, 1) == ""
    assert lookup_companion_stage({"companion_relationship": "x"}, 1) == ""


# ── build_emotional_context_block 集成：温度跟随关系阶段 ─────────────────

def test_block_warmth_follows_companion_stage():
    ctx = {
        "reply_count": 1,  # 启发式会判 stranger，但阶段是 steady → 应跟随阶段
        "companion_relationship": {
            chat_storage_key(777): {"stage": "steady", "exchange_count": 60},
        },
    }
    block = build_emotional_context_block("在吗", ctx, "", chat_id=777)
    assert "关系温度 — close" in block
    assert "关系温度 — stranger" not in block


def test_block_falls_back_to_reply_count_without_stage():
    # 无 companion_relationship → 走启发式（最低档位为 acquaintance）
    ctx = {"reply_count": 0}
    block = build_emotional_context_block("你好", ctx, "")
    assert "关系温度 — acquaintance" in block


def test_block_no_contradiction_intimate_stage():
    # 阶段 intimate → 温度 familiar，不应出现陌生档位
    ctx = {
        "reply_count": 2,
        "companion_relationship": {
            chat_storage_key(5): {"stage": "intimate", "exchange_count": 18},
        },
    }
    block = build_emotional_context_block("今天有点累", ctx, "", chat_id=5)
    assert "关系温度 — familiar" in block
    assert "stranger" not in block
    assert "acquaintance" not in block
