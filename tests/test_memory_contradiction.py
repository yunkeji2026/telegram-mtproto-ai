"""R10 记忆矛盾消解：同一单值属性槽冲突时保留最新、旧值标 stale。

覆盖纯函数槽位解析/冲突判定，以及 store.resolve_contradictions、consolidate 顺序、
stale 排除出 prompt 注入。
"""

from __future__ import annotations

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.utils.memory_slots import (
    SLOT_NAME,
    SLOT_RELATIONSHIP,
    SLOT_RESIDENCE,
    extract_slot,
    slots_conflict,
)


@pytest.fixture
def mem(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    yield store
    store.close()


# ── 槽位解析 ────────────────────────────────────────────────────────────

def test_extract_residence():
    s = extract_slot("我现在住在上海")
    assert s and s[0] == SLOT_RESIDENCE and s[1] == "上海"


def test_residence_city_suffix_normalized():
    a = extract_slot("住在北京")
    b = extract_slot("住在北京市")
    assert a[1] == b[1]  # 北京 == 北京市


def test_extract_relationship_canonical():
    assert extract_slot("我现在单身")[:2] == (SLOT_RELATIONSHIP, "single")
    assert extract_slot("我有对象了")[:2] == (SLOT_RELATIONSHIP, "partnered")
    assert extract_slot("我结婚了")[:2] == (SLOT_RELATIONSHIP, "married")


def test_extract_name_templates():
    assert extract_slot("用户自称：小明")[:2] == (SLOT_NAME, "小明")
    assert extract_slot("用户希望我称呼 TA：阿强")[:2] == (SLOT_NAME, "阿强")


def test_extract_pref_polarity():
    pos = extract_slot("我喜欢猫")
    neg = extract_slot("用户表示不喜欢：猫")
    assert pos[0] == "pref:猫" and pos[2] == 1
    assert neg[0] == "pref:猫" and neg[2] == -1


def test_no_slot_for_plain_text():
    assert extract_slot("今天天气不错") is None


# ── 冲突判定 ────────────────────────────────────────────────────────────

def test_conflict_same_slot_diff_value():
    assert slots_conflict((SLOT_RESIDENCE, "北京", 0), (SLOT_RESIDENCE, "上海", 0))
    assert not slots_conflict((SLOT_RESIDENCE, "北京", 0), (SLOT_RESIDENCE, "北京", 0))


def test_conflict_pref_opposite_polarity():
    assert slots_conflict(("pref:猫", "", 1), ("pref:猫", "", -1))
    assert not slots_conflict(("pref:猫", "", 1), ("pref:狗", "", -1))


def test_no_conflict_diff_slot():
    assert not slots_conflict((SLOT_RESIDENCE, "北京", 0), (SLOT_NAME, "北京", 0))


# ── store 集成 ──────────────────────────────────────────────────────────

def _add_ts(store, uid, text, ts):
    rid = store.add_fact(uid, text)
    store._conn.execute(
        "UPDATE episodic_memory SET created_at = ?, last_seen = ? WHERE id = ?",
        (ts, ts, rid),
    )
    store._conn.commit()
    return rid


def test_resolve_marks_old_stale(mem):
    uid = "u1"
    old = _add_ts(mem, uid, "我住在北京", 1000.0)
    new = _add_ts(mem, uid, "我住在上海", 2000.0)
    res = mem.resolve_contradictions(uid)
    assert res["superseded"] == 1
    assert res["conflicts"] == 1
    # 旧条 stale、新条仍 raw
    def _tier(rid):
        return mem._conn.execute(
            "SELECT tier FROM episodic_memory WHERE id = ?", (rid,)
        ).fetchone()[0]
    assert _tier(old) == "stale"
    assert _tier(new) == "raw"


def test_stale_excluded_from_prompt(mem):
    uid = "u2"
    _add_ts(mem, uid, "我住在北京", 1000.0)
    _add_ts(mem, uid, "我住在上海", 2000.0)
    mem.resolve_contradictions(uid)
    out = mem.get_bullets_for_prompt(uid, 10, 800)
    assert "上海" in out
    assert "北京" not in out  # 旧值已 stale，不进 prompt


def test_consistent_values_not_staled(mem):
    uid = "u3"
    _add_ts(mem, uid, "我住在北京", 1000.0)
    _add_ts(mem, uid, "我住在北京市", 2000.0)  # 同地，规范化后相等
    res = mem.resolve_contradictions(uid)
    assert res["superseded"] == 0


def test_consolidate_resolves_then_dedup(mem):
    uid = "u4"
    _add_ts(mem, uid, "我住在北京", 1000.0)
    _add_ts(mem, uid, "我住在上海", 2000.0)
    res = mem.consolidate(uid, resolve_contradictions=True)
    assert res["superseded"] == 1


def test_consolidate_default_no_resolve(mem):
    uid = "u5"
    _add_ts(mem, uid, "我住在北京", 1000.0)
    _add_ts(mem, uid, "我住在上海", 2000.0)
    res = mem.consolidate(uid)  # 不开 → 不消解
    assert res.get("superseded", 0) == 0
    out = mem.get_bullets_for_prompt(uid, 10, 800)
    assert "北京" in out and "上海" in out


# ── R11：新证据推翻旧 stable 结论 ────────────────────────────────────────

def _set_tier(store, rid, tier):
    store._conn.execute(
        "UPDATE episodic_memory SET tier = ? WHERE id = ?", (tier, rid)
    )
    store._conn.commit()


def _set_hits(store, rid, hits):
    store._conn.execute(
        "UPDATE episodic_memory SET hits = ? WHERE id = ?", (hits, rid)
    )
    store._conn.commit()


def test_stable_not_superseded_by_single_mention(mem):
    """一次随口提及不足以推翻 stable（出差去上海 ≠ 搬家）。"""
    uid = "s1"
    old = _add_ts(mem, uid, "我住在北京", 1000.0)
    _set_tier(mem, old, "stable")
    new = _add_ts(mem, uid, "我住在上海", 2000.0)  # hits=1 < 门槛 2
    res = mem.resolve_contradictions(uid, supersede_stable=True, stable_min_hits=2)
    assert res["stable_superseded"] == 0
    tier = mem._conn.execute(
        "SELECT tier FROM episodic_memory WHERE id = ?", (old,)
    ).fetchone()[0]
    assert tier == "stable"  # 证据不足，旧结论保留


def test_stable_superseded_by_repeated_evidence(mem):
    """反复提及（hits≥门槛）→ 推翻旧 stable，标 stale。"""
    uid = "s2"
    old = _add_ts(mem, uid, "我住在北京", 1000.0)
    _set_tier(mem, old, "stable")
    new = _add_ts(mem, uid, "我住在上海", 2000.0)
    _set_hits(mem, new, 3)  # 反复提=真的搬了
    res = mem.resolve_contradictions(uid, supersede_stable=True, stable_min_hits=2)
    assert res["stable_superseded"] == 1
    tier = mem._conn.execute(
        "SELECT tier FROM episodic_memory WHERE id = ?", (old,)
    ).fetchone()[0]
    assert tier == "stale"
    out = mem.get_bullets_for_prompt(uid, 10, 800)
    assert "上海" in out and "北京" not in out


def test_supersede_stable_off_by_default(mem):
    """默认不开 supersede_stable → stable 不受新 raw 影响（R10 行为不变）。"""
    uid = "s3"
    old = _add_ts(mem, uid, "我住在北京", 1000.0)
    _set_tier(mem, old, "stable")
    new = _add_ts(mem, uid, "我住在上海", 2000.0)
    _set_hits(mem, new, 5)
    res = mem.resolve_contradictions(uid)  # 不开
    assert res.get("stable_superseded", 0) == 0
    tier = mem._conn.execute(
        "SELECT tier FROM episodic_memory WHERE id = ?", (old,)
    ).fetchone()[0]
    assert tier == "stable"


def test_consolidate_supersede_stable_wired(mem):
    """consolidate 透传 supersede_stable / stable_min_hits。"""
    uid = "s4"
    old = _add_ts(mem, uid, "我单身", 1000.0)
    _set_tier(mem, old, "stable")
    new = _add_ts(mem, uid, "我有对象了", 2000.0)
    _set_hits(mem, new, 2)
    res = mem.consolidate(
        uid, resolve_contradictions=True, supersede_stable=True, stable_min_hits=2
    )
    assert res["stable_superseded"] == 1
    tier = mem._conn.execute(
        "SELECT tier FROM episodic_memory WHERE id = ?", (old,)
    ).fetchone()[0]
    assert tier == "stale"


def test_stable_not_superseded_when_no_conflict(mem):
    """同槽同值（住北京 vs 住北京市）不算冲突，stable 不动。"""
    uid = "s5"
    old = _add_ts(mem, uid, "我住在北京", 1000.0)
    _set_tier(mem, old, "stable")
    new = _add_ts(mem, uid, "我住在北京市", 2000.0)
    _set_hits(mem, new, 4)
    res = mem.resolve_contradictions(uid, supersede_stable=True, stable_min_hits=2)
    assert res["stable_superseded"] == 0
