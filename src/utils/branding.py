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

# ── 无界科技 · 智聊 ChatX（本 repo 默认品牌族）────────────────────────────
DEFAULT_COMPANY_NAME = "无界科技"
DEFAULT_COMPANY_NAME_EN = "BOUNDLESS"
DEFAULT_PRODUCT_NAME = "智聊"
DEFAULT_PRODUCT_NAME_EN = "ChatX"
DEFAULT_SITE_NAME = "无界科技 · 智聊"
DEFAULT_SITE_NAME_SHORT = "无界科技"
DEFAULT_SIDEBAR_NAME = "无界 · 智聊"
DEFAULT_LOGIN_SUBTITLE = "管理控制台"
POWERED_BY_TEXT = "Powered by 无界科技"
DEFAULT_WEBSITE_URL = "https://usdt2026.cc"
DEFAULT_BRAND_PATH = "/brand"

# 静态资源路径（相对 /static）
BRAND_MARK_URL = "/static/brand/boundless-mark-256.png"
PRODUCT_CHATX_ICON_URL = "/static/brand/chatx.png"

_FIELDS = (
    "site_name",
    "site_name_short",
    "company_name",
    "product_name",
    "primary_color",
    "logo_url",
    "login_subtitle",
)


def get_branding(
    config: Optional[Dict[str, Any]] = None,
    license_status: Any = None,
) -> Dict[str, Any]:
    """解析生效品牌（合并默认 + ``config['brand']`` overlay + 白标 gating）。

    返回 dict：site_name / site_name_short / company_name / product_name /
    sidebar_name / login_line / primary_color / logo_url / login_subtitle /
    show_powered_by / powered_by_text / white_label / brand_mark_url /
    product_icon_url。
    """
    cfg = config or {}
    b = cfg.get("brand", {}) or {}
    legacy_name = (cfg.get("web_admin", {}) or {}).get("site_name", "") or ""

    company = str(b.get("company_name") or DEFAULT_COMPANY_NAME).strip()
    product = str(b.get("product_name") or DEFAULT_PRODUCT_NAME).strip()
    site_short = str(b.get("site_name_short") or DEFAULT_SITE_NAME_SHORT).strip()

    site_name = str(b.get("site_name") or legacy_name or "").strip()
    if not site_name:
        site_name = f"{company} · {product}"

    login_sub = str(b.get("login_subtitle") or DEFAULT_LOGIN_SUBTITLE).strip()
    sidebar = str(b.get("sidebar_name") or "").strip()
    if not sidebar:
        if company == DEFAULT_COMPANY_NAME and product == DEFAULT_PRODUCT_NAME:
            sidebar = DEFAULT_SIDEBAR_NAME
        else:
            co_short = site_short or company
            sidebar = f"{co_short} · {product}" if co_short != product else product

    logo = str(b.get("logo_url") or "").strip()
    website = str(b.get("website_url") or DEFAULT_WEBSITE_URL).strip().rstrip("/")

    out: Dict[str, Any] = {
        "site_name": site_name,
        "site_name_short": site_short,
        "company_name": company,
        "company_name_en": DEFAULT_COMPANY_NAME_EN,
        "product_name": product,
        "product_name_en": DEFAULT_PRODUCT_NAME_EN,
        "sidebar_name": sidebar,
        "login_line": f"{product} · {login_sub}",
        "primary_color": str(b.get("primary_color") or "").strip(),
        "logo_url": logo,
        "login_subtitle": login_sub,
        "brand_mark_url": logo or BRAND_MARK_URL,
        "product_icon_url": PRODUCT_CHATX_ICON_URL,
        "website_url": website,
        "brand_hub_url": f"{website}{DEFAULT_BRAND_PATH}",
    }

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


def brand_catalog() -> Dict[str, Any]:
    """品牌族结构化清单（供静态 JSON / 文档 / 前端只读引用）。"""
    return {
        "company": {"zh": DEFAULT_COMPANY_NAME, "en": DEFAULT_COMPANY_NAME_EN},
        "product": {"zh": DEFAULT_PRODUCT_NAME, "en": DEFAULT_PRODUCT_NAME_EN},
        "site_name": DEFAULT_SITE_NAME,
        "tagline": {"zh": "让沟通，无界", "en": "Communication, Boundless."},
        "assets": {
            "mark": BRAND_MARK_URL,
            "product_icon": PRODUCT_CHATX_ICON_URL,
        },
        "links": {
            "website": DEFAULT_WEBSITE_URL,
            "brand_path": DEFAULT_BRAND_PATH,
        },
        "products": [
            {"key": "facex", "zh": "幻颜", "en": "FaceX", "emoji": "🎭"},
            {"key": "voicex", "zh": "幻声", "en": "VoiceX", "emoji": "🎙"},
            {"key": "livex", "zh": "幻影", "en": "LiveX", "emoji": "🎬"},
            {"key": "lingox", "zh": "通译", "en": "LingoX", "emoji": "🌐"},
            {"key": "chatx", "zh": "智聊", "en": "ChatX", "emoji": "💬"},
        ],
    }


def pwa_manifest(branding: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """坐席工作台 PWA manifest（合并生效品牌）。"""
    b = branding or {}
    product = str(b.get("product_name") or DEFAULT_PRODUCT_NAME).strip()
    company = str(b.get("company_name") or DEFAULT_COMPANY_NAME).strip()
    name = f"{product} · 坐席工作台"
    short = product
    desc = f"{company} {product} — 统一收件箱 · 多平台 AI 客服坐席工作台"
    mark = str(b.get("brand_mark_url") or BRAND_MARK_URL)
    theme = str(b.get("primary_color") or "#1b2038").strip() or "#1b2038"
    return {
        "name": name,
        "short_name": short,
        "description": desc,
        "id": "/workspace",
        "start_url": "/workspace?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui", "browser"],
        "orientation": "any",
        "background_color": "#1b2038",
        "theme_color": theme,
        "lang": "zh-CN",
        "categories": ["business", "productivity"],
        "icons": [
            {"src": mark, "sizes": "256x256", "type": "image/png", "purpose": "any"},
            {"src": PRODUCT_CHATX_ICON_URL, "sizes": "256x256", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-maskable.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
        ],
    }
