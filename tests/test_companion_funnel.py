"""Stage 3：付费解锁预告转化漏斗 CompanionFunnelStore + EntitlementStore.paid_events_for。"""

from __future__ import annotations

from src.utils.companion_funnel_store import (
    CompanionFunnelStore,
    get_companion_funnel_store,
    reset_companion_funnel_store,
)
from src.utils.entitlement_store import EntitlementStore

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _store():
    return CompanionFunnelStore(":memory:")


# ── 写入 / 基础查询 ─────────────────────────────────────────────────────

def test_record_and_count():
    s = _store()
    assert s.record_teaser("tg:a:u1", "beach_trip", "story_ch1", now=_NOW) is not None
    assert s.record_teaser("tg:a:u2", "beach_trip", "story_ch1", now=_NOW) is not None
    assert s.count() == 2


def test_record_blank_key_skipped():
    s = _store()
    assert s.record_teaser("", "beach_trip", "story_ch1", now=_NOW) is None
    assert s.count() == 0


def test_recent_orders_desc():
    s = _store()
    s.record_teaser("u1", "a", "f1", now=_NOW - 10)
    s.record_teaser("u2", "b", "f2", now=_NOW)
    rows = s.recent(limit=10)
    assert rows[0]["contact_key"] == "u2"
    assert rows[1]["contact_key"] == "u1"


# ── 漏斗统计：无 paid_lookup（仅触达） ────────────────────────────────────

def test_funnel_without_paid_lookup_zero_conversions():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)  # 同人两次预告
    s.record_teaser("u2", "starry", "all_story", now=_NOW)
    st = s.funnel_stats(now=_NOW + 60)
    assert st["teasers"] == 3
    assert st["contacts_teased"] == 2
    assert st["conversions"] == 0
    assert st["conversion_rate"] == 0.0
    # by_scenario：beach_trip 2 条/1 人，starry 1 条/1 人
    by = {x["scenario_id"]: x for x in st["by_scenario"]}
    assert by["beach_trip"]["teasers"] == 2
    assert by["beach_trip"]["contacts"] == 1
    assert by["starry"]["teasers"] == 1


# ── 漏斗统计：注入 paid_lookup 归因 ──────────────────────────────────────

def test_funnel_attributes_conversion_within_window():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)
    s.record_teaser("u2", "beach_trip", "story_ch1", now=_NOW)

    def paid_lookup(keys):
        # u1 预告后 3 天买了 story_ch1（精确命中）；u2 没买
        return {"u1": [{"item_id": "story_ch1", "kind": "unlock", "ts": _NOW + 3 * _DAY}]}

    st = s.funnel_stats(paid_lookup=paid_lookup, now=_NOW + 5 * _DAY,
                        window_days=30, attribution_days=14)
    assert st["contacts_teased"] == 2
    assert st["conversions"] == 1
    assert st["feature_conversions"] == 1  # item_id 命中 feature
    assert st["conversion_rate"] == 0.5
    by = {x["scenario_id"]: x for x in st["by_scenario"]}
    assert by["beach_trip"]["conversions"] == 1


def test_funnel_paid_outside_attribution_window_not_counted():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)

    def paid_lookup(keys):
        # 预告后 20 天才买 → 超出 14 天归因窗 → 不算转化
        return {"u1": [{"item_id": "story_ch1", "kind": "unlock", "ts": _NOW + 20 * _DAY}]}

    st = s.funnel_stats(paid_lookup=paid_lookup, now=_NOW + 30 * _DAY,
                        window_days=60, attribution_days=14)
    assert st["conversions"] == 0


def test_funnel_paid_before_teaser_not_counted():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)

    def paid_lookup(keys):
        # 预告之前就买的 → 不能归因给预告
        return {"u1": [{"item_id": "story_ch1", "kind": "unlock", "ts": _NOW - _DAY}]}

    st = s.funnel_stats(paid_lookup=paid_lookup, now=_NOW + _DAY)
    assert st["conversions"] == 0


def test_funnel_subscribe_counts_as_conversion_but_not_feature():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)

    def paid_lookup(keys):
        # 订阅（非精确 item 命中）→ 算转化，但不算精确转化
        return {"u1": [{"item_id": "vip", "kind": "subscribe", "ts": _NOW + 2 * _DAY}]}

    st = s.funnel_stats(paid_lookup=paid_lookup, now=_NOW + 5 * _DAY)
    assert st["conversions"] == 1
    assert st["feature_conversions"] == 0


def test_funnel_window_excludes_old_teasers():
    s = _store()
    s.record_teaser("u_old", "beach_trip", "story_ch1", now=_NOW - 40 * _DAY)
    s.record_teaser("u_new", "beach_trip", "story_ch1", now=_NOW)
    st = s.funnel_stats(now=_NOW + 60, window_days=30)
    assert st["teasers"] == 1
    assert st["contacts_teased"] == 1


def test_funnel_paid_lookup_error_swallowed():
    s = _store()
    s.record_teaser("u1", "beach_trip", "story_ch1", now=_NOW)

    def boom(keys):
        raise RuntimeError("db down")

    st = s.funnel_stats(paid_lookup=boom, now=_NOW + 60)
    assert st["conversions"] == 0  # 归因失败 → 退回 0，不抛


def test_funnel_empty_store():
    st = _store().funnel_stats(now=_NOW)
    assert st["teasers"] == 0
    assert st["contacts_teased"] == 0
    assert st["by_scenario"] == []


# ── 单例 ────────────────────────────────────────────────────────────────

def test_singleton_reuse_and_reset():
    reset_companion_funnel_store()
    s1 = get_companion_funnel_store(":memory:")
    s2 = get_companion_funnel_store()
    assert s1 is s2
    reset_companion_funnel_store()
    s3 = get_companion_funnel_store(":memory:")
    assert s3 is not s1


# ── Stage B：自拍/形象照（exclusive_album）转化漏斗 ──────────────────────

def test_selfie_record_kinds_and_count():
    s = _store()
    assert s.record_selfie("u1", "locked", now=_NOW) is not None
    assert s.record_selfie("u1", "delivered", now=_NOW) is not None
    assert s.record_selfie("u2", "too_soon", now=_NOW) is not None
    assert s.selfie_count() == 3


def test_selfie_record_rejects_bad_kind_and_blank_key():
    s = _store()
    assert s.record_selfie("u1", "bogus", now=_NOW) is None
    assert s.record_selfie("", "locked", now=_NOW) is None
    assert s.selfie_count() == 0


def test_selfie_recent_orders_desc():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW - 10)
    s.record_selfie("u2", "delivered", now=_NOW)
    rows = s.selfie_recent(limit=10)
    assert rows[0]["contact_key"] == "u2"
    assert rows[1]["contact_key"] == "u1"


def test_selfie_funnel_counts_by_kind_without_paid_lookup():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)
    s.record_selfie("u1", "delivered", now=_NOW)  # 同人两态
    s.record_selfie("u2", "too_soon", now=_NOW)
    s.record_selfie("u3", "locked", now=_NOW)
    st = s.selfie_funnel_stats(now=_NOW + 60)
    assert st["requests"] == 4
    assert st["contacts"] == 3
    assert st["locked"] == 2
    assert st["delivered"] == 1
    assert st["too_soon"] == 1
    assert st["locked_contacts"] == 2  # u1, u3
    assert st["conversions"] == 0
    assert st["conversion_rate"] == 0.0


def test_selfie_funnel_attributes_locked_to_album_purchase():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)
    s.record_selfie("u2", "locked", now=_NOW)

    def paid_lookup(keys):
        # u1 触墙后 3 天买 exclusive_album → 转化；u2 没买
        return {"u1": [{"item_id": "exclusive_album", "kind": "unlock",
                        "ts": _NOW + 3 * _DAY}]}

    st = s.selfie_funnel_stats(paid_lookup=paid_lookup, now=_NOW + 5 * _DAY,
                               window_days=30, attribution_days=14)
    assert st["locked_contacts"] == 2
    assert st["conversions"] == 1
    assert st["conversion_rate"] == 0.5


def test_selfie_funnel_only_album_item_counts():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)

    def paid_lookup(keys):
        # 买的是别的项（非 exclusive_album）→ 不算自拍墙转化
        return {"u1": [{"item_id": "story_ch1", "kind": "unlock", "ts": _NOW + _DAY}]}

    st = s.selfie_funnel_stats(paid_lookup=paid_lookup, now=_NOW + 5 * _DAY)
    assert st["conversions"] == 0


def test_selfie_funnel_only_locked_cohort_converts():
    s = _store()
    s.record_selfie("u1", "delivered", now=_NOW)   # 免费送达，没触墙
    s.record_selfie("u1", "too_soon", now=_NOW)

    def paid_lookup(keys):
        return {"u1": [{"item_id": "exclusive_album", "kind": "unlock",
                        "ts": _NOW + _DAY}]}

    st = s.selfie_funnel_stats(paid_lookup=paid_lookup, now=_NOW + 5 * _DAY)
    assert st["locked_contacts"] == 0  # 没人触墙
    assert st["conversions"] == 0


def test_selfie_funnel_paid_outside_window_not_counted():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)

    def paid_lookup(keys):
        return {"u1": [{"item_id": "exclusive_album", "kind": "unlock",
                        "ts": _NOW + 20 * _DAY}]}

    st = s.selfie_funnel_stats(paid_lookup=paid_lookup, now=_NOW + 30 * _DAY,
                               window_days=60, attribution_days=14)
    assert st["conversions"] == 0


def test_selfie_funnel_paid_before_locked_not_counted():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)

    def paid_lookup(keys):
        return {"u1": [{"item_id": "exclusive_album", "kind": "unlock",
                        "ts": _NOW - _DAY}]}

    st = s.selfie_funnel_stats(paid_lookup=paid_lookup, now=_NOW + _DAY)
    assert st["conversions"] == 0


def test_selfie_funnel_window_excludes_old_events():
    s = _store()
    s.record_selfie("u_old", "locked", now=_NOW - 40 * _DAY)
    s.record_selfie("u_new", "locked", now=_NOW)
    st = s.selfie_funnel_stats(now=_NOW + 60, window_days=30)
    assert st["requests"] == 1
    assert st["locked_contacts"] == 1


def test_selfie_funnel_paid_lookup_error_swallowed():
    s = _store()
    s.record_selfie("u1", "locked", now=_NOW)

    def boom(keys):
        raise RuntimeError("db down")

    st = s.selfie_funnel_stats(paid_lookup=boom, now=_NOW + 60)
    assert st["conversions"] == 0


def test_selfie_funnel_empty_store():
    st = _store().selfie_funnel_stats(now=_NOW)
    assert st["requests"] == 0
    assert st["contacts"] == 0
    assert st["locked_contacts"] == 0


def test_selfie_funnel_counts_capped_kind():
    s = _store()
    s.record_selfie("u1", "capped", now=_NOW)
    s.record_selfie("u1", "delivered", now=_NOW)
    s.record_selfie("u2", "capped", now=_NOW)
    st = s.selfie_funnel_stats(now=_NOW + 60)
    assert st["capped"] == 2
    assert st["delivered"] == 1
    assert st["requests"] == 3


# ── EntitlementStore.paid_events_for（漏斗的真实 paid_lookup 底座） ─────────

def test_paid_events_for_batched_and_paid_only():
    es = EntitlementStore(":memory:")
    es.record_unlock("u1", "story_ch1", source="manual", now=_NOW, record_ledger=True)
    es.record_unlock("u2", "story_ch2", source="manual", now=_NOW + 10, record_ledger=True)
    # 退款流水不应计入（status != paid）
    es.record_tx(contact_key="u1", kind="unlock", item_id="x", amount=1,
                 status="refunded", now=_NOW + 20)
    out = es.paid_events_for(["u1", "u2", "u3"])
    assert "u3" not in out
    u1_items = [e["item_id"] for e in out["u1"]]
    assert "story_ch1" in u1_items
    assert "x" not in u1_items  # refunded 排除
    assert out["u2"][0]["item_id"] == "story_ch2"


def test_paid_events_for_since_filter():
    es = EntitlementStore(":memory:")
    es.record_unlock("u1", "old", now=_NOW - 100 * _DAY)
    es.record_unlock("u1", "new", now=_NOW)
    out = es.paid_events_for(["u1"], since=_NOW - _DAY)
    items = [e["item_id"] for e in out["u1"]]
    assert items == ["new"]


def test_paid_events_for_empty_keys():
    es = EntitlementStore(":memory:")
    assert es.paid_events_for([]) == {}
