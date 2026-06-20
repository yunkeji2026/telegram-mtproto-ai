"""Phase K2：C 端变现 Web API。

把 ``EntitlementStore`` 暴露给后台 + 支付回调：
- ``GET  /api/monetize/overview``    营收概览（总额/分类/活跃订阅数/Top 消费）
- ``GET  /api/monetize/catalog``     价目目录（前端渲染套餐/解锁/礼物）
- ``GET  /api/monetize/entitlement`` 某端用户当前权益（tier/grants/已解锁）
- ``POST /api/monetize/grant``       运营手动开通（订阅/解锁/打赏入账）
- ``POST /api/monetize/webhook``     支付服务商回调桩（共享密钥校验 + 幂等记账+发权益）

store 经 ``app.state.entitlement_store`` 注入，缺则按 config 目录懒建单例。读写过 ``api_auth``；
webhook 另用配置共享密钥校验（外部回调，非后台 session）。变现总开关默认关。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

_DAY = 86400.0


def register_monetization_routes(app, *, api_auth, config_manager=None) -> None:
    def _cfg() -> dict:
        cm = getattr(app.state, "config_manager", None) or config_manager
        return (getattr(cm, "config", None) or {}) if cm else {}

    def _mon_cfg() -> dict:
        return ((_cfg().get("monetization") or {}) if isinstance(_cfg(), dict) else {})

    def _catalog():
        from src.utils.monetization import merge_catalog
        return merge_catalog(_mon_cfg().get("catalog"))

    def _store(request: Request):
        st = getattr(request.app.state, "entitlement_store", None)
        if st is not None:
            return st
        from src.utils.entitlement_store import get_entitlement_store
        db_path = ":memory:"
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            base = Path(getattr(cm, "config_path", "") or "").parent
            if str(base):
                db_path = base / "entitlements.db"
        except Exception:
            db_path = ":memory:"
        st = get_entitlement_store(db_path, catalog=_catalog())
        request.app.state.entitlement_store = st
        return st

    # ── 营收概览 ─────────────────────────────────────────────────────────
    @app.get("/api/monetize/overview")
    async def api_monetize_overview(request: Request, days: float = 30, _=Depends(api_auth)):
        """营收概览：近 N 天总额 + 按 kind 分组 + 活跃订阅数 + Top 消费端用户 + 最近流水。"""
        store = _store(request)
        now = time.time()
        d = max(1.0, min(float(days or 30), 365.0))
        since = now - d * _DAY
        return {
            "ok": True,
            "enabled": bool(_mon_cfg().get("enabled", False)),
            "window_days": d,
            "revenue": store.revenue_summary(since=since, until=now),
            "active_subscriptions": store.active_subscription_count(now=now),
            "top_spenders": store.top_spenders(since=since, until=now, limit=10),
            "recent_tx": store.recent_tx(limit=20),
        }

    @app.get("/api/monetize/catalog")
    async def api_monetize_catalog(request: Request, _=Depends(api_auth)):
        """价目目录（合并 config 覆盖后的有效目录）。"""
        return {"ok": True, "catalog": _catalog()}

    @app.get("/api/monetize/entitlement")
    async def api_monetize_entitlement(request: Request, contact_key: str = "", _=Depends(api_auth)):
        """某端用户当前权益。"""
        ck = str(contact_key or "").strip()
        if not ck:
            return {"ok": False, "reason": "missing", "message": "contact_key 必填"}
        store = _store(request)
        ent = store.get_entitlement(ck)
        return {"ok": True, "entitlement": ent,
                "tx": store.recent_tx(limit=20)}

    @app.post("/api/monetize/feature-check")
    async def api_monetize_feature_check(request: Request, _=Depends(api_auth)):
        """付费功能门控集成缝：任意前端/功能（语音/剧情/主动等）发送前先查。body：
        {contact_key, feature}。返回 {allowed, upsell?, pitch_hint?}。

        gate 关时恒 allowed=True（零破坏）；开启且端用户无权限时附升级报价 + 引导文案。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        ck = str(body.get("contact_key") or "").strip()
        feature = str(body.get("feature") or "").strip()
        if not ck or not feature:
            return {"ok": False, "reason": "missing",
                    "message": "contact_key 和 feature 必填"}
        _store(request)  # 确保 store 已挂 app.state（feature_check 经 runtime 读取）
        from src.utils.monetization_runtime import MonetizationRuntime
        rt = MonetizationRuntime.from_app(request.app)
        if rt is None:
            return {"ok": True, "allowed": True, "gate_enabled": False, "upsell": None}
        res = rt.feature_check(ck, feature)
        return {"ok": True, **res}

    # ── 运营手动开通 ─────────────────────────────────────────────────────
    @app.post("/api/monetize/grant")
    async def api_monetize_grant(request: Request, _=Depends(api_auth)):
        """运营手动开通。body：
        {contact_key, kind:subscribe|unlock|gift, item_id, days?(订阅时长,默认30),
         amount?(覆盖价目), ref?}。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        ck = str(body.get("contact_key") or "").strip()
        kind = str(body.get("kind") or "").strip().lower()
        item_id = str(body.get("item_id") or "").strip()
        if not ck or kind not in ("subscribe", "unlock", "gift") or not item_id:
            return {"ok": False, "reason": "bad_request",
                    "message": "contact_key / kind(subscribe|unlock|gift) / item_id 必填"}
        store = _store(request)
        now = time.time()
        ref = str(body.get("ref") or "")
        amount = body.get("amount")
        amount = float(amount) if amount is not None else None
        if kind == "subscribe":
            days = float(body.get("days") or 30)
            ok = store.grant_subscription(
                ck, item_id, now + days * _DAY, source="manual",
                ref=ref, amount=amount, now=now)
            if not ok:
                return {"ok": False, "reason": "grant_failed",
                        "message": "开通失败（可能 ref 重复或 tier 非法）"}
            return {"ok": True, "kind": kind, "entitlement": store.get_entitlement(ck)}
        if kind == "unlock":
            newly = store.record_unlock(ck, item_id, source="manual", ref=ref,
                                        amount=amount, now=now)
            return {"ok": True, "kind": kind, "newly_unlocked": bool(newly),
                    "entitlement": store.get_entitlement(ck)}
        tx_id = store.record_gift(ck, item_id, amount=amount, source="manual",
                                  ref=ref, now=now)
        return {"ok": True, "kind": kind, "tx_id": tx_id}

    # ── 支付回调桩（外部服务商）──────────────────────────────────────────
    @app.post("/api/monetize/webhook")
    async def api_monetize_webhook(request: Request):
        """支付服务商回调桩（provider-agnostic）。body：
        {contact_key, kind, item_id, amount?, currency?, ref, days?}。

        安全：配置 ``monetization.webhook_secret`` 时校验 ``X-Monetize-Secret`` 头；
        未配置则接受（开发/自测）。幂等：``ref`` 重复 → 跳过不重复记账/发权益。
        """
        secret = str(_mon_cfg().get("webhook_secret") or "")
        if secret:
            got = request.headers.get("x-monetize-secret") or ""
            if got != secret:
                return {"ok": False, "reason": "unauthorized"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ck = str(body.get("contact_key") or "").strip()
        kind = str(body.get("kind") or "").strip().lower()
        item_id = str(body.get("item_id") or "").strip()
        ref = str(body.get("ref") or "").strip()
        if not ck or kind not in ("subscribe", "unlock", "gift") or not item_id:
            return {"ok": False, "reason": "bad_request"}
        store = _store(request)
        now = time.time()
        amount = body.get("amount")
        amount = float(amount) if amount is not None else None
        currency = str(body.get("currency") or "")
        if kind == "subscribe":
            days = float(body.get("days") or 30)
            ok = store.grant_subscription(
                ck, item_id, now + days * _DAY, source="webhook",
                ref=ref, amount=amount, now=now)
            return {"ok": True, "applied": bool(ok), "kind": kind, "ref": ref}
        if kind == "unlock":
            # webhook 即使 unlock 已持有，也确保 ref 入账幂等（record_unlock 内已幂等）
            newly = store.record_unlock(ck, item_id, source="webhook", ref=ref,
                                        amount=amount, now=now)
            return {"ok": True, "applied": bool(newly), "kind": kind, "ref": ref}
        tx_id = store.record_gift(ck, item_id, amount=amount, source="webhook",
                                  ref=ref, now=now)
        if currency and tx_id is None and ref:
            pass  # 幂等重投
        return {"ok": True, "applied": tx_id is not None, "kind": kind, "ref": ref}


__all__ = ["register_monetization_routes"]
