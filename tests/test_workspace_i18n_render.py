"""③-P：工作台页面「中英双语」服务端渲染门禁（真渲染实测，非静态推断）。

i18n 覆盖门禁（test_i18n_coverage）只证明「每个 key 两套字典都在」；本门禁更进一步——
启真 app、过真 ``inject_i18n`` 中间件，按 ``?lang=`` 渲染出真 HTML，验证 ③-N/③-O 成果：

- ``<title>`` 服务端就是对应语言（en 首屏即英文，零闪烁——不靠 JS 跑完才对）。
- ``<html lang>`` 合法且随语言（zh→``zh-CN``、en→``en``；曾把 en 渲成非法 ``en-CN``）。
- ``window.WS_I18N`` 注入的整包译表就是该语言（客户端 ``window.T()`` 的数据源）。
- ``window.WS_LOCALE`` 随语言注入（③-Q：``wsFmt*`` 日期/时间格式化的 BCP47 locale）。

任一工作台页因模板语法错误渲染 500、或标题/语言/字典回退，CI 立刻点名。
"""

import json
import re

import pytest

# (路由, 该页 <title> block 用的 page_title 键)
_PAGES = [
    ("/workspace", "inbox.page_title"),
    ("/workspace/dash", "dash.page_title"),
    ("/workspace/drafts", "draft.page_title"),
]


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return m.group(1).strip() if m else ""


def _html_lang(html: str) -> str:
    m = re.search(r'<html[^>]*\blang="([^"]*)"', html)
    return m.group(1) if m else ""


def _ws_i18n(html: str) -> dict:
    """从 ``window.WS_I18N = {...};`` 注入处精确抠出整包译表（raw_decode 忽略尾随 ``;``）。"""
    marker = "window.WS_I18N = "
    i = html.find(marker)
    assert i != -1, "WS_I18N 注入缺失"
    obj, _ = json.JSONDecoder().raw_decode(html[i + len(marker):])
    return obj


def _ws_locale(html: str) -> str:
    m = re.search(r'window\.WS_LOCALE\s*=\s*"([^"]+)"', html)
    assert m, "WS_LOCALE 注入缺失"
    return m.group(1)


@pytest.mark.parametrize("lang,lang_attr,date_locale", [("zh", "zh-CN", "zh-CN"), ("en", "en", "en-US")])
def test_workspace_pages_localized_title_lang_and_dict(auth_client, lang, lang_attr, date_locale):
    from src.web.web_i18n import get_translations

    tr = get_translations(lang)
    for path, title_key in _PAGES:
        r = auth_client.get(f"{path}?lang={lang}")
        assert r.status_code == 200, (path, lang, r.status_code)
        html = r.text
        # <html lang> 合法且随语言（a11y / 浏览器翻译 / :lang() CSS）
        assert _html_lang(html) == lang_attr, (path, lang, _html_lang(html))
        # <title> 服务端就是该语言（block 部分 = page_title），首屏零闪烁
        title = _title(html)
        assert title.startswith(tr[title_key]), (path, lang, title, tr[title_key])
        # 注入的整包译表就是该语言（客户端 T() 的数据源），且含本页标题键
        d = _ws_i18n(html)
        assert d.get(title_key) == tr[title_key], (path, lang)
        assert d.get("base.page_title") == tr["base.page_title"], (path, lang)
        # 日期/时间格式化 locale 与 ui_lang 同源（wsFmtDate/Time/DateTime）
        assert _ws_locale(html) == date_locale, (path, lang, _ws_locale(html))


def test_unified_inbox_copilot_tooltips_localized(auth_client):
    """③-S9j 回归：unified_inbox 两处曾漏网的 title 提示（copilot App 切换 + iframe 业务面板）
    已接 data-i18n-title——EN 下客户端译表含这两键且为真英文（非键名回退、无残留中文）。

    这两处此前带 CJK 的 title= 属性却无 data-i18n-title，源码 cap-0 看不见（同行有 data-i18n /
    属于跨行标签），只有 EN 渲染门禁能抓——修复即补 data-i18n-title + 键，供 wsApplyI18n 换字。
    """
    r = auth_client.get("/workspace?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("inbox.cp.app_toggle_t", "inbox.cp.app_frame_t"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


def test_lang_switch_changes_same_page(auth_client):
    """同一页 ?lang=zh 与 ?lang=en 必须给出不同语言的标题/lang/字典（真随请求切，非缓存同一份）。"""
    zr = auth_client.get("/workspace?lang=zh").text
    er = auth_client.get("/workspace?lang=en").text
    assert _title(zr) != _title(er)
    assert _html_lang(zr) == "zh-CN" and _html_lang(er) == "en"
    assert _ws_i18n(zr)["inbox.page_title"] == "聊天工作台"
    assert _ws_i18n(er)["inbox.page_title"] == "Chat Workspace"
    assert _ws_locale(zr) == "zh-CN" and _ws_locale(er) == "en-US"


# 日期 locale 门禁靶面：所有外壳页 + 两套外壳本体 + 共享脚本/bootstrap partial。
# 外壳页由 shelled_templates() 自动发现（新页继承外壳即纳入），无需手维护清单。
def _date_gate_templates():
    from scripts.i18n_scan import shelled_templates

    # ops_overview.html 不继承外壳但走 Jinja 渲染（patched templates 注入 ui_lang/i18n），
    # ③-S2b 起 {% include %} 同一 partial + 条件化 <html lang>，故按外壳口径一并纳入主日期门禁。
    # ③-S9k：ops 运营家族三页同样升级为 Jinja + {% include _i18n_bootstrap %}，按同口径纳入。
    return sorted(
        set(shelled_templates())
        | {"base.html", "workspace_base.html", "_i18n_bootstrap.html",
           "_rpa_shared_scripts.html", "ops_overview.html",
           "ops/merge_reviews.html", "ops/contacts.html", "ops/mobile_handoffs.html"}
    )


# ③-S9k：ops 运营家族三页——原为 raw-served standalone（_load_ops_html 直出原始 HTML、不过 Jinja、
# 靠 /static/ops_locale.js 做日期本地化）。本轮升级为 Jinja 渲染 + {% include _i18n_bootstrap %}，
# 由共享 partial 统一提供 window.T/Tf + wsFmt*（单一真源），退役 ops_locale.js。故门禁反转：
# 这些页现在必须 include 共享 bootstrap、且不得再引 ops_locale.js。
_JINJA_OPS_PAGES = ["ops/merge_reviews.html", "ops/contacts.html", "ops/mobile_handoffs.html"]


def test_sealed_templates_no_hardcoded_zh_cn_date_locale():
    """③-Q/③-S2 防回潮：所有外壳页 + 外壳本体 + 共享脚本的 JS 不得硬编码 ``'zh-CN'`` 作 toLocale* locale。

    允许：``<html lang>`` / ``WS_LOCALE`` 服务端注入（含 ui_lang）/ ``wsDateLocale`` 回落常量。
    standalone ops 页（不继承外壳、无 wsFmt*）不在本门禁，见 ``DEFERRED_STANDALONE_DATE_PAGES``。
    """
    from scripts.i18n_scan import _TPL_DIR

    offenders = []
    for name in _date_gate_templates():
        for i, line in enumerate((_TPL_DIR / name).read_text(encoding="utf-8").splitlines(), 1):
            if "'zh-CN'" not in line and '"zh-CN"' not in line:
                continue
            if "<html" in line and "ui_lang" in line:
                continue
            if "WS_LOCALE" in line and "ui_lang" in line:
                continue
            if "wsDateLocale" in line:
                continue
            offenders.append(f"{name}:{i}: {line.strip()[:100]}")
    assert not offenders, "硬编码 zh-CN 日期 locale:\n" + "\n".join(offenders)


def test_jinja_ops_pages_use_shared_bootstrap():
    """③-S9k：ops 运营家族三页升级为 Jinja + 共享 bootstrap 后，日期/文案单一真源防回潮——
    ① 必须 {% include _i18n_bootstrap.html %}（拿 window.T/Tf + wsFmt*）；
    ② 不得再引退役的 /static/ops_locale.js；
    ③ <html lang> 必须条件化（含 ui_lang），不得硬编码 zh-CN；
    ④ 不得内联 new Date().toLocale* / 在 toLocale 上硬编码 zh-CN（应走 wsFmt*）。
    """
    from scripts.i18n_scan import _TPL_DIR

    offenders = []
    for name in _JINJA_OPS_PAGES:
        text = (_TPL_DIR / name).read_text(encoding="utf-8")
        if "_i18n_bootstrap.html" not in text:
            offenders.append(f"{name}: 未 include 共享 _i18n_bootstrap.html")
        if "ops_locale.js" in text:
            offenders.append(f"{name}: 仍引用退役的 /static/ops_locale.js")
        for i, line in enumerate(text.splitlines(), 1):
            if "<html" in line and "lang=" in line and "ui_lang" not in line:
                offenders.append(f"{name}:{i}: <html lang> 未条件化（缺 ui_lang）")
            if "toLocale" in line and ("'zh-CN'" in line or '"zh-CN"' in line):
                offenders.append(f"{name}:{i}: toLocale 硬编码 zh-CN")
        if re.search(r"new Date\([^\n]*?\)\.toLocale", text):
            offenders.append(f"{name}: 内联 new Date().toLocale*（应走 wsFmt*）")
    assert not offenders, "Jinja ops 页日期/文案单一真源门禁:\n" + "\n".join(offenders)


# ③-S9k：ops 家族三页静态层「双语真渲染」——静态走服务端 (i18n or {}).get，故 zh/en 直出对应语言。
# 每页取一处「仅静态可见」的锚点（JS 里 window.T('k') 渲染期不解析，不能做锚点）验证真切换。
_OPS_STATIC_ANCHORS = [
    # (模板, active, (zh 锚点, en 锚点))
    ("ops/merge_reviews.html", "/ops/merge-reviews", ("合并审核队列", "Merge Review Queue")),
    ("ops/contacts.html", "/ops/contacts", ("漏斗阶段分布（快照）", "Funnel Stage Distribution (Snapshot)")),
    ("ops/mobile_handoffs.html", "/ops/mobile-handoffs", ("暂无交接单", "No handoffs")),
]


@pytest.mark.parametrize("tmpl,active,anchors", _OPS_STATIC_ANCHORS,
                         ids=[a[0].split("/")[-1] for a in _OPS_STATIC_ANCHORS])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_ops_pages_static_localized(tmpl, active, anchors, lang):
    """③-S9k：ops 三页经 Jinja + 共享 bootstrap + ops/_ops_nav 渲染，静态锚点随语言真切换——
    zh 出中文锚点、en 出英文锚点且不残留对应中文（证明 raw-served → Jinja 迁移后 i18n 真生效，
    且 {% include _i18n_bootstrap %} / {% include ops/_ops_nav %} 都能被 loader 解析、不报错）。"""
    from src.web.admin import templates
    from src.web.web_i18n import get_translations

    zh_anchor, en_anchor = anchors
    html = templates.env.get_template(tmpl).render(
        i18n=get_translations(lang), ui_lang=lang, active=active,
    )
    # 共享 bootstrap 注入 + 导航 i18n 渲染成功（include 链路通）。
    assert "window.WS_I18N" in html, (tmpl, lang, "bootstrap 未注入")
    if lang == "zh":
        assert zh_anchor in html, (tmpl, "zh 缺锚点", zh_anchor)
    else:
        assert en_anchor in html, (tmpl, "en 缺锚点", en_anchor)
        assert zh_anchor not in html, (tmpl, "en 残留中文", zh_anchor)


def test_missing_lang_defaults_to_zh(auth_client):
    """无 ?lang= 且无 cookie → 回落 zh（与 inject_i18n 默认一致）。"""
    html = auth_client.get("/workspace").text
    assert _html_lang(html) == "zh-CN"
    assert _title(html).startswith("聊天工作台")
    assert _ws_locale(html) == "zh-CN"


# ── ③-S：管理后台外壳 i18n 地基（base.html 与 workspace_base.html 共享同一 bootstrap partial）──

def test_both_shells_include_shared_i18n_bootstrap():
    """两套外壳都必须 include 共享 bootstrap —— 单一真源、防两套脚本各自漂移。"""
    from scripts.i18n_scan import _TPL_DIR

    for shell in ("base.html", "workspace_base.html"):
        src = (_TPL_DIR / shell).read_text(encoding="utf-8")
        assert "_i18n_bootstrap.html" in src, f"{shell} 未 include 共享 i18n bootstrap"


def test_i18n_bootstrap_partial_localizes_by_lang():
    """共享 partial 单独渲染：WS_LANG/WS_LOCALE 随语言，且 T/Tf/wsFmt* 助手齐全。"""
    from src.web.admin import templates
    from src.web.web_i18n import get_translations

    for lang, locale in (("zh", "zh-CN"), ("en", "en-US")):
        out = templates.env.get_template("_i18n_bootstrap.html").render(
            i18n=get_translations(lang), ui_lang=lang,
        )
        assert f'window.WS_LOCALE = "{locale}"' in out, (lang, "WS_LOCALE")
        assert f'window.WS_LANG = "{lang}"' in out, (lang, "WS_LANG")
        for fn in ("window.wsFmtDate", "window.wsFmtTime", "window.wsFmtDateTime",
                   "window.wsDateLocale", "window.T ", "window.Tf ", "window.wsApplyI18n"):
            assert fn in out, (lang, fn)


@pytest.mark.parametrize("lang,lang_attr,date_locale", [("zh", "zh-CN", "zh-CN"), ("en", "en", "en-US")])
def test_admin_shell_localized_lang_and_locale(auth_client, lang, lang_attr, date_locale):
    """管理后台页（``/help`` → base.html）端到端：``<html lang>`` + WS_LOCALE + wsFmt* 助手随
    ``?lang=`` 正确注入（地基真的接到了真路由 / 真中间件上）。

    用 ``/help`` 而非 ``/``：后者在 simple 模式会 303→/cases，丢掉 ``?lang=`` 查询参数，不适合做语言门禁靶点。
    """
    r = auth_client.get(f"/help?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    assert _ws_locale(html) == date_locale, (lang, _ws_locale(html))
    for fn in ("window.wsFmtDate", "window.wsFmtDateTime", "window.WS_LOCALE"):
        assert fn in html, (lang, fn)


# ── ③-S2：已扫日期的管理后台页端到端（base.html；/rpa-overview 还含共享 _rpa_shared_scripts）──
# /admin/ops（ops_overview.html）③-S2b 起也 Jinja 渲染 + {% include %} 共享 partial，纳入同一端到端门禁。
_SWEPT_ADMIN_PAGES = ["/cases", "/rpa-overview", "/admin/ops"]
_LEGACY_DATE_MARKERS = (
    "toLocaleString('zh-CN'", 'toLocaleString("zh-CN"',
    "toLocaleDateString('zh-CN'", 'toLocaleDateString("zh-CN"',
    "toLocaleTimeString('zh-CN'", 'toLocaleTimeString("zh-CN"',
    "toTimeString().slice",
)


# ── ③-S3：base.html 共享侧栏导航「中英双语」服务端渲染（原硬编码 span 收口为 key 后真切换）──
# 选三个仅出现在导航、且简洁/完整模式都在的标签做断言（避开 ai_studio/whatsapp 等仅完整模式项）。
_CHROME_NAV_BILINGUAL = [
    ("主动关怀", "Proactive Care"),
    ("情景记忆", "Episodic Memory"),
    ("危机审计", "Crisis Audit"),
]


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_admin_chrome_nav_localized(auth_client, lang):
    """③-S3：共享外壳（base.html via /cases）侧栏导航随 ?lang= 真切换——
    zh 出中文标签、en 出英文标签且不残留对应中文（证明硬编码 span 已收口到 i18n key）。"""
    html = auth_client.get(f"/cases?lang={lang}").text
    for zh_label, en_label in _CHROME_NAV_BILINGUAL:
        if lang == "zh":
            assert zh_label in html, f"zh 缺导航标签 {zh_label!r}"
        else:
            assert en_label in html, f"en 缺导航标签 {en_label!r}"
            assert zh_label not in html, f"en 渲染仍残留中文 {zh_label!r}（i18n 未生效）"


# ③-S3：命令面板（Ctrl+K）name 字段经 |tojson 注入 JS。
# 注意：|tojson 会把 CJK 转义成 \uXXXX（浏览器正常解码，但源码里搜不到字面中文），
# 故这里走「英文向」断言：en 出英文 name；zh 不应出现英文 name（证明随语言切换）。
# 取「面板独有」英文（不会出现在侧栏/快捷键弹窗里的纯面板项）。
_CMD_PALETTE_EN_ONLY = ["Reload current page"]  # cmd_reload，仅命令面板出现


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_admin_command_palette_localized(auth_client, lang):
    """③-S3：Ctrl+K 命令面板 name 经 |tojson 服务端本地化随 ?lang= 切换；
    en 出英文面板项、zh 不残留英文（证明 JS 数组里的硬编码 name 已收口到 i18n key）。"""
    html = auth_client.get(f"/cases?lang={lang}").text
    for en_label in _CMD_PALETTE_EN_ONLY:
        if lang == "en":
            assert en_label in html, f"en 缺面板项 {en_label!r}"
        else:
            assert en_label not in html, f"zh 面板残留英文 {en_label!r}（i18n 未生效）"


@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
@pytest.mark.parametrize("path", _SWEPT_ADMIN_PAGES)
def test_swept_admin_pages_localized_dates(auth_client, path, lang, lang_attr):
    """③-S2：已扫日期的后台页运行期渲染——200 + wsFmt* 助手到位 + 无 legacy 日期写法 + lang 随语言。

    端到端坐实「日期扫荡确实落到真路由 / 真外壳」：仅取 page_auth-only、非角色门控的稳定页
    （/cases、/rpa-overview）；后者还内含共享 _rpa_shared_scripts，一并验证共享脚本的扫荡成果。
    """
    r = auth_client.get(f"{path}?lang={lang}")
    assert r.status_code == 200, (path, lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (path, lang, _html_lang(html))
    for fn in ("window.wsFmtDate", "window.wsFmtTime", "window.wsFmtDateTime"):
        assert fn in html, (path, lang, f"missing {fn}")
    for m in _LEGACY_DATE_MARKERS:
        assert m not in html, (path, lang, f"legacy marker {m!r}")
    assert not re.search(r"new Date\([^\n]*?\)\.toLocale", html), (path, lang, "inline new Date().toLocale*")


# ── messenger_rpa：真路由 + inject_i18n 中间件「客户端译表随语言、含 JS 层键」端到端冒烟 ──
# 文件级「零裸 CJK / 双语齐备 / EN 渲染零泄漏」由 test_i18n_coverage 四锁把关（源码 cap-0 +
# 自有 <script> 零 CJK + bare-Jinja EN 渲染零泄漏）。本处补「真 app」维度：?lang= 经真路由 +
# 真中间件后，注入客户端的 ``window.WS_I18N``（window.T/Tf 的数据源）确为该语言整包，且既含静态层
# (msg_s*) 又含 JS 层 (msg_js_*) 代表键——坐实「服务端注译表 → 客户端 T() 取该语言」整链路通，
# 防 JS 层键漏进客户端时 window.T 回退键名（界面显示 'msg_js_123'）。
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_messenger_rpa_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/messenger-rpa?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    # 静态层 + JS 层（含 Tf 占位符短语）代表键，注入值都该等于该语言译表
    for k in ("msg_s001", "msg_js_1504", "msg_js_p72"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_messenger_rpa_en_js_keys_are_english(auth_client):
    """en 下客户端译表里 JS 层代表键为真英文（非键名回退、无残留中文）——
    与 zh 版互为对照，证明 JS 层 msg_js_* 真随语言切换、未照抄中文。"""
    r = auth_client.get("/messenger-rpa?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("msg_s001", "msg_js_1504", "msg_js_p72"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── whatsapp_rpa：同 messenger 口径的「真 app 客户端译表随语言、含静态/JS/Tf 三层键」冒烟 ──
# 静态层 wa_s*（Jinja get）、JS 层 wa_js*（window.T）、Tf 短语 wa_js_p*（window.Tf 占位符插值）
# 三层代表键都该经真路由 + inject_i18n 注入客户端整包 window.WS_I18N，且随语言取对应译文。
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_whatsapp_rpa_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/whatsapp-rpa?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("wa_s002", "wa_js_009", "wa_js_p13"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_whatsapp_rpa_en_js_keys_are_english(auth_client):
    """en 下 whatsapp 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/whatsapp-rpa?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("wa_s002", "wa_js_009", "wa_js_p13"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── line_rpa：同口径「真 app 客户端译表随语言、含静态/JS/Tf 三层键」冒烟 ──
# 静态层 ln_s*（Jinja get）、JS 层 ln_js*（window.T）、Tf 短语 ln_js_p*（window.Tf 占位符插值）
# 三层代表键都该经真路由 /line-rpa + inject_i18n 注入客户端整包 window.WS_I18N，随语言取对应译文。
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_line_rpa_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/line-rpa?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("ln_s002", "ln_js_012", "ln_js_p01"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_line_rpa_en_js_keys_are_english(auth_client):
    """en 下 line 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/line-rpa?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("ln_s002", "ln_js_012", "ln_js_p01"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── telegram：原生 mtproto 运营台，同口径三层键（静态 tg_s* / JS tg_js* / Tf tg_js_p*）冒烟 ──
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_telegram_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/telegram?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("tg_s002", "tg_js_013", "tg_js_p01"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_telegram_en_js_keys_are_english(auth_client):
    """en 下 telegram 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/telegram?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("tg_s002", "tg_js_013", "tg_js_p01"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── dashboard：落地首屏（route "/"），同口径三层键（静态 db_s* / JS db_js* / Tf db_js_p*）冒烟 ──
# 注：``/`` 在 simple 模式会 303→/cases 丢掉 ?lang=（见 resolve_ui_mode 默认 simple），
# 故显式带 ``ui_mode=full`` cookie 让根路由真渲染 dashboard.html。
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_dashboard_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/?lang={lang}", cookies={"ui_mode": "full"})
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("db_s001", "db_js_019", "db_js_p01"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_dashboard_en_js_keys_are_english(auth_client):
    """en 下 dashboard 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/?lang=en", cookies={"ui_mode": "full"})
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("db_s001", "db_js_019", "db_js_p01"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── settings：系统设置（route "/settings"，直渲无模式跳转），同口径三层键（set_s* / set_js* / set_js_p*）──
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_settings_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/settings?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("set_s018", "set_js_003", "set_js_p01"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_settings_en_js_keys_are_english(auth_client):
    """en 下 settings 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/settings?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("set_s018", "set_js_003", "set_js_p01"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── knowledge：知识库管理（route "/knowledge"，直渲），同口径三层键（kb_s* / kb_js* / kb_js_p*）──
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_knowledge_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/knowledge?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    for k in ("kb_s007", "kb_js_046", "kb_js_p04"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))


def test_knowledge_en_js_keys_are_english(auth_client):
    """en 下 knowledge 三层代表键为真英文（非键名回退、无残留中文）。"""
    r = auth_client.get("/knowledge?lang=en")
    assert r.status_code == 200, r.status_code
    d = _ws_i18n(r.text)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for k in ("kb_s007", "kb_js_046", "kb_js_p04"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"


# ── agent_perf：坐席绩效看板（route "/workspace/agent-perf"，主管专属；auth_client=master 可进）──
# 首个「工作台家族」内容页，i18n 机制与前几页不同：静态层走 **服务端 (i18n or {}).get(ap_s*)**——
# 直出当前语言、无「先中文后 JS 换字」闪烁、免 JS 亦可读；JS 层照旧 window.T/Tf（ap_js*/ap_js_p*）。
# 故除「客户端整包 WS_I18N 随语言」外，额外坐实「静态英文文案已被服务端直接渲进 HTML」（其余工作台
# 页此处仍是中文、靠 data-i18n 加载时换）——这是本页机制的关键差异点。
@pytest.mark.parametrize("lang,lang_attr", [("zh", "zh-CN"), ("en", "en")])
def test_agent_perf_localized_dict_injected(auth_client, lang, lang_attr):
    from src.web.web_i18n import get_translations

    r = auth_client.get(f"/workspace/agent-perf?lang={lang}")
    assert r.status_code == 200, (lang, r.status_code)
    html = r.text
    assert _html_lang(html) == lang_attr, (lang, _html_lang(html))
    d = _ws_i18n(html)
    tr = get_translations(lang)
    # 客户端整包（JS 层 window.T 数据源）随语言；含静态/JS/Tf 三层代表键
    for k in ("ap_s001", "ap_js_001", "ap_js_p01"):
        assert d.get(k) == tr[k], (lang, k, d.get(k))
    # 静态层服务端直渲：标题英文/中文已在 HTML 里（不靠 JS 换字）——本页独有的 no-flash 证据
    assert tr["ap_s001"] in html, (lang, "static i18n.get not server-rendered")


def test_agent_perf_en_static_and_js_are_english(auth_client):
    """en 下 agent_perf 静态层已服务端渲成英文文案，且 JS 三层代表键为真英文（非回退、无残留中文）。"""
    r = auth_client.get("/workspace/agent-perf?lang=en")
    assert r.status_code == 200, r.status_code
    html = r.text
    cjk = re.compile(r"[\u4e00-\u9fff]")
    # 静态英文直接出现在服务端 HTML（server-side i18n.get 直出）
    for en_text in ("Agent Performance Dashboard", "Active agents", "Daily disposition trend"):
        assert en_text in html, f"en 静态文案未服务端渲染: {en_text!r}"
    # JS 层客户端译表代表键为真英文
    d = _ws_i18n(html)
    for k in ("ap_s001", "ap_js_001", "ap_js_p01"):
        v = d.get(k)
        assert v and v != k, f"en 键 {k} 缺失或回退为键名: {v!r}"
        assert not cjk.search(v), f"en 键 {k} 残留中文: {v!r}"
