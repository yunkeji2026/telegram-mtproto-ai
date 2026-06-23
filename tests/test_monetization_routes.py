"""Phase K2：/api/monetize/* 路由契约 + 支付回调桩。"""
import hashlib
import hmac
import json
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils.entitlement_store import EntitlementStore
from src.utils.payment_gateway import encode_invoice_payload
from src.web.routes.monetization_routes import register_monetization_routes


class _CM:
    def __init__(self, cfg):
        self.config = cfg
        self.config_path = ""


def _client(mon_cfg=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    cm = _CM({"monetization": mon_cfg or {"enabled": True}})
    app.state.config_manager = cm
    register_monetization_routes(app, api_auth=_auth, config_manager=cm)
    app.state.entitlement_store = EntitlementStore(":memory:")
    return TestClient(app), app.state.entitlement_store


def test_catalog_endpoint():
    client, _ = _client()
    d = client.get("/api/monetize/catalog").json()
    assert d["ok"] is True
    assert "vip" in d["catalog"]["tiers"]


def test_overview_empty():
    client, _ = _client()
    d = client.get("/api/monetize/overview").json()
    assert d["ok"] is True
    assert d["revenue"]["total"] == 0
    assert d["active_subscriptions"] == 0


def test_teaser_funnel_disabled_when_no_store():
    # 漏斗库未挂 app.state → enabled=false 空漏斗（不报错）
    client, _ = _client()
    d = client.get("/api/monetize/teaser-funnel").json()
    assert d["ok"] is True
    assert d["enabled"] is False
    assert d["funnel"]["teasers"] == 0


def test_teaser_funnel_attributes_conversion_end_to_end():
    from src.utils.companion_funnel_store import CompanionFunnelStore
    client, store = _client()
    now = time.time()
    funnel = CompanionFunnelStore(":memory:")
    client.app.state.companion_funnel_store = funnel
    # u1 被预告并随后买了 story_ch1（精确转化）；u2 仅被预告
    funnel.record_teaser("u1", "beach_trip", "story_ch1", now=now - 2 * 86400)
    funnel.record_teaser("u2", "beach_trip", "story_ch1", now=now - 2 * 86400)
    store.record_unlock("u1", "story_ch1", source="manual", now=now - 86400)
    d = client.get("/api/monetize/teaser-funnel?window_days=30&attribution_days=14").json()
    assert d["ok"] is True and d["enabled"] is True
    f = d["funnel"]
    assert f["teasers"] == 2
    assert f["contacts_teased"] == 2
    assert f["conversions"] == 1
    assert f["feature_conversions"] == 1
    assert f["conversion_rate"] == 0.5
    assert len(d["recent"]) == 2


def test_selfie_funnel_disabled_when_no_store():
    client, _ = _client()
    d = client.get("/api/monetize/selfie-funnel").json()
    assert d["ok"] is True
    assert d["enabled"] is False
    assert d["funnel"]["requests"] == 0
    assert d["funnel"]["conversions"] == 0


def test_selfie_funnel_attributes_album_conversion_end_to_end():
    from src.utils.companion_funnel_store import CompanionFunnelStore
    client, store = _client()
    now = time.time()
    funnel = CompanionFunnelStore(":memory:")
    client.app.state.companion_funnel_store = funnel
    # u1 触墙后买 exclusive_album（转化）；u2 仅触墙；u3 免费送达（不入墙群体）
    funnel.record_selfie("u1", "locked", now=now - 2 * 86400)
    funnel.record_selfie("u2", "locked", now=now - 2 * 86400)
    funnel.record_selfie("u3", "delivered", now=now - 2 * 86400)
    store.record_unlock("u1", "exclusive_album", source="manual", now=now - 86400)
    d = client.get(
        "/api/monetize/selfie-funnel?window_days=30&attribution_days=14").json()
    assert d["ok"] is True and d["enabled"] is True
    f = d["funnel"]
    assert f["requests"] == 3
    assert f["locked"] == 2
    assert f["delivered"] == 1
    assert f["locked_contacts"] == 2
    assert f["conversions"] == 1
    assert f["conversion_rate"] == 0.5
    assert len(d["recent"]) == 3


def test_monetization_page_wires_both_funnels():
    """Stage E：变现看板 UI 引用两条转化漏斗端点与渲染容器（防接线被误删）。"""
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "monetization.html"
    html = tpl.read_text(encoding="utf-8")
    assert "/api/monetize/teaser-funnel" in html
    assert "/api/monetize/selfie-funnel" in html
    assert 'id="mz-teaser-cards"' in html
    assert 'id="mz-selfie-cards"' in html
    assert "mzFunnels(" in html  # 初始加载/刷新都触发
    # Stage J：全局出图预算快照卡接线
    assert "/api/monetize/selfie-cap" in html
    assert 'id="mz-selfiecap-cards"' in html
    assert "mzSelfieCap(" in html
    # Stage K：名单下钻接线
    assert "/api/monetize/selfie-contacts" in html
    assert "/api/monetize/teaser-contacts" in html
    assert 'id="mz-drill"' in html
    assert "mzSelfieContacts(" in html
    assert "mzTeaserContacts(" in html


# ── Stage J：全局出图预算快照（/api/monetize/selfie-cap） ──────────────────

def _client_full(full_cfg):
    app = FastAPI()

    def _auth(request: Request):
        return True

    cm = _CM(full_cfg)
    app.state.config_manager = cm
    register_monetization_routes(app, api_auth=_auth, config_manager=cm)
    app.state.entitlement_store = EntitlementStore(":memory:")
    return TestClient(app)


def test_selfie_cap_unlimited_when_zero():
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    reset_selfie_cap_tracker()
    client = _client_full({"monetization": {"enabled": True},
                           "companion": {"selfie": {"enabled": True,
                                                    "daily_global_cap": 0}}})
    d = client.get("/api/monetize/selfie-cap").json()
    assert d["ok"] is True
    assert d["enabled"] is False        # cap=0 → 不限
    assert d["remaining"] == -1
    assert d["selfie_enabled"] is True


def test_selfie_cap_no_tracker_uses_config():
    from src.utils.selfie_cap import reset_selfie_cap_tracker
    reset_selfie_cap_tracker()          # 未出过图 → 跟踪器未建
    client = _client_full({"monetization": {"enabled": True},
                           "companion": {"selfie": {"enabled": True,
                                                    "daily_global_cap": 3}}})
    d = client.get("/api/monetize/selfie-cap").json()
    assert d["enabled"] is True
    assert d["daily_cap"] == 3
    assert d["daily_sent"] == 0
    assert d["remaining"] == 3
    reset_selfie_cap_tracker()


def test_selfie_cap_reports_used_and_remaining():
    from src.utils.selfie_cap import (
        get_selfie_cap_tracker, reset_selfie_cap_tracker)
    reset_selfie_cap_tracker()
    t = get_selfie_cap_tracker(5)
    t.record_sent(2)                    # 模拟今日已出 2 张
    client = _client_full({"monetization": {"enabled": True},
                           "companion": {"selfie": {"enabled": True,
                                                    "daily_global_cap": 5}}})
    d = client.get("/api/monetize/selfie-cap").json()
    assert d["enabled"] is True
    assert d["daily_cap"] == 5
    assert d["daily_sent"] == 2
    assert d["remaining"] == 3
    assert d["reset_at_ts"] > 0
    reset_selfie_cap_tracker()


# ── Stage K：名单下钻（/api/monetize/{selfie,teaser}-contacts） ─────────────

def test_selfie_contacts_disabled_when_no_store():
    client, _ = _client()
    d = client.get("/api/monetize/selfie-contacts?kind=locked").json()
    assert d["ok"] is True and d["enabled"] is False and d["items"] == []


def test_selfie_contacts_bad_kind():
    client, _ = _client()
    d = client.get("/api/monetize/selfie-contacts?kind=bogus").json()
    assert d["ok"] is False and d["reason"] == "bad_kind"


def test_selfie_contacts_locked_annotates_conversion():
    from src.utils.companion_funnel_store import CompanionFunnelStore
    client, store = _client()
    now = time.time()
    funnel = CompanionFunnelStore(":memory:")
    client.app.state.companion_funnel_store = funnel
    funnel.record_selfie("u1", "locked", now=now - 2 * 86400)
    funnel.record_selfie("u2", "locked", now=now - 2 * 86400)
    store.record_unlock("u1", "exclusive_album", source="manual", now=now - 86400)
    d = client.get(
        "/api/monetize/selfie-contacts?kind=locked&window_days=30&attribution_days=14").json()
    assert d["ok"] is True and d["enabled"] is True and d["kind"] == "locked"
    by = {it["contact_key"]: it for it in d["items"]}
    assert by["u1"]["converted"] is True
    assert by["u2"]["converted"] is False


def test_selfie_contacts_capped_no_conversion_field_logic():
    from src.utils.companion_funnel_store import CompanionFunnelStore
    client, _ = _client()
    now = time.time()
    funnel = CompanionFunnelStore(":memory:")
    client.app.state.companion_funnel_store = funnel
    funnel.record_selfie("u9", "capped", now=now - 3600)
    d = client.get("/api/monetize/selfie-contacts?kind=capped").json()
    assert d["ok"] is True and d["count"] == 1
    assert d["items"][0]["contact_key"] == "u9"
    assert d["items"][0]["count"] == 1


def test_teaser_contacts_annotates_conversion_and_scenario_filter():
    from src.utils.companion_funnel_store import CompanionFunnelStore
    client, store = _client()
    now = time.time()
    funnel = CompanionFunnelStore(":memory:")
    client.app.state.companion_funnel_store = funnel
    funnel.record_teaser("u1", "beach", "story_ch1", now=now - 2 * 86400)
    funnel.record_teaser("u2", "city", "story_ch2", now=now - 2 * 86400)
    store.record_unlock("u1", "story_ch1", source="manual", now=now - 86400)
    d = client.get("/api/monetize/teaser-contacts?window_days=30").json()
    by = {it["contact_key"]: it for it in d["items"]}
    assert by["u1"]["converted"] is True
    assert by["u2"]["converted"] is False
    # 场景过滤
    d2 = client.get("/api/monetize/teaser-contacts?scenario_id=beach").json()
    assert [it["contact_key"] for it in d2["items"]] == ["u1"]


def test_grant_subscribe_then_entitlement():
    client, _ = _client()
    r = client.post("/api/monetize/grant", json={
        "contact_key": "c1", "kind": "subscribe", "item_id": "vip", "days": 30,
    })
    body = r.json()
    assert body["ok"] is True
    assert body["entitlement"]["tier"] == "vip"
    assert "voice_reply" in body["entitlement"]["grants"]

    e = client.get("/api/monetize/entitlement?contact_key=c1").json()
    assert e["ok"] is True and e["entitlement"]["active"] is True


def test_grant_unlock_and_gift_reflect_in_overview():
    client, _ = _client()
    client.post("/api/monetize/grant", json={
        "contact_key": "c1", "kind": "unlock", "item_id": "story_ch1"})
    client.post("/api/monetize/grant", json={
        "contact_key": "c2", "kind": "gift", "item_id": "rose"})
    ov = client.get("/api/monetize/overview").json()
    assert ov["revenue"]["count"] == 2
    assert ov["revenue"]["by_kind"]["unlock"]["amount"] == 1.99
    assert ov["revenue"]["by_kind"]["gift"]["amount"] == 0.99


def test_grant_bad_request():
    client, _ = _client()
    r = client.post("/api/monetize/grant", json={"contact_key": "c1", "kind": "bogus"})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "bad_request"


def test_entitlement_missing_contact():
    client, _ = _client()
    r = client.get("/api/monetize/entitlement")
    assert r.json()["ok"] is False and r.json()["reason"] == "missing"


def test_webhook_applies_and_idempotent():
    client, store = _client()
    payload = {"contact_key": "c1", "kind": "subscribe", "item_id": "vip",
               "ref": "pay_42", "days": 30}
    r1 = client.post("/api/monetize/webhook", json=payload)
    assert r1.json()["applied"] is True
    # 重投同 ref → 幂等不再 applied
    r2 = client.post("/api/monetize/webhook", json=payload)
    assert r2.json()["applied"] is False
    assert store.get_entitlement("c1")["tier"] == "vip"
    assert store.count_tx() == 1


def test_feature_check_gate_off_allows():
    client, _ = _client(mon_cfg={"enabled": True, "gate": {"enabled": False}})
    r = client.post("/api/monetize/feature-check",
                    json={"contact_key": "c1", "feature": "voice_reply"})
    d = r.json()
    assert d["ok"] is True and d["allowed"] is True
    assert d["upsell"] is None


def test_feature_check_gate_on_denies_with_upsell():
    client, _ = _client(mon_cfg={"enabled": True, "gate": {"enabled": True}})
    r = client.post("/api/monetize/feature-check",
                    json={"contact_key": "c1", "feature": "voice_reply"})
    d = r.json()
    assert d["allowed"] is False
    assert d["upsell"]["tier"] == "vip"
    assert "pitch_hint" in d


def test_feature_check_subscriber_allowed():
    client, store = _client(mon_cfg={"enabled": True, "gate": {"enabled": True}})
    store.grant_subscription("c1", "vip", time.time() + 30 * 86400)
    r = client.post("/api/monetize/feature-check",
                    json={"contact_key": "c1", "feature": "voice_reply"})
    assert r.json()["allowed"] is True


def test_feature_check_missing_fields():
    client, _ = _client()
    r = client.post("/api/monetize/feature-check", json={"contact_key": "c1"})
    assert r.json()["ok"] is False and r.json()["reason"] == "missing"


def test_webhook_secret_enforced():
    client, _ = _client(mon_cfg={"enabled": True, "webhook_secret": "s3cr3t"})
    bad = client.post("/api/monetize/webhook", json={
        "contact_key": "c1", "kind": "gift", "item_id": "rose", "ref": "g1"})
    assert bad.json()["ok"] is False and bad.json()["reason"] == "unauthorized"
    ok = client.post("/api/monetize/webhook",
                     headers={"X-Monetize-Secret": "s3cr3t"},
                     json={"contact_key": "c1", "kind": "gift", "item_id": "rose", "ref": "g1"})
    assert ok.json()["ok"] is True and ok.json()["applied"] is True


# ── ④ 支付网关：checkout + provider webhook ─────────────────────────────
def test_checkout_provider_disabled():
    client, _ = _client(mon_cfg={"enabled": True})  # 无 providers
    r = client.post("/api/monetize/checkout", json={
        "contact_key": "c1", "kind": "subscribe", "item_id": "vip",
        "provider": "stripe"})
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "provider_disabled"


def test_checkout_unknown_item_and_provider():
    client, _ = _client(mon_cfg={"enabled": True})
    r = client.post("/api/monetize/checkout", json={
        "contact_key": "c1", "kind": "subscribe", "item_id": "nope",
        "provider": "stripe"})
    assert r.json()["reason"] == "unknown_item"
    r2 = client.post("/api/monetize/checkout", json={
        "contact_key": "c1", "kind": "gift", "item_id": "rose",
        "provider": "paypal"})
    assert r2.json()["reason"] == "unknown_provider"


def _stripe_sig(raw: bytes, secret: str, t: int) -> str:
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + raw,
                  hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def test_webhook_stripe_bad_signature():
    client, _ = _client(mon_cfg={
        "enabled": True, "providers": {"stripe": {"webhook_secret": "whsec_x"}}})
    r = client.post("/api/monetize/webhook/stripe", content=b"{}",
                    headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.json()["ok"] is False and r.json()["reason"] == "bad_signature"


def test_webhook_stripe_applies_idempotent():
    client, store = _client(mon_cfg={
        "enabled": True, "providers": {"stripe": {"webhook_secret": "whsec_x"}}})
    event = {"id": "evt_9", "type": "checkout.session.completed",
             "data": {"object": {"amount_total": 990, "currency": "usd",
                                  "metadata": {"contact_key": "c1",
                                               "kind": "subscribe",
                                               "item_id": "vip", "days": "30"}}}}
    raw = json.dumps(event).encode()
    t = int(time.time())
    hdr = {"Stripe-Signature": _stripe_sig(raw, "whsec_x", t)}
    r1 = client.post("/api/monetize/webhook/stripe", content=raw, headers=hdr)
    assert r1.json()["applied"] is True
    assert store.get_entitlement("c1")["tier"] == "vip"
    # 重投同事件 id → 幂等
    r2 = client.post("/api/monetize/webhook/stripe", content=raw, headers=hdr)
    assert r2.json()["applied"] is False


def test_webhook_stripe_invoice_paid_recurring():
    client, store = _client(mon_cfg={
        "enabled": True, "providers": {"stripe": {"webhook_secret": "whsec_x"}}})
    # 订阅首付的 checkout.session.completed（mode=subscription）应被忽略（让位 invoice.paid）
    sess = {"id": "evt_s", "type": "checkout.session.completed",
            "data": {"object": {"mode": "subscription", "amount_total": 990,
                                 "currency": "usd",
                                 "metadata": {"contact_key": "c1",
                                              "kind": "subscribe",
                                              "item_id": "vip", "days": "30"}}}}
    raw_s = json.dumps(sess).encode()
    t = int(time.time())
    r0 = client.post("/api/monetize/webhook/stripe", content=raw_s,
                     headers={"Stripe-Signature": _stripe_sig(raw_s, "whsec_x", t)})
    assert r0.json()["applied"] is False
    # invoice.paid（首付/续费）真正发权益
    inv = {"id": "evt_i", "type": "invoice.paid",
           "data": {"object": {"id": "in_1", "amount_paid": 990, "currency": "usd",
                               "subscription_details": {"metadata": {
                                   "contact_key": "c1", "kind": "subscribe",
                                   "item_id": "vip", "days": "30"}}}}}
    raw_i = json.dumps(inv).encode()
    r1 = client.post("/api/monetize/webhook/stripe", content=raw_i,
                     headers={"Stripe-Signature": _stripe_sig(raw_i, "whsec_x", t)})
    assert r1.json()["applied"] is True
    assert store.get_entitlement("c1")["tier"] == "vip"


def test_webhook_stripe_cancellation_expires():
    client, store = _client(mon_cfg={
        "enabled": True, "providers": {"stripe": {"webhook_secret": "whsec_x"}}})
    store.grant_subscription("c1", "vip", time.time() + 30 * 86400,
                             record_ledger=False)
    assert store.get_entitlement("c1")["active"] is True
    event = {"id": "evt_c", "type": "customer.subscription.deleted",
             "data": {"object": {"id": "sub_1",
                                 "metadata": {"contact_key": "c1"}}}}
    raw = json.dumps(event).encode()
    t = int(time.time())
    r = client.post("/api/monetize/webhook/stripe", content=raw,
                    headers={"Stripe-Signature": _stripe_sig(raw, "whsec_x", t)})
    assert r.json()["cancelled"] is True
    assert store.get_entitlement("c1")["active"] is False


def test_retention_endpoint_lists_lapsed():
    client, store = _client()
    now = time.time()
    store.record_gift("c1", "crown", amount=20.0, now=now - 40 * 86400)
    store.record_gift("c2", "rose", amount=5.0, now=now - 2 * 86400)
    d = client.get("/api/monetize/retention?recent_days=30").json()
    assert d["ok"] is True
    keys = [it["contact_key"] for it in d["items"]]
    assert "c1" in keys and "c2" not in keys
    assert d["items"][0]["ltv"] == 20.0


def test_webhook_telegram_unauthorized():
    client, _ = _client(mon_cfg={
        "enabled": True,
        "providers": {"telegram_stars": {"webhook_secret": "tok"}}})
    r = client.post("/api/monetize/webhook/telegram", json={"message": {}},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert r.json()["reason"] == "unauthorized"


def test_webhook_telegram_pre_checkout_and_payment():
    client, store = _client(mon_cfg={
        "enabled": True,
        "providers": {"telegram_stars": {"webhook_secret": "tok"}}})  # bot_token 空→不发网络
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "tok"}
    payload = encode_invoice_payload({"contact_key": "c1", "kind": "subscribe",
                                      "item_id": "vip", "days": 30})
    # pre_checkout
    pc = client.post("/api/monetize/webhook/telegram", headers=hdr, json={
        "pre_checkout_query": {"id": "pcq", "invoice_payload": payload}})
    assert pc.json()["pre_checkout"] is True
    # successful_payment
    sp = client.post("/api/monetize/webhook/telegram", headers=hdr, json={
        "message": {"successful_payment": {
            "invoice_payload": payload, "currency": "XTR", "total_amount": 120,
            "telegram_payment_charge_id": "ch_9"}}})
    assert sp.json()["applied"] is True
    assert store.get_entitlement("c1")["tier"] == "vip"
