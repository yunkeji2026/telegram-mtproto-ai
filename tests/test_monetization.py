"""Phase K2：C 端变现纯函数 + EntitlementStore 单测。"""
import time

from src.utils.entitlement_store import EntitlementStore
from src.utils.monetization import (
    DEFAULT_CATALOG,
    effective_tier,
    entitlement_allows,
    feature_allowed,
    merge_catalog,
    proactive_quota_allowed,
    best_tier,
    quote,
    revenue_from_txs,
    subscription_active,
    tier_grants,
    tier_rank,
    upsell_offer,
    upsell_pitch_hint,
)

NOW = 1_700_000_000.0
DAY = 86400.0


# ── 纯函数 ────────────────────────────────────────────────────────────
def test_tier_grants_known_and_unknown():
    assert "voice_reply" in tier_grants("vip")
    assert "all_story" in tier_grants("svip")
    assert tier_grants("free") == set()
    assert tier_grants("nope") == set()


def test_subscription_active_window():
    assert subscription_active(NOW + DAY, NOW) is True
    assert subscription_active(NOW - 1, NOW) is False
    assert subscription_active(0, NOW) is False


def test_effective_tier_downgrades_on_expiry():
    assert effective_tier("vip", NOW + DAY, NOW) == "vip"
    assert effective_tier("vip", NOW - 1, NOW) == "free"
    assert effective_tier("free", NOW + DAY, NOW) == "free"


def test_entitlement_allows_by_grant_or_unlock():
    ent = {"grants": ["voice_reply"], "unlocked": ["story_ch1"]}
    assert entitlement_allows(ent, "voice_reply") is True
    assert entitlement_allows(ent, "story_ch1") is True
    assert entitlement_allows(ent, "exclusive_persona") is False


def test_feature_allowed_gate_off_always_true():
    ent = {"grants": [], "unlocked": []}
    # gate 关 → 恒放行（零破坏）
    assert feature_allowed(ent, "voice_reply", gate_enabled=False) is True
    # gate 开 → 按权益
    assert feature_allowed(ent, "voice_reply", gate_enabled=True) is False
    assert feature_allowed({"grants": ["voice_reply"]}, "voice_reply", gate_enabled=True) is True
    # None entitlement 视为 free
    assert feature_allowed(None, "voice_reply", gate_enabled=True) is False


def test_quote_kinds():
    assert quote("subscribe", "vip")["amount"] == 9.9
    assert quote("unlock", "story_ch1")["amount"] == 1.99
    assert quote("gift", "rose")["amount"] == 0.99
    assert quote("subscribe", "nope") is None
    assert quote("bogus", "x") is None


def test_merge_catalog_deep_merge():
    cat = merge_catalog({"currency": "CNY", "tiers": {"vip": {"monthly": 18}}})
    assert cat["currency"] == "CNY"
    assert cat["tiers"]["vip"]["monthly"] == 18
    # 未覆盖字段保留默认
    assert "voice_reply" in cat["tiers"]["vip"]["grants"]
    # 默认目录不被污染
    assert DEFAULT_CATALOG["currency"] == "USD"


def test_upsell_offer_recommends_cheapest_tier():
    ent = {"grants": [], "unlocked": []}
    offer = upsell_offer(ent, "voice_reply", gate_enabled=True)
    assert offer is not None
    assert offer["kind"] == "subscribe"
    assert offer["tier"] == "vip"      # vip(9.9) 比 svip(29.9) 便宜且含 voice_reply
    assert offer["amount"] == 9.9


def test_upsell_offer_none_when_already_has_or_gate_off():
    ent = {"grants": ["voice_reply"], "unlocked": []}
    assert upsell_offer(ent, "voice_reply", gate_enabled=True) is None  # 已拥有
    assert upsell_offer({"grants": []}, "voice_reply", gate_enabled=False) is None  # gate 关
    assert upsell_offer({"grants": []}, "", gate_enabled=True) is None  # 无 feature


def test_upsell_offer_falls_back_to_item_unlock():
    # exclusive_persona 只在 svip → 选 svip；不存在的 feature → 看同名 item
    offer = upsell_offer({"grants": []}, "exclusive_album", gate_enabled=True)
    # exclusive_album 不是任何 tier 的 grant，但是 items 里有同名解锁项
    assert offer is not None and offer["kind"] == "unlock"
    assert offer["item_id"] == "exclusive_album"


def test_upsell_pitch_hint_tone():
    offer = upsell_offer({"grants": []}, "voice_reply", gate_enabled=True)
    hint = upsell_pitch_hint(offer, persona_name="小柔")
    assert "VIP" in hint and "小柔" in hint
    assert upsell_pitch_hint(None) == ""


def test_proactive_quota_allowed():
    free = {"grants": [], "unlocked": []}
    vip = {"grants": ["unlimited_proactive"], "unlocked": []}
    # gate 关 → 恒放行
    assert proactive_quota_allowed(free, 99, free_quota=1, gate_enabled=False) is True
    # VIP → 不限
    assert proactive_quota_allowed(vip, 99, free_quota=1, gate_enabled=True) is True
    # 免费用户：未超额放行，超额拦
    assert proactive_quota_allowed(free, 0, free_quota=1, gate_enabled=True) is True
    assert proactive_quota_allowed(free, 1, free_quota=1, gate_enabled=True) is False


def test_revenue_from_txs_skips_non_paid():
    txs = [
        {"kind": "subscribe", "amount": 9.9, "status": "paid"},
        {"kind": "gift", "amount": 0.99},  # status 缺省视为 paid
        {"kind": "unlock", "amount": 1.99, "status": "refunded"},  # 不计
    ]
    rev = revenue_from_txs(txs)
    assert rev["total"] == 10.89
    assert rev["count"] == 2
    assert rev["by_kind"]["subscribe"]["amount"] == 9.9


def test_tier_rank_and_best_tier():
    assert tier_rank("svip") > tier_rank("vip") > tier_rank("free")
    assert tier_rank("nope") == 0.0
    assert best_tier(["free", "vip", "svip"]) == "svip"
    assert best_tier(["vip", "free"]) == "vip"
    assert best_tier([]) == "free"
    assert best_tier(["free", "free"]) == "free"


# ── EntitlementStore ──────────────────────────────────────────────────
def test_store_spend_and_tiers_by_contacts():
    s = EntitlementStore(":memory:")
    s.record_gift("c1", "rose", amount=5.0)
    s.record_gift("c1", "rose", amount=3.0)
    s.record_tx(contact_key="c2", kind="gift", amount=10.0, status="paid")
    s.record_tx(contact_key="c3", kind="gift", amount=99.0, status="refunded")
    s.grant_subscription("c1", "vip", active_until=time.time() + 86400,
                         record_ledger=False)
    s.grant_subscription("c2", "svip", active_until=time.time() - 10,
                         record_ledger=False)  # 已过期
    spend = s.spend_by_contacts(["c1", "c2", "c3", "missing"])
    assert spend["c1"] == 8.0
    assert spend["c2"] == 10.0
    assert "c3" not in spend  # refunded 不计
    assert "missing" not in spend
    tiers = s.tiers_by_contacts(["c1", "c2", "c3"])
    assert tiers["c1"] == "vip"
    assert "c2" not in tiers  # 过期不返回
    assert s.spend_by_contacts([]) == {}


def test_store_grant_subscription_and_entitlement():
    s = EntitlementStore(":memory:")
    assert s.grant_subscription("c1", "vip", NOW + 30 * DAY, now=NOW) is True
    ent = s.get_entitlement("c1", now=NOW)
    assert ent["tier"] == "vip"
    assert ent["active"] is True
    assert "voice_reply" in ent["grants"]
    # 过期后降级 free
    ent2 = s.get_entitlement("c1", now=NOW + 31 * DAY)
    assert ent2["tier"] == "free"
    assert ent2["grants"] == []


def test_store_unlock_idempotent():
    s = EntitlementStore(":memory:")
    assert s.record_unlock("c1", "story_ch1", now=NOW) is True
    assert s.record_unlock("c1", "story_ch1", now=NOW) is False  # 已持有
    assert s.is_unlocked("c1", "story_ch1") is True
    assert "story_ch1" in s.get_entitlement("c1", now=NOW)["unlocked"]
    # 只入账一次
    assert s.count_tx() == 1


def test_store_tx_ref_idempotent():
    s = EntitlementStore(":memory:")
    a = s.record_tx(contact_key="c1", kind="gift", amount=0.99, ref="pay_1", now=NOW)
    b = s.record_tx(contact_key="c1", kind="gift", amount=0.99, ref="pay_1", now=NOW)
    assert a is not None
    assert b is None  # 重复 ref 幂等跳过
    assert s.count_tx() == 1


def test_store_grant_subscription_ref_idempotent():
    s = EntitlementStore(":memory:")
    assert s.grant_subscription("c1", "vip", NOW + 30 * DAY, ref="sub_1", now=NOW) is True
    # 同 ref 重投 → 不重复入账也不改订阅
    assert s.grant_subscription("c1", "svip", NOW + 60 * DAY, ref="sub_1", now=NOW) is False
    assert s.get_entitlement("c1", now=NOW)["tier"] == "vip"
    assert s.count_tx() == 1


def test_store_revenue_summary_and_top_spenders():
    s = EntitlementStore(":memory:")
    s.grant_subscription("c1", "vip", NOW + 30 * DAY, now=NOW)        # 9.9
    s.record_unlock("c1", "story_ch1", now=NOW)                       # 1.99
    s.record_gift("c2", "crown", now=NOW)                            # 9.99
    rev = s.revenue_summary(since=NOW - DAY, until=NOW + DAY)
    assert rev["total"] == round(9.9 + 1.99 + 9.99, 2)
    assert rev["by_kind"]["subscribe"]["count"] == 1
    top = s.top_spenders(since=NOW - DAY, until=NOW + DAY, limit=5)
    assert top[0]["contact_key"] == "c1"  # 9.9+1.99 = 11.89 > 9.99
    assert top[0]["spent"] == 11.89


def test_store_active_subscription_count_excludes_expired_and_free():
    s = EntitlementStore(":memory:")
    s.grant_subscription("c1", "vip", NOW + 30 * DAY, now=NOW)
    s.grant_subscription("c2", "svip", NOW - DAY, now=NOW)  # 已过期
    assert s.active_subscription_count(now=NOW) == 1
    assert s.active_subscription_count(now=NOW + 31 * DAY) == 0


def test_store_expire_subscriptions():
    s = EntitlementStore(":memory:")
    s.grant_subscription("c1", "vip", NOW - DAY, now=NOW)  # 已过期但 status=active
    n = s.expire_subscriptions(now=NOW)
    assert n == 1
    row = s._subscription_row("c1")
    assert row["status"] == "expired"


def test_store_never_raises_on_bad_input():
    s = EntitlementStore(":memory:")
    assert s.grant_subscription("", "vip", NOW + DAY) is False
    assert s.record_unlock("c1", "") is False
    assert s.get_entitlement("nobody")["tier"] == "free"


# ── MonetizationRuntime ───────────────────────────────────────────────
def _runtime(gate=True, free_quota=1):
    from src.utils.monetization_runtime import MonetizationRuntime
    s = EntitlementStore(":memory:")
    cfg = {"enabled": True, "gate": {"enabled": gate},
           "upsell": {"free_proactive_daily": free_quota}}
    return MonetizationRuntime(store=s, mon_cfg=cfg), s


def test_runtime_feature_check_allows_when_gate_off():
    rt, _ = _runtime(gate=False)
    res = rt.feature_check("c1", "voice_reply")
    assert res["allowed"] is True
    assert res["upsell"] is None


def test_runtime_feature_check_denies_free_with_upsell():
    rt, _ = _runtime(gate=True)
    res = rt.feature_check("c1", "voice_reply")
    assert res["allowed"] is False
    assert res["upsell"]["tier"] == "vip"
    assert "pitch_hint" in res


def test_runtime_feature_check_allows_subscriber():
    rt, store = _runtime(gate=True)
    store.grant_subscription("c1", "vip", NOW + 30 * DAY, now=NOW)
    # entitlement_for 用当前时间；用足够长有效期保证仍有效
    store.grant_subscription("c1", "vip", time.time() + 30 * DAY)
    res = rt.feature_check("c1", "voice_reply")
    assert res["allowed"] is True
    assert res["upsell"] is None


def test_runtime_proactive_allowed_quota():
    rt, _ = _runtime(gate=True, free_quota=1)
    assert rt.proactive_allowed("c1", 0) is True
    assert rt.proactive_allowed("c1", 1) is False  # 超免费配额
    rt_off, _ = _runtime(gate=False)
    assert rt_off.proactive_allowed("c1", 99) is True  # gate 关恒放行


# ── CareScheduleStore.count_sent_since ────────────────────────────────
def test_care_store_count_sent_since():
    from src.contacts.care_commitment import CareCommitment
    from src.contacts.care_schedule import CareScheduleStore
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=time.time(), event_at=time.time(), topic="x",
                       sentiment="neutral", anchor_text="a", source_text="s",
                       confidence=1.0)
    sid = s.add_commitment(c, contact_key="c1", platform="messenger", chat_key="fb:1",
                           min_confidence=0.0, dedup_window_days=0.0)
    assert s.count_sent_since("c1", 0) == 0  # 还没发
    s.mark_sent(sid)
    assert s.count_sent_since("c1", 0) == 1
    assert s.count_sent_since("c1", time.time() + 100) == 0  # 窗口起点在未来
