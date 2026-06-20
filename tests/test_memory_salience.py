"""R2 记忆显著性 + 时间衰减重排（REMT-lite）单测。

覆盖纯函数（salience/recency/blend）+ EpisodicMemoryStore.get_bullets_for_prompt
开启重排前后的行为（默认关零变化、开启后情绪浓/近期记忆上浮）。
"""

from __future__ import annotations

import time

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.utils.memory_salience import (
    blend_rank,
    recency_factor,
    salience_score,
)


# ── 纯函数 ──────────────────────────────────────────────────────────────

def test_salience_neutral_low_emotional_high():
    neutral = salience_score("用户住在北京")
    charged = salience_score("用户最近很难过很伤心，哭了")
    assert 0.0 <= neutral <= 1.0
    assert 0.0 <= charged <= 1.0
    assert charged > neutral


def test_salience_empty_zero():
    assert salience_score("") == 0.0
    assert salience_score("   ") == 0.0


def test_recency_newer_higher():
    now = 1_000_000.0
    fresh = recency_factor(now - 1 * 86400, now, half_life_days=30.0)
    old = recency_factor(now - 60 * 86400, now, half_life_days=30.0)
    assert fresh > old
    # 60 天 = 2 个半衰期 → ~0.25
    assert abs(old - 0.25) < 0.02


def test_recency_missing_is_neutral():
    assert recency_factor(None) == 0.5
    assert recency_factor(0) == 0.5


def test_recency_future_clamped_to_one():
    now = 1000.0
    assert recency_factor(now + 99999, now) == pytest.approx(1.0, abs=1e-9)


def test_blend_adds_weighted_terms():
    out = blend_rank(0.5, 1.0, 1.0, salience_weight=0.15, recency_weight=0.10)
    assert out == pytest.approx(0.5 + 0.15 + 0.10)


def test_blend_base_dominates():
    # 强相关(base=0.9)中性记忆 仍应高于 弱相关(base=0.2)情绪浓记忆
    strong = blend_rank(0.9, 0.0, 0.5)
    weak_emotional = blend_rank(0.2, 1.0, 1.0)
    assert strong > weak_emotional


# ── store 集成 ──────────────────────────────────────────────────────────

@pytest.fixture
def mem(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    yield store
    store.close()


def test_rerank_off_is_default_recency_order(mem):
    # 无 query、无重排 → 近期优先（与旧版一致）
    mem.add_fact("u1", "事实A住在北京")
    time.sleep(0.01)
    mem.add_fact("u1", "事实B喜欢猫")
    out = mem.get_bullets_for_prompt("u1", 5, 500)
    # 最新的在前
    assert out.index("事实B") < out.index("事实A")


def test_rerank_lifts_emotional_recent(mem):
    # 两条都无 query 相关；开启重排后，情绪浓的应上浮
    mem.add_fact("u2", "用户住在北京海淀区")
    mem.add_fact("u2", "用户最近失恋了，非常难过，经常哭")
    out = mem.get_bullets_for_prompt(
        "u2", 5, 500, use_salience_rerank=True,
    )
    assert "难过" in out
    # 情绪浓的那条应排在中性事实之前
    assert out.index("难过") < out.index("海淀区")


def test_rerank_keyword_path_still_returns_relevant(mem):
    mem.add_fact("u3", "用户喜欢喝燕麦拿铁")
    mem.add_fact("u3", "用户住在北京")
    out = mem.get_bullets_for_prompt(
        "u3", 2, 500, query_text="你记得我喜欢喝什么吗",
        rerank_keywords=True, use_salience_rerank=True,
    )
    # 强相关项（拿铁）即便中性也应被选中（base 占主导）
    assert "燕麦" in out or "拿铁" in out


def test_empty_user_returns_blank(mem):
    assert mem.get_bullets_for_prompt("nobody", 5, 500, use_salience_rerank=True) == ""
