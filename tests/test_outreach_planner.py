"""P61-3：分组批量触达 dry-run 规划层 + outreach_log 存储 契约测试。"""

import time

from src.inbox.models import InboxConversation
from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner
from src.inbox.store import InboxStore

DAY = 86400.0


def _store_with_convs(specs):
    """specs: list of dict(cid, platform, account_id, last_ts, tags?, archived?, rel?)."""
    store = InboxStore(":memory:")
    for s in specs:
        conv = InboxConversation(
            conversation_id=s["cid"], platform=s.get("platform", "telegram"),
            account_id=s.get("account_id", "default"), chat_key=s["cid"].split(":")[-1],
            display_name=s.get("name", s["cid"]), last_ts=s["last_ts"],
        )
        store.ingest_batch(conv, [])
        if s.get("tags"):
            store.set_conv_tags(s["cid"], s["tags"])
        if s.get("archived"):
            store.set_conv_archived(s["cid"], True)
        if s.get("rel"):
            store.set_rel_stage_cached(s["cid"], s["rel"])
    return store


class _FakeLimiter:
    def __init__(self, caps):
        self._caps = caps

    def remaining_for(self, account_id, *, now=None):
        return self._caps.get(account_id, 0)


# ── select_segment ────────────────────────────────────────────────────────
def test_min_silent_days_filter():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY},   # 沉默5天
        {"cid": "telegram:a:2", "last_ts": now - 1 * DAY},   # 沉默1天
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(min_silent_days=3), now=now)
    assert [t.conversation_id for t in seg] == ["telegram:a:1"]


def test_max_silent_days_excludes_too_old():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY},
        {"cid": "telegram:a:2", "last_ts": now - 40 * DAY},  # 流失太久
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(min_silent_days=3, max_silent_days=30), now=now)
    assert [t.conversation_id for t in seg] == ["telegram:a:1"]


def test_tags_any_filter():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY, "tags": ["vip"]},
        {"cid": "telegram:a:2", "last_ts": now - 5 * DAY, "tags": ["cold"]},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(tags_any=["vip"]), now=now)
    assert [t.conversation_id for t in seg] == ["telegram:a:1"]


def test_rel_stage_filter():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY, "rel": "bonded"},
        {"cid": "telegram:a:2", "last_ts": now - 5 * DAY, "rel": "new"},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(rel_stages=["bonded"]), now=now)
    assert [t.conversation_id for t in seg] == ["telegram:a:1"]


def test_exclude_archived():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY},
        {"cid": "telegram:a:2", "last_ts": now - 5 * DAY, "archived": True},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(), now=now)
    assert [t.conversation_id for t in seg] == ["telegram:a:1"]


def test_platform_filter_and_silent_sort():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 2 * DAY},
        {"cid": "telegram:a:2", "last_ts": now - 9 * DAY},
        {"cid": "line:a:3", "last_ts": now - 5 * DAY, "platform": "line"},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(platform="telegram"), now=now)
    # 仅 telegram，且最沉默优先
    assert [t.conversation_id for t in seg] == ["telegram:a:2", "telegram:a:1"]


# ── build_plan ─────────────────────────────────────────────────────────────
def test_account_cap_distribution():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY, "account_id": "a"},
        {"cid": "telegram:a:2", "last_ts": now - 6 * DAY, "account_id": "a"},
        {"cid": "telegram:a:3", "last_ts": now - 7 * DAY, "account_id": "a"},
    ])
    p = OutreachPlanner(store, limiter=_FakeLimiter({"a": 2}), cooldown_days=0)
    plan = p.build_plan(OutreachFilters(), now=now)
    assert len(plan.eligible) == 2
    assert any(s["reason"] == "account_cap" for s in plan.skipped)
    assert plan.per_account["a"]["assigned"] == 2
    assert plan.per_account["a"]["cap"] == 2


def test_cooldown_skips_recent():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY, "account_id": "a"},
        {"cid": "telegram:a:2", "last_ts": now - 6 * DAY, "account_id": "a"},
    ])
    # conv1 最近触达过（2 天前），cooldown=14 天 → 应跳过
    store.record_outreach("telegram:a:1", batch_id="b0", ts=now - 2 * DAY)
    p = OutreachPlanner(store, limiter=_FakeLimiter({"a": 99}), cooldown_days=14)
    plan = p.build_plan(OutreachFilters(), now=now)
    assert [t.conversation_id for t in plan.eligible] == ["telegram:a:2"]
    assert {"conversation_id": "telegram:a:1", "reason": "cooldown"} in plan.skipped


def test_estimated_seconds_and_todict():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": "telegram:a:1", "last_ts": now - 5 * DAY, "account_id": "a"},
        {"cid": "telegram:a:2", "last_ts": now - 6 * DAY, "account_id": "a"},
    ])
    p = OutreachPlanner(store, limiter=_FakeLimiter({"a": 99}),
                        cooldown_days=0, per_send_seconds=10)
    plan = p.build_plan(OutreachFilters(), now=now)
    d = plan.to_dict()
    assert d["eligible_count"] == 2
    assert d["estimated_seconds"] == 20.0
    assert d["total_matched"] == 2


def test_default_cap_used_without_limiter():
    now = 1_000_000.0
    store = _store_with_convs([
        {"cid": f"telegram:a:{i}", "last_ts": now - (5 + i) * DAY, "account_id": "a"}
        for i in range(5)
    ])
    p = OutreachPlanner(store, cooldown_days=0, default_account_cap=3)
    plan = p.build_plan(OutreachFilters(), now=now)
    assert len(plan.eligible) == 3
    assert sum(1 for s in plan.skipped if s["reason"] == "account_cap") == 2


# ── outreach_log 存储 ───────────────────────────────────────────────────────
def test_outreach_log_record_and_query():
    store = InboxStore(":memory:")
    t = time.time()
    store.record_outreach("c1", batch_id="b1", platform="telegram", account_id="a",
                          status="sent", ts=t - 100)
    store.record_outreach("c1", batch_id="b1", account_id="a", status="sent", ts=t)
    store.record_outreach("c2", batch_id="b1", account_id="a", status="failed", ts=t)
    assert store.last_outreach_ts("c1") == t
    assert store.last_outreach_ts("none") == 0.0
    bulk = store.last_outreach_ts_bulk(["c1", "c2"])
    assert bulk["c1"] == t and bulk["c2"] == t
    stats = store.outreach_batch_stats("b1")
    assert stats["total"] == 3
    assert stats["by_status"] == {"sent": 2, "failed": 1}
