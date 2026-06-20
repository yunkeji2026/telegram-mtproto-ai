"""Phase K2：C 端变现纯函数（价目目录 + 权益判定 + 营收聚合）。

付费主体 = 与 AI 陪伴对话的**端用户**（contact），以 ``contact_key``（= 收件箱
``conversation_id``，与 care_schedule O4 对齐）为唯一标识。本模块只放**无副作用纯函数**，
便于单测与复用（API / CLI / gate / 后台都能调），不依赖 FastAPI、不碰 DB。

变现三件套
==========
- **订阅（subscription）**：端用户购买会员 tier（free/vip/svip…），有有效期 ``active_until``；
  tier 授予一组功能位 ``grants``（如 ``voice_reply`` / ``unlimited_proactive``）。
- **付费解锁（unlock）**：一次性买断某内容项（``items``，如剧情章节/专属相册）→ 永久持有。
- **打赏/虚拟礼物（gift）**：``gifts`` 价目，纯入账（不授予功能位），用于营收 + 后续好感度。

与 B2B ``billing.py`` 正交：billing 是「运营方→平台」的席位/消息计费；本模块是
「端用户→运营方」的内容/会员变现。两套价目互不污染。

gate 默认放行
=============
``feature_allowed`` 在 ``gate_enabled=False`` 时**恒放行**（与 ``licensing.gate`` 同范式）——
变现门控默认关，零破坏既有陪伴行为；开启后才按权益判定。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Set

# 默认价目目录（USD）。生产可在 ``config.monetization.catalog`` 覆盖（深合并）。
DEFAULT_CATALOG: Dict[str, Any] = {
    "currency": "USD",
    "tiers": {
        "free": {"monthly": 0.0, "label": "免费", "grants": []},
        "vip": {
            "monthly": 9.9, "label": "VIP",
            "grants": ["unlimited_proactive", "voice_reply", "priority_reply"],
        },
        "svip": {
            "monthly": 29.9, "label": "SVIP",
            "grants": ["unlimited_proactive", "voice_reply", "priority_reply",
                       "exclusive_persona", "all_story"],
        },
    },
    "items": {  # 一次性解锁项（剧情/专属内容）
        "story_ch1": {"price": 1.99, "label": "剧情·第一章"},
        "exclusive_album": {"price": 4.99, "label": "专属相册"},
    },
    "gifts": {  # 虚拟礼物 / 打赏
        "rose": {"amount": 0.99, "label": "🌹 玫瑰"},
        "coffee": {"amount": 2.99, "label": "☕ 咖啡"},
        "crown": {"amount": 9.99, "label": "👑 皇冠"},
    },
}

TX_KINDS = ("subscribe", "unlock", "gift")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def merge_catalog(override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 config 覆盖深合并到默认目录上（tiers/items/gifts 逐键合并，标量直接覆盖）。"""
    cat = copy.deepcopy(DEFAULT_CATALOG)
    if not isinstance(override, dict):
        return cat
    for key in ("currency",):
        if override.get(key):
            cat[key] = override[key]
    for group in ("tiers", "items", "gifts"):
        ov = override.get(group)
        if isinstance(ov, dict):
            base = cat.setdefault(group, {})
            for k, v in ov.items():
                if isinstance(v, dict):
                    base[str(k)] = {**(base.get(str(k)) or {}), **v}
                else:
                    base[str(k)] = v
    return cat


def catalog_currency(catalog: Optional[Dict[str, Any]] = None) -> str:
    cat = catalog or DEFAULT_CATALOG
    return str(cat.get("currency") or DEFAULT_CATALOG["currency"])


def tier_grants(tier: str, catalog: Optional[Dict[str, Any]] = None) -> Set[str]:
    """某会员 tier 授予的功能位集合；未知 tier → 空集。"""
    cat = catalog or DEFAULT_CATALOG
    tiers = cat.get("tiers") or {}
    cfg = tiers.get(str(tier or "")) or {}
    grants = cfg.get("grants") or []
    return {str(g) for g in grants if g}


def known_tiers(catalog: Optional[Dict[str, Any]] = None) -> List[str]:
    cat = catalog or DEFAULT_CATALOG
    return list((cat.get("tiers") or {}).keys())


def tier_rank(tier: str, catalog: Optional[Dict[str, Any]] = None) -> float:
    """tier 排序权重（按月费高低；free/未知=0）。用于多会话取「最高档」。"""
    cat = catalog or DEFAULT_CATALOG
    cfg = (cat.get("tiers") or {}).get(str(tier or "")) or {}
    return _f(cfg.get("monthly"))


def best_tier(tiers: Iterable[str], catalog: Optional[Dict[str, Any]] = None) -> str:
    """从若干 tier 里挑「最高档」（月费最高）；空/全 free → 'free'。"""
    best, best_r = "free", -1.0
    for t in tiers or []:
        r = tier_rank(t, catalog)
        if r > best_r:
            best_r, best = r, str(t or "free")
    return best


def subscription_active(active_until: Any, now: float) -> bool:
    """订阅是否在有效期内。``active_until`` 为 epoch 秒；<=0 或已过 now → 失效。"""
    au = _f(active_until)
    return au > 0 and au > float(now)


def effective_tier(tier: str, active_until: Any, now: float) -> str:
    """有效会员档：订阅有效则返回 tier，否则降级 ``free``。"""
    t = str(tier or "free")
    if t == "free":
        return "free"
    return t if subscription_active(active_until, now) else "free"


def entitlement_allows(entitlement: Dict[str, Any], feature: str) -> bool:
    """权益是否覆盖某功能：被有效 tier 的 grants 覆盖 **或** 已解锁同名内容项。

    ``entitlement`` 形如 ``{"grants": set/list, "unlocked": set/list, ...}``（store 产出）。
    """
    if not feature:
        return True
    feat = str(feature)
    grants = entitlement.get("grants") or ()
    if feat in set(str(g) for g in grants):
        return True
    unlocked = entitlement.get("unlocked") or ()
    return feat in set(str(u) for u in unlocked)


def feature_allowed(
    entitlement: Optional[Dict[str, Any]],
    feature: str,
    *,
    gate_enabled: bool = False,
) -> bool:
    """变现 gate 原语：``gate_enabled=False`` → 恒放行（零破坏）；开启后按权益判定。

    与 ``licensing.gate`` 同范式——强制层（send/voice 等）调用本原语，是否生效由
    ``gate_enabled`` 决定。``entitlement`` 缺省视为 free（无任何 grant）。
    """
    if not gate_enabled:
        return True
    if not feature:
        return True
    return entitlement_allows(entitlement or {"grants": (), "unlocked": ()}, feature)


def quote(kind: str, item_id: str, catalog: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """报价：返回 ``{amount, currency, label, kind, item_id}`` 或 None（未知项）。

    - ``subscribe``：item_id = tier 名，amount = 月费。
    - ``unlock``：item_id = items 项，amount = price。
    - ``gift``：item_id = gifts 项，amount = amount。
    """
    cat = catalog or DEFAULT_CATALOG
    cur = catalog_currency(cat)
    k = str(kind or "")
    iid = str(item_id or "")
    if k == "subscribe":
        cfg = (cat.get("tiers") or {}).get(iid)
        if not cfg:
            return None
        amount = _f(cfg.get("monthly"))
    elif k == "unlock":
        cfg = (cat.get("items") or {}).get(iid)
        if not cfg:
            return None
        amount = _f(cfg.get("price"))
    elif k == "gift":
        cfg = (cat.get("gifts") or {}).get(iid)
        if not cfg:
            return None
        amount = _f(cfg.get("amount"))
    else:
        return None
    return {
        "kind": k, "item_id": iid, "amount": round(amount, 2),
        "currency": cur, "label": str(cfg.get("label") or iid),
    }


def upsell_offer(
    entitlement: Optional[Dict[str, Any]],
    feature: str,
    *,
    catalog: Optional[Dict[str, Any]] = None,
    gate_enabled: bool = True,
) -> Optional[Dict[str, Any]]:
    """为「缺某功能的端用户」算一个最划算的升级报价（纯函数）。

    - gate 关 / 无 feature / 已拥有该功能 → None（不打扰）。
    - 否则找**最便宜**的、其 grants 含该 feature 的 tier；找不到再看同名解锁项。
    返回 ``{kind, tier, item_id, amount, currency, label, feature}`` 或 None。
    """
    if not gate_enabled or not feature:
        return None
    ent = entitlement or {"grants": (), "unlocked": ()}
    if entitlement_allows(ent, feature):
        return None
    cat = catalog or DEFAULT_CATALOG
    cur = catalog_currency(cat)
    best = None  # (tier_name, price, cfg)
    for tname, tcfg in (cat.get("tiers") or {}).items():
        grants = {str(g) for g in (tcfg.get("grants") or [])}
        if str(feature) in grants:
            price = _f(tcfg.get("monthly"))
            if best is None or price < best[1]:
                best = (str(tname), price, tcfg)
    if best is not None:
        tname, price, tcfg = best
        return {
            "kind": "subscribe", "tier": tname, "item_id": tname,
            "amount": round(price, 2), "currency": cur,
            "label": str(tcfg.get("label") or tname), "feature": str(feature),
            "grants": sorted(str(g) for g in (tcfg.get("grants") or [])),
        }
    items = cat.get("items") or {}
    if str(feature) in items:
        icfg = items[str(feature)]
        return {
            "kind": "unlock", "tier": "", "item_id": str(feature),
            "amount": round(_f(icfg.get("price")), 2), "currency": cur,
            "label": str(icfg.get("label") or feature), "feature": str(feature),
        }
    return None


def upsell_pitch_hint(offer: Optional[Dict[str, Any]], *, persona_name: str = "") -> str:
    """把升级报价转成**得体、贴合人设**的软引导文案（供 UI/坐席展示，非硬推销）。

    刻意短小、不夸张、不承诺——陪伴产品的转化应像「她也想更近一点」而非弹窗广告。
    """
    if not offer:
        return ""
    label = str(offer.get("label") or offer.get("tier") or "会员")
    cur = str(offer.get("currency") or "USD")
    amt = offer.get("amount") or 0
    who = persona_name or "我"
    if offer.get("kind") == "subscribe":
        return f"升级「{label}」（{cur} {amt}/月），{who}就能更常陪着你～💕"
    return f"解锁「{label}」（{cur} {amt}）就能看到啦～"


def proactive_quota_allowed(
    entitlement: Optional[Dict[str, Any]],
    sent_count: int,
    *,
    free_quota: int,
    gate_enabled: bool = False,
) -> bool:
    """主动关怀配额门控（纯函数）：免费用户超额则不再主动；会员（unlimited_proactive）不限。

    - gate 关 → 恒放行（零破坏）。
    - 拥有 ``unlimited_proactive`` → 恒放行。
    - 否则：窗口内已发主动数 < 免费配额 才放行。``free_quota<=0`` 视为「免费不允许主动」。
    """
    if not gate_enabled:
        return True
    if entitlement_allows(entitlement or {}, "unlimited_proactive"):
        return True
    return int(sent_count or 0) < int(free_quota or 0)


def revenue_from_txs(txs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """把交易流水聚合成营收概览（纯函数）：总额 + 按 kind 分组 + 计数。

    只计 ``status`` 为 paid/空（视为已支付）的流水；refunded/failed 不计入营收。
    """
    total = 0.0
    by_kind: Dict[str, Dict[str, Any]] = {}
    count = 0
    currency = DEFAULT_CATALOG["currency"]
    for tx in txs or []:
        status = str(tx.get("status") or "paid").lower()
        if status not in ("paid", ""):
            continue
        amt = _f(tx.get("amount"))
        kind = str(tx.get("kind") or "other")
        if tx.get("currency"):
            currency = str(tx["currency"])
        total += amt
        count += 1
        slot = by_kind.setdefault(kind, {"amount": 0.0, "count": 0})
        slot["amount"] = round(slot["amount"] + amt, 2)
        slot["count"] += 1
    return {
        "total": round(total, 2),
        "currency": currency,
        "count": count,
        "by_kind": by_kind,
    }


__all__ = [
    "DEFAULT_CATALOG", "TX_KINDS", "merge_catalog", "catalog_currency",
    "tier_grants", "known_tiers", "tier_rank", "best_tier",
    "subscription_active", "effective_tier",
    "entitlement_allows", "feature_allowed", "quote", "revenue_from_txs",
    "upsell_offer", "upsell_pitch_hint", "proactive_quota_allowed",
]
