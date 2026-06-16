"""C1-1 白标/贴牌：品牌设置解析 + 套餐 gating（纯逻辑，便于单测）。

商业模型
========
- **改名/换色/换 logo**：所有付费档位均可（轻度定制，提升专业感）。
- **去除厂商署名（Powered by）**：仅旗舰版（授权功能位 ``white_label``）。
  非旗舰即使把 ``hide_powered_by`` 设为 true 也会被忽略——署名强制保留。

``enforce`` 关时（社区/开发/现网默认）``feature_allowed`` 恒放行，故品牌设置完全生效、
署名可隐藏——零破坏且开发友好。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_SITE_NAME = "华灵科技客户转化聊天系统"
DEFAULT_SITE_NAME_SHORT = "华灵科技"
POWERED_BY_TEXT = "Powered by 华灵科技"

_FIELDS = ("site_name", "site_name_short", "primary_color",
           "logo_url", "login_subtitle")


def get_branding(
    config: Optional[Dict[str, Any]] = None,
    license_status: Any = None,
) -> Dict[str, Any]:
    """解析生效品牌（合并默认 + ``config['brand']`` overlay + 白标 gating）。

    返回 dict：site_name / site_name_short / primary_color / logo_url /
    login_subtitle / show_powered_by / powered_by_text / white_label。
    """
    cfg = config or {}
    b = cfg.get("brand", {}) or {}
    # 兼容旧字段：web_admin.site_name 作为 site_name 的次选默认
    legacy_name = (cfg.get("web_admin", {}) or {}).get("site_name", "") or ""

    out: Dict[str, Any] = {
        "site_name": str(b.get("site_name") or legacy_name or DEFAULT_SITE_NAME),
        "site_name_short": str(
            b.get("site_name_short") or DEFAULT_SITE_NAME_SHORT),
        "primary_color": str(b.get("primary_color") or "").strip(),
        "logo_url": str(b.get("logo_url") or "").strip(),
        "login_subtitle": str(b.get("login_subtitle") or "").strip(),
    }

    # 白标 gating：能否去除厂商署名
    allowed = _white_label_allowed(license_status)
    want_hide = bool(b.get("hide_powered_by", False))
    out["white_label"] = allowed
    out["show_powered_by"] = not (want_hide and allowed)
    out["powered_by_text"] = POWERED_BY_TEXT
    return out


def _white_label_allowed(license_status: Any) -> bool:
    """白标功能位是否放行；无授权上下文（None）时默认放行（开发/现网零破坏）。"""
    if license_status is None:
        return True
    try:
        from src.licensing import feature_allowed

        return bool(feature_allowed(license_status, "white_label"))
    except Exception:
        return True
