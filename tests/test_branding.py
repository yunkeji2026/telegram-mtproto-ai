"""C1-1 白标/贴牌测试：品牌解析 / 白标 gating / overlay 落盘 / 路由。"""

import time

import pytest

from src.utils.branding import (
    DEFAULT_SITE_NAME,
    DEFAULT_SITE_NAME_SHORT,
    DEFAULT_PRODUCT_NAME,
    POWERED_BY_TEXT,
    get_branding,
)


def test_defaults_when_no_brand():
    b = get_branding({})
    assert b["site_name"] == DEFAULT_SITE_NAME
    assert b["site_name_short"] == DEFAULT_SITE_NAME_SHORT
    assert b["product_name"] == DEFAULT_PRODUCT_NAME
    assert b["login_line"] == f"{DEFAULT_PRODUCT_NAME} · 管理控制台"
    assert b["sidebar_name"] == "无界 · 智聊"
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


def test_custom_company_product_sidebar():
    cfg = {"brand": {"company_name": "星辰", "product_name": "智聊", "site_name_short": "星辰"}}
    b = get_branding(cfg)
    assert b["sidebar_name"] == "星辰 · 智聊"
    assert b["login_line"] == "智聊 · 管理控制台"


def test_pwa_manifest_defaults():
    from src.utils.branding import pwa_manifest

    m = pwa_manifest(get_branding({}))
    assert m["short_name"] == "智聊"
    assert "坐席工作台" in m["name"]
    assert any("boundless-mark" in ic["src"] for ic in m["icons"])


def test_brand_catalog_matches_json():
    import json
    from pathlib import Path

    from src.utils.branding import brand_catalog

    p = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "brand" / "brand.json"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    expected = brand_catalog()
    for key in ("company", "product", "site_name", "tagline", "assets", "links", "products"):
        assert on_disk.get(key) == expected.get(key), key


def test_save_branding_writes_overlay(tmp_path):
    import yaml
    from src.utils.config_manager import ConfigManager
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telegram: {}\n", encoding="utf-8")
    cm = ConfigManager()
    cm.config_path = cfg_path
    cm.config = {"telegram": {}}
    ok, msg = cm.save_branding({
        "company_name": "星辰", "product_name": "智聊",
        "site_name": "星辰", "primary_color": "#abc", "hide_powered_by": True,
        "evil_key": "x",  # 非白名单字段应被丢弃
    })
    assert ok
    overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert overlay["brand"]["company_name"] == "星辰"
    assert overlay["brand"]["product_name"] == "智聊"
    assert overlay["brand"]["site_name"] == "星辰"
    assert overlay["brand"]["primary_color"] == "#abc"
    assert overlay["brand"]["hide_powered_by"] is True
    assert "evil_key" not in overlay["brand"]
    # 即时生效到内存
    assert cm.config["brand"]["site_name"] == "星辰"


def test_widget_shows_product_icon():
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _widget_html

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    brand = {
        "product_name": "智聊",
        "product_icon_url": "/static/brand/chatx.png",
        "show_powered_by": False,
    }
    html = _widget_html(svc, standalone=True, brand=brand)
    assert 'class="wc-icon"' in html
    assert "chatx.png" in html


def test_parse_brand_ts():
    from scripts.sync_brand_json import parse_brand_ts

    data = parse_brand_ts()
    assert data["company"]["zh"] == "无界科技"
    assert data["product"]["en"] == "ChatX"
    assert any(p["key"] == "chatx" for p in data["products"])


def test_brand_json_in_sync_with_ts():
    """CI 漂移门禁：website/lib/brand.ts（官网品牌唯一真源）改了却忘重跑
    ``python -m scripts.sync_brand_json --from-ts`` 时，brand.json 会与 TS 脱钩，
    前端各端读到旧品牌数据。此门禁在 CI 早期抓住。

    brand.ts 不在此 checkout（精简部署）→ 优雅跳过（遵循「缺资源优雅跳过」约定）。"""
    import json
    from pathlib import Path

    import pytest

    from scripts.sync_brand_json import TS, build_catalog

    if not TS.is_file():
        pytest.skip("website/lib/brand.ts 不在此 checkout，跳过 TS 漂移门禁")
    expected = build_catalog(from_ts=True)
    p = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "brand" / "brand.json"
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk == expected, (
        "brand.json 与 website/lib/brand.ts 脱钩——请重跑 "
        "`python -m scripts.sync_brand_json --from-ts` 后提交。"
    )


def test_brand_hub_url():
    b = get_branding({})
    assert b["brand_hub_url"].endswith("/brand")


def test_website_url_override_drives_brand_hub():
    cfg = {"brand": {"website_url": "https://boundless.example/"}}
    b = get_branding(cfg)
    assert b["website_url"] == "https://boundless.example"  # 末尾斜杠被规整
    assert b["brand_hub_url"] == "https://boundless.example/brand"


def test_website_url_in_save_allowlist(tmp_path):
    import yaml
    from src.utils.config_manager import ConfigManager
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telegram: {}\n", encoding="utf-8")
    cm = ConfigManager()
    cm.config_path = cfg_path
    cm.config = {"telegram": {}}
    ok, _ = cm.save_branding({"website_url": "https://x.io"})
    assert ok
    overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
    assert overlay["brand"]["website_url"] == "https://x.io"
    import inspect
    from src.web.routes import branding_routes
    src = inspect.getsource(branding_routes.register_branding_routes)
    assert "/api/admin/branding" in src


def test_widget_uses_product_name_for_title():
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _widget_html

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    brand = {"product_name": "智聊", "site_name_short": "无界科技", "show_powered_by": False}
    html = _widget_html(svc, standalone=True, brand=brand)
    assert ">智聊<" in html


def test_widget_applies_brand_and_powered_by():
    """C1-1：/chat widget 在 web_chat 用默认值时回退品牌色/标题，并按 gating 显示署名。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _widget_html
    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    brand = {"site_name_short": "星辰", "primary_color": "#ff8800",
             "show_powered_by": True, "powered_by_text": POWERED_BY_TEXT}
    html = _widget_html(svc, standalone=True, brand=brand)
    assert "#ff8800" in html        # 品牌主色回退生效
    assert "星辰" in html            # 品牌标题回退生效
    assert POWERED_BY_TEXT in html
    # 关闭署名
    brand["show_powered_by"] = False
    html2 = _widget_html(svc, standalone=True, brand=brand)
    assert "Powered by" not in html2


def test_embed_js_applies_brand_theme():
    """embed 悬浮气泡在 web_chat 用默认蓝时回退品牌主色——与聊天窗同色（白标一致）。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _embed_js

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    js = _embed_js(svc, brand={"primary_color": "#ff8800"})
    assert "#ff8800" in js       # 品牌主色进气泡
    assert "#2563eb" not in js   # 默认蓝已被回退替换


def test_embed_js_keeps_explicit_theme_over_brand():
    """web_chat 显式配了非默认 theme 时，不被品牌主色覆盖（显式优先）。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _embed_js

    svc = WebChatService(title="在线客服", theme_color="#123456")
    js = _embed_js(svc, brand={"primary_color": "#ff8800"})
    assert "#123456" in js
    assert "#ff8800" not in js


def test_embed_js_renders_brand_icon():
    """悬浮气泡渲染品牌图标：默认回落 chatx，带 <img> + SVG 兜底。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _embed_js

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    js = _embed_js(svc, brand={"product_icon_url": "/static/brand/chatx.png"})
    assert "/static/brand/chatx.png" in js   # 图标进气泡
    assert "createElement('img')" in js       # 以 img 渲染
    assert "onerror" in js                     # 加载失败回落 SVG


def test_safe_color_rejects_injection():
    """颜色白名单：合法 hex/rgb/命名色放行；含 CSS/JS 越界字符的注入串回落默认。"""
    from src.web.routes.web_chat_routes import _safe_color, _DEFAULT_THEME

    assert _safe_color("#ff8800") == "#ff8800"
    assert _safe_color("#abc") == "#abc"
    assert _safe_color("rgba(10, 20, 30, .5)") == "rgba(10, 20, 30, .5)"
    assert _safe_color("cornflowerblue") == "cornflowerblue"
    # 注入串 → 回落默认
    assert _safe_color("red;}</style><script>alert(1)</script>") == _DEFAULT_THEME
    assert _safe_color("#fff'};alert(1);//") == _DEFAULT_THEME
    assert _safe_color("") == _DEFAULT_THEME


def test_embed_js_injection_is_json_encoded():
    """embed.js 的 ICON 用 JSON 编码注入——恶意 logo 值被转义、无法越出 JS 字符串。"""
    import json as _json
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _embed_js

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    mal = 'http://x/a";evil()//.png'
    js = _embed_js(svc, brand={"logo_url": mal})
    # JS 直接赋值为 JSON 编码后的字面量（双引号被转义为 \"），无裸越界
    assert ("ICON=" + _json.dumps(mal)) in js


def test_embed_and_widget_prefer_white_label_logo():
    """白标自定义 logo_url 设了时，聊天窗头部与悬浮气泡都用客户 logo（非 chatx）。"""
    from src.integrations.web_chat.service import WebChatService
    from src.web.routes.web_chat_routes import _embed_js, _widget_html

    svc = WebChatService(title="在线客服", theme_color="#2563eb")
    brand = {
        "logo_url": "https://cdn.example/star-logo.png",
        "product_icon_url": "/static/brand/chatx.png",
        "show_powered_by": False,
    }
    js = _embed_js(svc, brand=brand)
    html = _widget_html(svc, standalone=True, brand=brand)
    assert "https://cdn.example/star-logo.png" in js
    assert "https://cdn.example/star-logo.png" in html
    assert "chatx.png" not in js   # 自定义 logo 优先于产品图标
