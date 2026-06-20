"""R3 记忆离线巩固 / 分层（PersonaTree/REMT-lite 思想的轻量落地）。

覆盖：写入即落 salience、重复事实累加 hits、consolidate 复发/情绪浓晋升 stable、
prune 保护 stable、stable 检索加权，以及向后兼容（默认关、旧库 ALTER 升列）。
"""

from __future__ import annotations

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore


@pytest.fixture
def mem(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    yield store
    store.close()


def _row(store, user_id, content):
    for r in store.list_rows(prefix=user_id, limit=50):
        if r["content"] == content:
            return r
    return None


def _raw_full_row(store, content):
    cur = store._conn.execute(
        "SELECT salience, tier, hits FROM episodic_memory WHERE content = ?",
        (content,),
    ).fetchone()
    return cur


# ── 写入期落 salience + 复发累加 hits ────────────────────────────────────

def test_add_persists_salience(mem):
    mem.add_fact("u1", "用户最近很难过，经常哭")
    sal, tier, hits = _raw_full_row(mem, "用户最近很难过，经常哭")
    assert sal is not None and sal > 0.0
    assert tier == "raw"
    assert hits == 1


def test_duplicate_bumps_hits_returns_none(mem):
    assert mem.add_fact("u2", "用户喜欢猫") is not None
    # 重复：返回 None、count 不变、但 hits 累加（向后兼容 test_add_dedupe 语义）
    assert mem.add_fact("u2", "用户喜欢猫") is None
    assert mem.add_fact("u2", "用户喜欢猫") is None
    assert mem.count("u2") == 1
    _, _, hits = _raw_full_row(mem, "用户喜欢猫")
    assert hits == 3


# ── consolidate 晋升 ─────────────────────────────────────────────────────

def test_consolidate_promotes_recurring(mem):
    mem.add_fact("u3", "用户养了一只叫团子的猫")  # hits=1
    mem.add_fact("u3", "用户养了一只叫团子的猫")  # hits=2
    mem.add_fact("u3", "用户住在北京")  # hits=1（不达标）
    res = mem.consolidate("u3", min_hits=2)
    assert res["promoted"] == 1
    assert res["stable_total"] == 1
    _, tier, _ = _raw_full_row(mem, "用户养了一只叫团子的猫")
    assert tier == "stable"
    _, tier2, _ = _raw_full_row(mem, "用户住在北京")
    assert tier2 == "raw"


def test_consolidate_promotes_high_salience(mem):
    mem.add_fact("u4", "用户最近失恋了非常难过经常哭很伤心")  # 情绪浓, hits=1
    mem.add_fact("u4", "用户住在上海")  # 中性, hits=1
    res = mem.consolidate("u4", min_hits=99, min_salience=0.2)
    assert res["promoted"] >= 1
    _, tier, _ = _raw_full_row(mem, "用户最近失恋了非常难过经常哭很伤心")
    assert tier == "stable"


def test_consolidate_idempotent(mem):
    mem.add_fact("u5", "复发事实")
    mem.add_fact("u5", "复发事实")
    assert mem.consolidate("u5", min_hits=2)["promoted"] == 1
    # 再跑一次不重复晋升
    assert mem.consolidate("u5", min_hits=2)["promoted"] == 0


# ── prune 保护 stable ────────────────────────────────────────────────────

def test_prune_protects_stable(mem):
    # 1 条 stable + 多条 raw；prune 到很小，stable 必须存活
    mem.add_fact("u6", "稳定事实")
    mem.add_fact("u6", "稳定事实")  # hits=2
    mem.consolidate("u6", min_hits=2)
    for i in range(6):
        mem.add_fact("u6", f"琐事{i}")
    mem.prune_oldest("u6", 2)
    contents = [r["content"] for r in mem.list_rows(prefix="u6", limit=50)]
    assert "稳定事实" in contents


# ── stable 检索加权 ──────────────────────────────────────────────────────

def test_stable_tier_boosts_retrieval(mem):
    # 两条中性事实，一条晋升 stable；开重排后 stable 应上浮
    mem.add_fact("u7", "事实甲普通内容")
    mem.add_fact("u7", "事实甲普通内容")  # hits=2 → 可晋升
    mem.add_fact("u7", "事实乙普通内容")
    mem.consolidate("u7", min_hits=2)
    out = mem.get_bullets_for_prompt("u7", 5, 500, use_salience_rerank=True)
    assert out.index("事实甲") < out.index("事实乙")


def test_rerank_off_unaffected_by_tier(mem):
    # 默认不重排时，tier 不影响顺序（仍按近期）
    mem.add_fact("u8", "较早的事实甲")
    mem.add_fact("u8", "较早的事实甲")
    mem.consolidate("u8", min_hits=2)
    mem.add_fact("u8", "更晚的事实乙")
    out = mem.get_bullets_for_prompt("u8", 5, 500)
    # 最新（乙）在前，未被 stable 加权改变
    assert out.index("更晚的事实乙") < out.index("较早的事实甲")


# ── 向后兼容：旧库（无新列）能平滑升列 ──────────────────────────────────

def test_legacy_db_migrates_columns(tmp_path):
    import sqlite3
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(
        """
        CREATE TABLE episodic_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            created_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO episodic_memory (user_id, content, content_hash, category, created_at)"
        " VALUES ('old', '旧事实', 'h1', 'general', 1000.0)"
    )
    conn.commit()
    conn.close()

    store = EpisodicMemoryStore(p)
    try:
        cols = [r[1] for r in store._conn.execute(
            "PRAGMA table_info(episodic_memory)").fetchall()]
        assert {"salience", "tier", "hits", "last_seen"} <= set(cols)
        # 旧行 last_seen 已回填为 created_at
        row = store._conn.execute(
            "SELECT tier, hits, last_seen FROM episodic_memory WHERE content='旧事实'"
        ).fetchone()
        assert row[0] == "raw" and row[1] == 1 and row[2] == 1000.0
        # 旧库仍可正常写入/检索
        assert store.add_fact("old", "新事实") is not None
    finally:
        store.close()
