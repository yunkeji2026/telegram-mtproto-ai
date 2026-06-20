"""Phase K2b：变现运行时门控（把 config + EntitlementStore + 纯函数缝在一起）。

提供给「想做付费门控的功能」一个统一、best-effort、绝不抛的入口：
- ``feature_check(contact_key, feature)`` → {allowed, entitlement, upsell, gate_enabled}
- ``proactive_allowed(contact_key, sent_count)`` → 主动关怀配额门控（免费超额则 False）

设计：gate 默认关（``monetization.enabled`` 或 ``monetization.gate.enabled`` 任一关 → 恒放行），
对陪伴行为零影响。catalog/配额/store 都从 config + app.state 取，缺则安全降级（放行/free）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.utils.monetization import (
    feature_allowed,
    merge_catalog,
    proactive_quota_allowed,
    upsell_offer,
    upsell_pitch_hint,
)

logger = logging.getLogger(__name__)


class MonetizationRuntime:
    def __init__(self, *, store: Any, mon_cfg: Optional[Dict[str, Any]] = None):
        self._store = store
        self._cfg = mon_cfg or {}

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_app(cls, app) -> Optional["MonetizationRuntime"]:
        """从 FastAPI app.state 组装；store 缺失 → None（调用方据此降级为放行）。"""
        try:
            store = getattr(getattr(app, "state", None), "entitlement_store", None)
            cm = getattr(getattr(app, "state", None), "config_manager", None)
            cfg = (getattr(cm, "config", None) or {}) if cm else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            if store is None:
                return None
            return cls(store=store, mon_cfg=mon)
        except Exception:
            return None

    # ── 配置读取 ──────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False))

    def gate_enabled(self) -> bool:
        """门控总闸：变现启用 **且** gate.enabled。任一关 → 恒放行。"""
        if not self.enabled():
            return False
        return bool((self._cfg.get("gate") or {}).get("enabled", False))

    def catalog(self) -> Dict[str, Any]:
        return merge_catalog(self._cfg.get("catalog"))

    def free_proactive_quota(self) -> int:
        return int((self._cfg.get("upsell") or {}).get("free_proactive_daily", 1))

    # ── 权益 / 门控 ────────────────────────────────────────────────────────
    def entitlement_for(self, contact_key: str) -> Dict[str, Any]:
        try:
            return self._store.get_entitlement(str(contact_key))
        except Exception:
            return {"contact_key": str(contact_key), "tier": "free",
                    "active": False, "grants": [], "unlocked": []}

    def feature_check(self, contact_key: str, feature: str) -> Dict[str, Any]:
        """某端用户能否用某付费功能 + 不能时的升级报价。绝不抛。"""
        ge = self.gate_enabled()
        ent = self.entitlement_for(contact_key)
        allowed = feature_allowed(ent, feature, gate_enabled=ge)
        offer = None
        if not allowed:
            offer = upsell_offer(ent, feature, catalog=self.catalog(), gate_enabled=ge)
        out = {"allowed": allowed, "entitlement": ent, "gate_enabled": ge,
               "feature": str(feature), "upsell": offer}
        if offer:
            out["pitch_hint"] = upsell_pitch_hint(offer)
        return out

    def proactive_allowed(self, contact_key: str, sent_count: int) -> bool:
        """主动关怀配额门控：免费用户窗口内超额 → False（gate 关恒 True）。"""
        try:
            ent = self.entitlement_for(contact_key)
            return proactive_quota_allowed(
                ent, int(sent_count or 0),
                free_quota=self.free_proactive_quota(),
                gate_enabled=self.gate_enabled())
        except Exception:
            return True  # 门控异常绝不拦关怀


__all__ = ["MonetizationRuntime"]
