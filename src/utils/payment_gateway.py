"""Phase K2④：支付网关接入（Stripe / Telegram Stars）的**纯函数**核心。

把「可测、无网络」的部分独立出来——验签、事件解析、参数构建——便于单测与复用；
真正的网络调用（创建 Checkout / Invoice、应答 pre_checkout）由路由层 best-effort 发起。

落地的事实源仍是 ``EntitlementStore.tx_ledger``：所有 provider 回调最终归一成
``{contact_key, kind, item_id, amount, currency, ref, days}`` 这个 **grant 字典**，
再经 ``ref`` 幂等记账 + 发权益（支付回调天然 at-least-once，必须幂等）。

设计原则：
- 纯函数绝不抛、缺字段返回 None（坏回调不应 500）。
- 验签用 ``hmac.compare_digest`` 防时序侧信道；Stripe 带时间容差防重放。
- provider 默认关（config ``monetization.providers.*.enabled``）；未配密钥的验签恒 False。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional


# ── Stripe ──────────────────────────────────────────────────────────────
def stripe_verify_signature(
    payload: Any,
    sig_header: str,
    secret: str,
    *,
    tolerance: int = 300,
    now: Optional[float] = None,
) -> bool:
    """校验 Stripe ``Stripe-Signature`` 头（scheme: ``t=<ts>,v1=<hmac-sha256>``）。

    签名负载 = ``f"{t}.{raw_body}"``，HMAC-SHA256（key=whsec_…）后与 v1 比对。
    ``tolerance>0`` 时校验时间戳容差防重放。任一环节失败/缺密钥 → False。
    """
    if not secret or not sig_header:
        return False
    raw = payload.encode() if isinstance(payload, str) else bytes(payload or b"")
    items = [p.split("=", 1) for p in str(sig_header).split(",") if "=" in p]
    t = next((v for k, v in items if k.strip() == "t"), None)
    v1s = [v.strip() for k, v in items if k.strip() == "v1"]
    if not t or not v1s:
        return False
    signed = f"{t}.".encode() + raw
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in v1s):
        return False
    if tolerance and tolerance > 0:
        n = now if now is not None else time.time()
        try:
            if abs(float(n) - float(int(t))) > float(tolerance):
                return False
        except Exception:
            return False
    return True


def parse_stripe_event(event: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把已验签的 Stripe 事件解析成 grant 字典；不支持的事件 → None。

    - ``checkout.session.completed``（一次性 ``mode=payment``）：从 session ``metadata`` 取，
      金额从 ``amount_total`` 还原，ref=事件 id。
    - ``checkout.session.completed`` 且 ``mode=subscription`` → **None**（让位给 ``invoice.paid``，
      避免首付被 session 与 invoice 双发）。
    - ``invoice.paid``（订阅首付 + 每期续费）：metadata 依次从 ``subscription_details.metadata`` /
      ``lines.data[].metadata`` / 顶层 ``metadata`` 取，金额从 ``amount_paid`` 还原，ref=发票 id
      （每期一张 → 续费天然按期发权益且幂等）。
    """
    if not isinstance(event, dict):
        return None
    etype = str(event.get("type") or "")
    if etype not in ("checkout.session.completed", "invoice.paid"):
        return None
    obj = ((event.get("data") or {}).get("object") or {})
    if etype == "checkout.session.completed":
        if str(obj.get("mode") or "") == "subscription":
            return None  # 订阅首付由 invoice.paid 统一发，防双发
        md = obj.get("metadata") or {}
        cents = obj.get("amount_total")
        ref = str(event.get("id") or obj.get("id") or "").strip()
    else:  # invoice.paid（首付 + 续费）
        md = (obj.get("subscription_details") or {}).get("metadata") or {}
        if not md:
            lines = ((obj.get("lines") or {}).get("data") or [])
            if lines and isinstance(lines[0], dict):
                md = lines[0].get("metadata") or {}
        if not md:
            md = obj.get("metadata") or {}
        cents = obj.get("amount_paid")
        if cents is None:
            cents = obj.get("amount_total")
        ref = str(obj.get("id") or event.get("id") or "").strip()
    ck = str(md.get("contact_key") or "").strip()
    kind = str(md.get("kind") or "").strip().lower()
    item_id = str(md.get("item_id") or "").strip()
    if not ck or kind not in ("subscribe", "unlock", "gift") or not item_id:
        return None
    amount = round(float(cents) / 100.0, 2) if cents is not None else None
    out: Dict[str, Any] = {
        "contact_key": ck, "kind": kind, "item_id": item_id,
        "amount": amount, "currency": str(obj.get("currency") or "").upper(),
        "ref": ref, "provider": "stripe",
    }
    if kind == "subscribe":
        try:
            out["days"] = float(md.get("days") or 30)
        except Exception:
            out["days"] = 30.0
    return out


def build_stripe_checkout_params(
    *,
    contact_key: str,
    kind: str,
    item_id: str,
    amount: float,
    currency: str,
    label: str = "",
    days: float = 30,
    success_url: str = "",
    cancel_url: str = "",
    recurring: bool = False,
    interval: str = "month",
) -> Dict[str, str]:
    """构建 Stripe Checkout Session create 的 form 参数（application/x-www-form-urlencoded）。

    - 一次性付费 / 默认：``mode=payment``。
    - ``recurring=True``（仅订阅）：``mode=subscription`` + price_data 带 ``recurring[interval]``；
      grant 信息**同时**写到 session metadata 与 ``subscription_data[metadata]``——后者让**续费发票**
      （``invoice.paid``）也能溯源到端用户（首付从 invoice.paid 发，session.completed 让位防双发）。
    金额转分（unit_amount）。返回扁平 form-key 字典（便于直接喂 aiohttp data= 与单测断言）。
    """
    cents = int(round(float(amount or 0) * 100))
    cur = str(currency or "usd").lower()
    name = label or item_id
    is_sub = bool(recurring) and str(kind) == "subscribe"
    params: Dict[str, str] = {
        "mode": "subscription" if is_sub else "payment",
        "line_items[0][price_data][currency]": cur,
        "line_items[0][price_data][product_data][name]": str(name),
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][quantity]": "1",
        "metadata[contact_key]": str(contact_key),
        "metadata[kind]": str(kind),
        "metadata[item_id]": str(item_id),
    }
    if str(kind) == "subscribe":
        params["metadata[days]"] = str(int(days or 30))
    if is_sub:
        params["line_items[0][price_data][recurring][interval]"] = str(interval or "month")
        # 续费发票溯源：把 grant 关键字段挂到订阅 metadata
        params["subscription_data[metadata][contact_key]"] = str(contact_key)
        params["subscription_data[metadata][kind]"] = "subscribe"
        params["subscription_data[metadata][item_id]"] = str(item_id)
        params["subscription_data[metadata][days]"] = str(int(days or 30))
    if success_url:
        params["success_url"] = str(success_url)
    if cancel_url:
        params["cancel_url"] = str(cancel_url)
    return params


# ── Telegram Stars ────────────────────────────────────────────────────────
def telegram_verify_secret(header_token: str, secret: str) -> bool:
    """校验 Telegram webhook 的 ``X-Telegram-Bot-Api-Secret-Token`` 头。

    未配 secret（空）→ False（强制要求配置，避免任意人伪造 update）。
    """
    if not secret:
        return False
    return hmac.compare_digest(str(header_token or ""), str(secret))


def encode_invoice_payload(grant: Dict[str, Any]) -> str:
    """把 grant 关键字段编码进 Telegram invoice 的 ``payload``（成功支付时原样回传）。"""
    return json.dumps({
        "contact_key": str(grant.get("contact_key") or ""),
        "kind": str(grant.get("kind") or ""),
        "item_id": str(grant.get("item_id") or ""),
        "days": int(grant.get("days") or 30),
    }, ensure_ascii=False, separators=(",", ":"))


def _decode_invoice_payload(raw: str) -> Dict[str, Any]:
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def extract_telegram_pre_checkout(update: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从 update 取 pre_checkout_query（必须 10s 内 answerPreCheckoutQuery）。

    返回 ``{id, payload(grant 解码)}`` 或 None。
    """
    if not isinstance(update, dict):
        return None
    pcq = update.get("pre_checkout_query")
    if not isinstance(pcq, dict) or not pcq.get("id"):
        return None
    return {"id": str(pcq.get("id")),
            "payload": _decode_invoice_payload(str(pcq.get("invoice_payload") or ""))}


def parse_telegram_successful_payment(
    update: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """把 Telegram ``message.successful_payment`` 解析成 grant 字典；否则 None。

    ``invoice_payload`` 携带我们编码的 grant；``total_amount`` 对 XTR 为整数星数；
    ``telegram_payment_charge_id`` 作幂等 ref。
    """
    if not isinstance(update, dict):
        return None
    msg = update.get("message") or {}
    sp = msg.get("successful_payment")
    if not isinstance(sp, dict):
        return None
    payload = _decode_invoice_payload(str(sp.get("invoice_payload") or ""))
    ck = str(payload.get("contact_key") or "").strip()
    kind = str(payload.get("kind") or "").strip().lower()
    item_id = str(payload.get("item_id") or "").strip()
    if not ck or kind not in ("subscribe", "unlock", "gift") or not item_id:
        return None
    cur = str(sp.get("currency") or "XTR").upper()
    total = sp.get("total_amount")
    # XTR（星）无小数；法币 total_amount 是最小货币单位（分）→ /100
    if cur == "XTR":
        amount = float(total) if total is not None else None
    else:
        amount = round(float(total) / 100.0, 2) if total is not None else None
    ref = str(sp.get("telegram_payment_charge_id")
              or sp.get("provider_payment_charge_id") or "").strip()
    out: Dict[str, Any] = {
        "contact_key": ck, "kind": kind, "item_id": item_id,
        "amount": amount, "currency": cur, "ref": ref, "provider": "telegram",
    }
    if kind == "subscribe":
        try:
            out["days"] = float(payload.get("days") or 30)
        except Exception:
            out["days"] = 30.0
    return out


def build_telegram_invoice_params(
    *,
    contact_key: str,
    kind: str,
    item_id: str,
    amount_stars: int,
    label: str = "",
    description: str = "",
    days: float = 30,
) -> Dict[str, Any]:
    """构建 createInvoiceLink 的 JSON body（Telegram Stars: currency=XTR）。

    Stars 计价为整数星；``prices`` 为 LabeledPrice 列表（XTR 的 amount 即星数）。
    payload 编码 grant 供成功回调还原。
    """
    title = (label or item_id)[:32] or "Purchase"
    desc = (description or label or item_id)[:255] or title
    stars = max(1, int(amount_stars or 1))
    return {
        "title": title,
        "description": desc,
        "payload": encode_invoice_payload({
            "contact_key": contact_key, "kind": kind,
            "item_id": item_id, "days": days,
        }),
        "currency": "XTR",
        "prices": [{"label": title, "amount": stars}],
    }


__all__ = [
    "stripe_verify_signature",
    "parse_stripe_event",
    "build_stripe_checkout_params",
    "telegram_verify_secret",
    "encode_invoice_payload",
    "extract_telegram_pre_checkout",
    "parse_telegram_successful_payment",
    "build_telegram_invoice_params",
]
