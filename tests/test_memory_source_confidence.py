"""R12 记忆置信/来源标注：user_stated vs ai_inferred 的分级置信。

覆盖 add_fact 落 source、复发升级（推断→明说）、source_aware 晋升门槛分级、
以及推翻 stable 时只数 user_stated 证据。
"""

from __future__ import annotations

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore


@pytest.fixture
def mem(tmp_path):
    store = EpisodicMemoryStore(tmp_path / "epi.db")
    yield store
    store.close()


def _source(store, rid):
    return store._conn.execute(
        "SELECT COALESCE(source, 'user_stated') FROM episodic_memory WHERE id = ?",
        (rid,),
    ).fetchone()[0]


def _tier(store, rid):
    return store._conn.execute(
        "SELECT tier FROM episodic_memory WHERE id = ?", (rid,)
    ).fetchone()[0]


def _add_ts(store, uid, text, ts, source="user_stated"):
    rid = store.add_fact(uid, text, source=source)
    if rid:
        store._conn.execute(
            "UPDATE episodic_memory SET created_at = ?, last_seen = ? WHERE id = ?",
            (ts, ts, rid),
        )
        store._conn.commit()
    return rid


def _set_hits(store, rid, hits):
    store._conn.execute(
        "UPDATE episodic_memory SET hits = ? WHERE id = ?", (hits, rid)
    )
    store._conn.commit()


# ── 写入与来源 ──────────────────────────────────────────────────────────

def test_add_fact_default_source_user_stated(mem):
    rid = mem.add_fact("u1", "我喜欢爬山")
    assert _source(mem, rid) == "user_stated"


def test_add_fact_ai_inferred_tagged(mem):
    rid = mem.add_fact("u1", "用户可能在备考", source="ai_inferred")
    assert _source(mem, rid) == "ai_inferred"


def test_invalid_source_falls_back_to_user_stated(mem):
    rid = mem.add_fact("u1", "随便一句话内容", source="garbage")
    assert _source(mem, rid) == "user_stated"


def test_recurrence_upgrades_inferred_to_stated(mem):
    """AI 先推断、用户后亲口说同一事实 → 置信升级为 user_stated。"""
    rid = mem.add_fact("u1", "用户喜欢猫", source="ai_inferred")
    assert _source(mem, rid) == "ai_inferred"
    # 同 hash 复发，这次是用户明说
    again = mem.add_fact("u1", "用户喜欢猫", source="user_stated")
    assert again is None  # 复发不新增行
    assert _source(mem, rid) == "user_stated"  # 已升级


def test_recurrence_never_downgrades(mem):
    """用户明说在先，AI 再推断同事实 → 不降级。"""
    rid = mem.add_fact("u1", "用户住在广州", source="user_stated")
    mem.add_fact("u1", "用户住在广州", source="ai_inferred")
    assert _source(mem, rid) == "user_stated"


# ── source_aware 晋升门槛 ───────────────────────────────────────────────

def test_inferred_needs_higher_bar_to_promote(mem):
    """source_aware 开：ai_inferred hits=2 不晋升（门槛 min_hits+1=3）。"""
    uid = "p1"
    rid = mem.add_fact(uid, "用户大概是程序员", source="ai_inferred")
    _set_hits(mem, rid, 2)
    res = mem.consolidate(uid, min_hits=2, source_aware=True)
    assert _tier(mem, rid) == "raw"  # 未晋升
    assert res["promoted"] == 0


def test_inferred_promotes_at_higher_bar(mem):
    uid = "p2"
    rid = mem.add_fact(uid, "用户大概是程序员", source="ai_inferred")
    _set_hits(mem, rid, 3)  # 达到 min_hits+1
    res = mem.consolidate(uid, min_hits=2, source_aware=True)
    assert _tier(mem, rid) == "stable"
    assert res["promoted"] == 1


def test_stated_uses_normal_bar_under_source_aware(mem):
    """user_stated 在 source_aware 下仍走原门槛（hits>=min_hits）。"""
    uid = "p3"
    rid = mem.add_fact(uid, "用户住在杭州", source="user_stated")
    _set_hits(mem, rid, 2)
    res = mem.consolidate(uid, min_hits=2, source_aware=True)
    assert _tier(mem, rid) == "stable"
    assert res["promoted"] == 1


def test_inferred_no_salience_shortcut(mem):
    """source_aware 下 ai_inferred 不享受情绪显著性捷径，仅 hits 达标才晋升。"""
    uid = "p4"
    rid = mem.add_fact(uid, "用户也许很焦虑很痛苦", source="ai_inferred")
    # 拉高 salience，但 hits 仅 1
    mem._conn.execute(
        "UPDATE episodic_memory SET salience = 0.99, hits = 1 WHERE id = ?", (rid,)
    )
    mem._conn.commit()
    res = mem.consolidate(uid, min_hits=2, min_salience=0.5, source_aware=True)
    assert _tier(mem, rid) == "raw"  # 情绪浓也不晋升推断
    assert res["promoted"] == 0


def test_source_aware_off_keeps_r11_behavior(mem):
    """source_aware 关：ai_inferred 与 user_stated 一视同仁（R11 行为）。"""
    uid = "p5"
    rid = mem.add_fact(uid, "用户大概是老师", source="ai_inferred")
    _set_hits(mem, rid, 2)
    res = mem.consolidate(uid, min_hits=2)  # 不开 source_aware
    assert _tier(mem, rid) == "stable"
    assert res["promoted"] == 1


# ── 推翻 stable 时只数 user_stated 证据 ─────────────────────────────────

def test_inferred_evidence_cannot_supersede_stable(mem):
    """source_aware：ai_inferred 的高 hits 也推不翻 user_stated stable。"""
    uid = "s1"
    old = _add_ts(mem, uid, "我住在北京", 1000.0, source="user_stated")
    mem._conn.execute(
        "UPDATE episodic_memory SET tier = 'stable' WHERE id = ?", (old,)
    )
    mem._conn.commit()
    new = _add_ts(mem, uid, "我住在上海", 2000.0, source="ai_inferred")
    _set_hits(mem, new, 5)
    res = mem.resolve_contradictions(
        uid, supersede_stable=True, stable_min_hits=2, source_aware=True
    )
    assert res["stable_superseded"] == 0
    assert _tier(mem, old) == "stable"


def test_stated_evidence_supersedes_stable_under_source_aware(mem):
    uid = "s2"
    old = _add_ts(mem, uid, "我住在北京", 1000.0, source="user_stated")
    mem._conn.execute(
        "UPDATE episodic_memory SET tier = 'stable' WHERE id = ?", (old,)
    )
    mem._conn.commit()
    new = _add_ts(mem, uid, "我住在上海", 2000.0, source="user_stated")
    _set_hits(mem, new, 2)
    res = mem.resolve_contradictions(
        uid, supersede_stable=True, stable_min_hits=2, source_aware=True
    )
    assert res["stable_superseded"] == 1
    assert _tier(mem, old) == "stale"


# ── R13：list_rows 暴露 source/tier + 来源筛选 ──────────────────────────

def test_list_rows_exposes_source_tier_hits(mem):
    uid = "L1"
    mem.add_fact(uid, "用户喜欢茶", source="user_stated")
    mem.add_fact(uid, "用户大概在加班", source="ai_inferred")
    rows = mem.list_rows(prefix=uid, limit=50)
    assert len(rows) == 2
    for r in rows:
        assert r["source"] in ("user_stated", "ai_inferred")
        assert r["tier"] == "raw"
        assert r["hits"] == 1


def test_list_rows_source_filter(mem):
    uid = "L2"
    mem.add_fact(uid, "用户住在成都", source="user_stated")
    mem.add_fact(uid, "用户可能喜欢辣", source="ai_inferred")
    mem.add_fact(uid, "用户养了狗", source="user_stated")
    stated = mem.list_rows(prefix=uid, source="user_stated")
    inferred = mem.list_rows(prefix=uid, source="ai_inferred")
    assert len(stated) == 2 and all(r["source"] == "user_stated" for r in stated)
    assert len(inferred) == 1 and inferred[0]["source"] == "ai_inferred"


def test_list_rows_invalid_source_returns_all(mem):
    uid = "L3"
    mem.add_fact(uid, "用户住在重庆", source="user_stated")
    mem.add_fact(uid, "用户可能单身", source="ai_inferred")
    rows = mem.list_rows(prefix=uid, source="garbage")
    assert len(rows) == 2


# ── R14：profile_summary 聚合 ──────────────────────────────────────────

def test_profile_summary_empty(mem):
    assert mem.profile_summary("nobody") == {
        "total": 0, "stable": 0, "raw": 0,
        "user_stated": 0, "ai_inferred": 0, "top_stable": [],
        "pending_inferred": [],
    }


def test_profile_summary_counts_by_tier_source(mem):
    uid = "P1"
    mem.add_fact(uid, "用户住在南京", source="user_stated")
    mem.add_fact(uid, "用户喜欢茶", source="user_stated")
    mem.add_fact(uid, "用户可能在创业", source="ai_inferred")
    out = mem.profile_summary(uid)
    assert out["total"] == 3
    assert out["raw"] == 3 and out["stable"] == 0
    assert out["user_stated"] == 2 and out["ai_inferred"] == 1


def test_profile_summary_top_stable_and_stale_excluded(mem):
    uid = "P2"
    a = mem.add_fact(uid, "用户是医生", source="user_stated")
    b = mem.add_fact(uid, "用户养了两只猫", source="user_stated")
    stale = mem.add_fact(uid, "用户住在旧城", source="user_stated")
    mem._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id IN (?,?)", (a, b))
    mem._conn.execute("UPDATE episodic_memory SET tier='stale' WHERE id=?", (stale,))
    mem._conn.commit()
    out = mem.profile_summary(uid, top_stable=3)
    assert out["stable"] == 2
    assert out["total"] == 2  # stale 不计
    assert len(out["top_stable"]) == 2
    assert "用户住在旧城" not in out["top_stable"]


def test_profile_summary_blank_key(mem):
    assert mem.profile_summary("")["total"] == 0


# ── R15：待确认推断 + 一键转明说 ───────────────────────────────────────

def test_profile_summary_pending_inferred_listed(mem):
    uid = "P3"
    mem.add_fact(uid, "用户明说住北京", source="user_stated")
    inf = mem.add_fact(uid, "用户可能是工程师", source="ai_inferred")
    out = mem.profile_summary(uid)
    ids = [p["id"] for p in out["pending_inferred"]]
    assert inf in ids
    assert all(p["content"] for p in out["pending_inferred"])
    # user_stated 不进待确认列表
    assert "用户明说住北京" not in [p["content"] for p in out["pending_inferred"]]


def test_profile_summary_no_pending_when_no_inferred(mem):
    uid = "P4"
    mem.add_fact(uid, "用户明说喜欢咖啡", source="user_stated")
    assert mem.profile_summary(uid)["pending_inferred"] == []


def test_confirm_inferred_promotes_to_stable_user_stated(mem):
    uid = "P5"
    inf = mem.add_fact(uid, "用户可能养狗", source="ai_inferred")
    # R16：确认返回被确认的 content（供审计留痕）
    assert mem.confirm_inferred_fact(inf) == "用户可能养狗"
    assert _source(mem, inf) == "user_stated"
    assert _tier(mem, inf) == "stable"
    # 确认后不再出现在待确认列表
    out = mem.profile_summary(uid)
    assert inf not in [p["id"] for p in out["pending_inferred"]]
    assert out["stable"] == 1 and out["user_stated"] == 1


def test_confirm_only_affects_inferred(mem):
    uid = "P6"
    stated = mem.add_fact(uid, "用户明说在上海", source="user_stated")
    # 已是 user_stated → confirm 不命中（返回 None）
    assert mem.confirm_inferred_fact(stated) is None


def test_confirm_missing_row_returns_none(mem):
    assert mem.confirm_inferred_fact(999999) is None
    assert mem.confirm_inferred_fact("bad") is None


# ── R17：全库 AI 推断计数 ───────────────────────────────────────────────

def test_inferred_counts_empty(mem):
    assert mem.inferred_counts() == {"pending": 0, "total": 0}


def test_inferred_counts_pending_vs_total(mem):
    mem.add_fact("u1", "推断A", source="ai_inferred")  # raw → pending
    b = mem.add_fact("u2", "推断B", source="ai_inferred")
    c = mem.add_fact("u3", "推断C", source="ai_inferred")
    mem.add_fact("u4", "明说D", source="user_stated")  # 不计
    # B 晋升 stable、C 置 stale —— 都不再算 pending，但仍是 ai_inferred 计入 total
    mem._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (b,))
    mem._conn.execute("UPDATE episodic_memory SET tier='stale' WHERE id=?", (c,))
    mem._conn.commit()
    out = mem.inferred_counts()
    assert out["total"] == 3   # A/B/C 均 ai_inferred
    assert out["pending"] == 1  # 仅 A 是 raw


def test_inferred_counts_excludes_confirmed(mem):
    inf = mem.add_fact("u1", "推断X", source="ai_inferred")
    mem.confirm_inferred_fact(inf)  # 翻成 user_stated，移出 ai_inferred 集合
    assert mem.inferred_counts() == {"pending": 0, "total": 0}


def test_legacy_db_backfills_source(tmp_path):
    """旧库（无 source 列）ALTER 升列后默认 user_stated，可读写。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
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
        "INSERT INTO episodic_memory (user_id, content, content_hash, created_at)"
        " VALUES ('u1', '旧事实内容', 'h1', 1000.0)"
    )
    conn.commit()
    conn.close()

    store = EpisodicMemoryStore(db)
    rid = store._conn.execute(
        "SELECT id FROM episodic_memory WHERE content_hash = 'h1'"
    ).fetchone()[0]
    assert _source(store, rid) == "user_stated"
    new = store.add_fact("u1", "新事实内容", source="ai_inferred")
    assert _source(store, new) == "ai_inferred"
    store.close()
