"""C1-1 白标/贴牌测试：品牌解析 / 白标 gating / overlay 落盘 / 路由。"""

import time

import pytest

from src.utils.branding import (
    DEFAULT_SITE_NAME,
    DEFAULT_SITE_NAME_SHORT,
    get_branding,
)


def test_defaults_when_no_brand():
    b = get_branding({})
    assert b["site_name"] == DEFAULT_SITE_NAME
    assert b["site_name_short"] == DEFAULT_SITE_NAME_SHORT
    assert b["primary_color"] == ""
    assert b["show_powered_by"] is True  # 无授权上下文默认放行但 hide 未设 → 显示


def test_overlay_overrides():
    cfg = {"brand": {"site_name": "星辰系统", "site_name_short": "星辰",
                     "primary_color": "#ff8800", "logo_url": "http://x/l.png",
                     "login_subtitle": "成交每一单"}}
    b = get_branding(cfg)
    assert b["site_name"] == "星辰系统"
    assert b["site_name_short"] == "星辰"
    assert b["primary_color"] == "#ff8800"
    assert b["logo_url"] == "http://x/l.png"
    assert b["login_subtitle"] == "成交每一单"


def test_legacy_web_admin_name_fallback():
    cfg = {"web_admin": {"site_name": "旧名"}}
    assert get_branding(cfg)["site_name"] == "旧名"
    # brand.site_name 优先于 legacy
    cfg2 = {"web_admin": {"site_name": "旧名"}, "brand": {"site_name": "新名"}}
    assert get_branding(cfg2)["site_name"] == "新名"


def _license(enforce, white_label):
    from src.licensing import LicenseManager, issue_license, generate_keypair
    kp = generate_keypair()
    now = time.time()
    feats = {"white_label": True} if white_label else {}
    tok = issue_license(
        {"sub": "X", "plan": "flagship" if white_label else "basic",
         "exp": int(now) + 86400, "features": feats},
        kp["private_hex"])
    return LicenseManager(license_token=tok, public_key_hex=kp["public_hex"],
                          enforce=enforce, now_fn=lambda: now).status()


def test_hide_powered_by_requires_white_label_when_enforce():
    cfg = {"brand": {"hide_powered_by": True}}
    # enforce + 非白标 → 强制保留署名
    b1 = get_branding(cfg, _license(enforce=True, white_label=False))
    assert b1["white_label"] is False
    assert b1["show_powered_by"] is True
    # enforce + 旗舰白标 → 可隐藏署名
    b2 = get_branding(cfg, _license(enforce=True, white_label=True))
    assert b2["white_label"] is True
    assert b2["show_powered_by"] is False


def test_hide_powered_by_allowed_when_enforce_off():
    """enforce 关（社区/现网默认）→ 白标放行，可隐藏署名（零破坏 + 开发友好）。"""
    cfg = {"brand": {"hide_powered_by": True}}
    b = get_branding(cfg, _license(enforce=False, white_label=False))
    assert b["show_powered_by"] is False


def test_save_branding_writes_overlay(tmp_path):
    import yaml
    from src.utils.config_manager import ConfigManager
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telegram: {}\n", encoding="utf-8")
    cm = ConfigManager()
    cm.config_path = cfg_path
    cm.config = {"telegram": {}}
    ok, msg = cm.save_branding({
        "site_name": "星辰", "primary_color": "#abc", "hide_powered_by": True,
        "evil_key": "x",  # 非白名单字段应被丢弃
    })
    assert ok
    overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert overlay["brand"]["site_name"] == "星辰"
    assert overlay["brand"]["primary_color"] == "#abc"
    assert overlay["brand"]["hide_powered_by"] is True
    assert "evil_key" not in overlay["brand"]
    # 即时生效到内存
    assert cm.config["brand"]["site_name"] == "星辰"


def test_branding_routes_registered():
    import inspect
    from src.web.routes import branding_routes
    src = inspect.getsource(branding_routes.register_branding_routes)
    assert "/api/admin/branding" in src


def test_widget_applies_brand_and_powered_by():
    """C1-1：/chat widget 在 web_chat 用默认值时回退品牌色/标题，并按 gating 显示署名。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _widget_html
    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    brand = {"site_name_short": "星辰", "primary_color": "#ff8800",
             "show_powered_by": True, "powered_by_text": "Powered by 华灵科技"}
    html = _widget_html(svc, standalone=True, brand=brand)
    assert "#ff8800" in html        # 品牌主色回退生效
    assert "星辰" in html            # 品牌标题回退生效
    assert "Powered by 华灵科技" in html
    # 关闭署名
    brand["show_powered_by"] = False
    html2 = _widget_html(svc, standalone=True, brand=brand)
    assert "Powered by" not in html2
