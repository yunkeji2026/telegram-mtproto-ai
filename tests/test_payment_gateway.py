"""Phase K2④：支付网关纯函数单测（Stripe 验签/事件解析 + Telegram 支付解析/参数构建）。"""
import hashlib
import hmac
import time

from src.utils.payment_gateway import (
    build_stripe_checkout_params,
    build_telegram_invoice_params,
    encode_invoice_payload,
    extract_telegram_pre_checkout,
    parse_stripe_event,
    parse_telegram_successful_payment,
    stripe_verify_signature,
    telegram_verify_secret,
)


def _stripe_sig(payload: bytes, secret: str, t: int) -> str:
    signed = f"{t}.".encode() + payload
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


# ── Stripe 验签 ──────────────────────────────────────────────────────────
def test_stripe_verify_signature_valid():
    secret = "whsec_test"
    payload = b'{"id":"evt_1"}'
    now = 1_700_000_000
    sig = _stripe_sig(payload, secret, now)
    assert stripe_verify_signature(payload, sig, secret, now=now) is True
    # str payload 也支持
    assert stripe_verify_signature(payload.decode(), sig, secret, now=now) is True


def test_stripe_verify_signature_tamper_and_missing():
    secret = "whsec_test"
    payload = b'{"id":"evt_1"}'
    now = 1_700_000_000
    sig = _stripe_sig(payload, secret, now)
    # 篡改 body
    assert stripe_verify_signature(b'{"id":"evt_2"}', sig, secret, now=now) is False
    # 错密钥
    assert stripe_verify_signature(payload, sig, "whsec_other", now=now) is False
    # 缺密钥 / 缺头
    assert stripe_verify_signature(payload, sig, "", now=now) is False
    assert stripe_verify_signature(payload, "", secret, now=now) is False


def test_stripe_verify_signature_replay_tolerance():
    secret = "whsec_test"
    payload = b'{"id":"evt_1"}'
    t = 1_700_000_000
    sig = _stripe_sig(payload, secret, t)
    # 超过容差（5 分钟）
    assert stripe_verify_signature(payload, sig, secret, now=t + 1000) is False
    # 容差内
    assert stripe_verify_signature(payload, sig, secret, now=t + 100) is True
    # 关容差
    assert stripe_verify_signature(payload, sig, secret, tolerance=0, now=t + 9999) is True


def test_parse_stripe_event_checkout_completed():
    event = {
        "id": "evt_123", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_1", "amount_total": 990, "currency": "usd",
            "metadata": {"contact_key": "c1", "kind": "subscribe",
                         "item_id": "vip", "days": "30"},
        }},
    }
    g = parse_stripe_event(event)
    assert g["contact_key"] == "c1"
    assert g["kind"] == "subscribe"
    assert g["item_id"] == "vip"
    assert g["amount"] == 9.9
    assert g["currency"] == "USD"
    assert g["ref"] == "evt_123"
    assert g["days"] == 30.0


def test_parse_stripe_event_ignores_other_and_bad():
    assert parse_stripe_event({"type": "payment_intent.created"}) is None
    assert parse_stripe_event(None) is None
    # 缺 metadata 必填
    assert parse_stripe_event({
        "id": "e", "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"kind": "subscribe"}}}}) is None


def test_build_stripe_checkout_params():
    p = build_stripe_checkout_params(
        contact_key="c1", kind="subscribe", item_id="vip", amount=9.9,
        currency="USD", label="VIP", days=30,
        success_url="https://ok", cancel_url="https://no")
    assert p["mode"] == "payment"
    assert p["line_items[0][price_data][unit_amount]"] == "990"
    assert p["line_items[0][price_data][currency]"] == "usd"
    assert p["metadata[contact_key]"] == "c1"
    assert p["metadata[days]"] == "30"
    assert p["success_url"] == "https://ok"


# ── Telegram ──────────────────────────────────────────────────────────────
def test_telegram_verify_secret():
    assert telegram_verify_secret("tok123", "tok123") is True
    assert telegram_verify_secret("nope", "tok123") is False
    assert telegram_verify_secret("tok123", "") is False  # 未配密钥拒绝


def test_telegram_invoice_payload_roundtrip():
    raw = encode_invoice_payload({"contact_key": "c1", "kind": "unlock",
                                  "item_id": "story_ch1", "days": 30})
    update = {"message": {"successful_payment": {
        "invoice_payload": raw, "currency": "XTR", "total_amount": 50,
        "telegram_payment_charge_id": "ch_1"}}}
    g = parse_telegram_successful_payment(update)
    assert g["contact_key"] == "c1"
    assert g["kind"] == "unlock"
    assert g["item_id"] == "story_ch1"
    assert g["currency"] == "XTR"
    assert g["amount"] == 50  # XTR 无小数
    assert g["ref"] == "ch_1"
    assert g["provider"] == "telegram"


def test_telegram_successful_payment_fiat_divides_cents():
    raw = encode_invoice_payload({"contact_key": "c1", "kind": "gift",
                                  "item_id": "rose"})
    update = {"message": {"successful_payment": {
        "invoice_payload": raw, "currency": "USD", "total_amount": 99,
        "telegram_payment_charge_id": "ch_2"}}}
    g = parse_telegram_successful_payment(update)
    assert g["amount"] == 0.99


def test_telegram_pre_checkout_extract():
    raw = encode_invoice_payload({"contact_key": "c1", "kind": "subscribe",
                                  "item_id": "vip"})
    update = {"pre_checkout_query": {"id": "pcq_1", "invoice_payload": raw}}
    pcq = extract_telegram_pre_checkout(update)
    assert pcq["id"] == "pcq_1"
    assert pcq["payload"]["item_id"] == "vip"
    # 非 pre_checkout 更新 → None
    assert extract_telegram_pre_checkout({"message": {}}) is None


def test_telegram_parse_ignores_non_payment():
    assert parse_telegram_successful_payment({"message": {"text": "hi"}}) is None
    assert parse_telegram_successful_payment(None) is None


def test_build_telegram_invoice_params():
    inv = build_telegram_invoice_params(
        contact_key="c1", kind="subscribe", item_id="vip", amount_stars=120,
        label="VIP", days=30)
    assert inv["currency"] == "XTR"
    assert inv["prices"][0]["amount"] == 120
    assert inv["title"] == "VIP"
    # payload 可被还原
    import json
    pl = json.loads(inv["payload"])
    assert pl["contact_key"] == "c1"
    assert pl["item_id"] == "vip"
