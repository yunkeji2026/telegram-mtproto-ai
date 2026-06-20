"""R5 近似去重巩固：把语义近似的 raw 事实归并为一条（承接 R3）。

覆盖：高相似度归并 + hits 累加 + survivor 择优、低相似度不动、stable 不受影响、
min_raw 早退、无 embedding 跳过、consolidate(dedup_threshold) 端到端（并后晋升）。
"""

from __future__ import annotations

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.utils.episodic_vector import vec_to_blob


@pytest.fixture
def mem(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    yield store
    store.close()


def _add(store, uid, text, vec, **over):
    rid = store.add_fact(uid, text, embedding_blob=vec_to_blob(vec))
    assert rid is not None
    if over:
        cols = ", ".join(f"{k} = ?" for k in over)
        store._conn.execute(
            f"UPDATE episodic_memory SET {cols} WHERE id = ?",
            (*over.values(), rid),
        )
        store._conn.commit()
    return rid


def _row(store, rid):
    return store._conn.execute(
        "SELECT content, hits, salience, tier FROM episodic_memory WHERE id = ?",
        (rid,),
    ).fetchone()


def _exists(store, rid):
    return store._conn.execute(
        "SELECT 1 FROM episodic_memory WHERE id = ?", (rid,)
    ).fetchone() is not None


_DIM = 16


def _oh(idx: int, second: int = -1, w: float = 0.02):
    """长 16 的近独热向量：主轴 idx=1.0；可选 second 轴加微噪声做"近义"。"""
    v = [0.0] * _DIM
    v[idx] = 1.0
    if second >= 0:
        v[second] = w
    return v


def _pad(uid, store, n=6):
    # 撑过 min_raw 门槛用的无关、互相正交事实（占用高位轴 8..，与测试向量不冲突）
    for i in range(n):
        _add(store, uid, f"无关琐事{i}号内容", _oh(8 + i))


# ── 近义归并 ────────────────────────────────────────────────────────────

def test_merges_near_duplicates(mem):
    uid = "u1"
    _pad(uid, mem)
    a = _add(mem, uid, "用户喜欢猫", _oh(0), hits=2)
    b = _add(mem, uid, "用户养了一只猫", _oh(0, 1), hits=1)
    res = mem.merge_near_duplicates(uid, threshold=0.9)
    assert res["merged"] == 1
    assert res["clusters"] == 1
    # 二者只剩一条；survivor 的 hits 累加为 3
    alive = [r for r in (a, b) if _exists(mem, r)]
    assert len(alive) == 1
    assert _row(mem, alive[0])[1] == 3


def test_survivor_prefers_higher_salience(mem):
    uid = "u2"
    _pad(uid, mem)
    low = _add(mem, uid, "短句", _oh(0), salience=0.1, hits=1)
    high = _add(mem, uid, "用户很爱猫非常喜欢", _oh(0, 1),
                salience=0.8, hits=1)
    mem.merge_near_duplicates(uid, threshold=0.9)
    # 高 salience 者存活
    assert _exists(mem, high)
    assert not _exists(mem, low)


def test_low_similarity_not_merged(mem):
    uid = "u3"
    _pad(uid, mem)
    a = _add(mem, uid, "用户喜欢猫", _oh(0))
    b = _add(mem, uid, "用户住在北京", _oh(1))
    res = mem.merge_near_duplicates(uid, threshold=0.9)
    assert res["merged"] == 0
    assert _exists(mem, a) and _exists(mem, b)


def test_stable_tier_untouched(mem):
    uid = "u4"
    _pad(uid, mem)
    s = _add(mem, uid, "稳定的猫事实", _oh(0), tier="stable")
    r = _add(mem, uid, "另一种说法的猫", _oh(0, 1), tier="raw")
    mem.merge_near_duplicates(uid, threshold=0.9)
    # stable 永不被并/删
    assert _exists(mem, s) and _exists(mem, r)


def test_min_raw_short_circuits(mem):
    uid = "u5"
    a = _add(mem, uid, "用户喜欢猫", _oh(0))
    b = _add(mem, uid, "用户养了猫", _oh(0, 1))
    # raw 只有 2 条 < min_raw(默认6) → 直接跳过
    res = mem.merge_near_duplicates(uid)
    assert res["merged"] == 0
    assert _exists(mem, a) and _exists(mem, b)


def test_no_embedding_skipped(mem):
    uid = "u6"
    _pad(uid, mem)
    a = mem.add_fact(uid, "无向量事实甲")
    b = mem.add_fact(uid, "无向量事实乙")
    res = mem.merge_near_duplicates(uid, threshold=0.5)
    # 无 embedding 的不参与；pad 互不相似 → 0 并
    assert res["merged"] == 0
    assert _exists(mem, a) and _exists(mem, b)


# ── consolidate 端到端：先并近义、再晋升 ─────────────────────────────────

def test_consolidate_dedup_then_promote(mem):
    uid = "u7"
    _pad(uid, mem)
    # 两条近义、各 hits=1：单独都不达 min_hits=2；并后 survivor hits=2 → 应被晋升
    _add(mem, uid, "用户喜欢喝燕麦拿铁", _oh(0), hits=1)
    _add(mem, uid, "用户爱点燕麦拿铁", _oh(0, 1), hits=1)
    res = mem.consolidate(uid, min_hits=2, dedup_threshold=0.9)
    assert res["merged"] == 1
    assert res["promoted"] >= 1
    # survivor 已是 stable
    row = mem._conn.execute(
        "SELECT tier, hits FROM episodic_memory WHERE content LIKE '%燕麦拿铁%'"
    ).fetchone()
    assert row[0] == "stable" and row[1] == 2


def test_consolidate_without_dedup_unchanged(mem):
    uid = "u8"
    _pad(uid, mem)
    _add(mem, uid, "用户喜欢喝燕麦拿铁", _oh(0), hits=1)
    _add(mem, uid, "用户爱点燕麦拿铁", _oh(0, 1), hits=1)
    # 不传 dedup_threshold → 不并；各 hits=1 < 2 → 不晋升
    res = mem.consolidate(uid, min_hits=2)
    assert res["merged"] == 0
    assert res["promoted"] == 0
