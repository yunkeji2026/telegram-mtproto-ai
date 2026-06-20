"""Phase K2：/api/monetize/* 路由契约 + 支付回调桩。"""
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils.entitlement_store import EntitlementStore
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
