"""Resolve effective domain and payment-plugin gate from config."""

from __future__ import annotations

from typing import Any, Dict


def payment_plugin_enabled(cfg: Dict[str, Any] | None) -> bool:
    """
    When ``domain_plugins.payment.enabled`` is set, it wins.
    Otherwise: payment plugin is considered enabled only if ``domain`` is ``payment``
    (backward compatible for existing installs).
    """
    if not isinstance(cfg, dict):
        return False
    dp = cfg.get("domain_plugins") or {}
    pay = dp.get("payment") or {}
    if "enabled" in pay:
        return bool(pay["enabled"])
    return (cfg.get("domain") or "").strip().lower() == "payment"


def effective_domain_name(cfg: Dict[str, Any] | None) -> str:
    """Active domain for loaders and Web UI; remaps ``payment`` → ``conversion`` when payment plugin is off."""
    if not isinstance(cfg, dict):
        return "conversion"
    raw = (cfg.get("domain") or "conversion").strip() or "conversion"
    if raw == "payment" and not payment_plugin_enabled(cfg):
        return "conversion"
    return raw
