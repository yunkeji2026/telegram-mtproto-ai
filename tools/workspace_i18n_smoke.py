"""workspace_i18n_smoke.py — 工作台 i18n / 日期 locale 冒烟（真 HTTP，需 main.py 在跑）。

③-R：把「起服务肉眼走查」 codify 成可重复脚本——登录后按 ?lang= 拉 3 张密封页，
断言 title / html lang / WS_LOCALE / wsFmt* 助手 / 导航文案语言 / 无硬编码 zh-CN 日期 locale。

用法：
  python tools/workspace_i18n_smoke.py
  python tools/workspace_i18n_smoke.py --base-url http://127.0.0.1:18799 --token YOUR_TOKEN
  python tools/workspace_i18n_smoke.py --lang en   # 只验英文
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAGES = [
    ("/workspace", "inbox.page_title", {"zh": "聊天工作台", "en": "Chat Workspace"}),
    ("/workspace/dash", "dash.page_title", {"zh": "工作台概览", "en": "Workspace Overview"}),
    ("/workspace/drafts", "draft.page_title", {"zh": "草稿审批工作台", "en": "Draft Review"}),
]

NAV_MARKERS = {
    "zh": ("客户", "概览"),
    "en": ("Contacts", "Overview"),
}

# ③-S2：抽查「已扫日期」的管理后台页（继承 base.html；/rpa-overview 还含共享 _rpa_shared_scripts）。
# /admin/ops（ops_overview，③-S2b 起 Jinja + 共享 partial）一并纳入。仅取 page_auth-only、非角色门控稳定页。
SWEPT_ADMIN_PAGES = ["/cases", "/rpa-overview", "/admin/ops"]

LEGACY_DATE_MARKERS = (
    "toLocaleString('zh-CN'", 'toLocaleString("zh-CN"',
    "toLocaleDateString('zh-CN'", 'toLocaleDateString("zh-CN"',
    "toLocaleTimeString('zh-CN'", 'toLocaleTimeString("zh-CN"',
    "toTimeString().slice",
)

ALLOWED_ZH_CN = (
    "<html",
    "WS_LOCALE",
    "wsDateLocale",
)


def _load_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        import yaml
        cfg_path = ROOT / "config" / "config.yaml"
        if cfg_path.is_file():
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            tok = (raw.get("web_admin") or {}).get("auth_token")
            if tok:
                return str(tok)
    except Exception:
        pass
    return ""


def _client(base: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "workspace-i18n-smoke/1.0")]
    return opener, base.rstrip("/")


def _get(opener, base: str, path: str, *, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(base + path)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
        return ex.code, body


def _login(opener, base: str, token: str) -> bool:
    code, _ = _get(opener, base, "/login")
    if code != 200:
        print(f"[login] GET /login → {code}")
        return False
    data = urllib.parse.urlencode({"auth_token": token}).encode()
    req = urllib.request.Request(
        base + "/login", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(req, timeout=20) as resp:
            final = resp.geturl()
            if "/login" in final and resp.status == 200:
                # 仍停在 login 页 → token 不对
                print("[login] POST /login 后仍在 /login —— 检查 auth_token")
                return False
            print(f"[login] OK (status={resp.status})")
            return True
    except urllib.error.HTTPError as ex:
        print(f"[login] POST /login → HTTP {ex.code}")
        return False


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return m.group(1).strip() if m else ""


def _html_lang(html: str) -> str:
    m = re.search(r'<html[^>]*\blang="([^"]*)"', html)
    return m.group(1) if m else ""


def _ws_locale(html: str) -> str:
    m = re.search(r'window\.WS_LOCALE\s*=\s*"([^"]+)"', html)
    return m.group(1) if m else ""


def _forbidden_zh_cn_lines(html: str) -> list[str]:
    bad = []
    for i, line in enumerate(html.splitlines(), 1):
        if "'zh-CN'" not in line and '"zh-CN"' not in line:
            continue
        if any(a in line for a in ALLOWED_ZH_CN):
            continue
        bad.append(f"L{i}: {line.strip()[:100]}")
    return bad


def _check_page(base: str, path: str, title_key: str, titles: dict, lang: str, html: str) -> list[str]:
    errs: list[str] = []
    label = f"{path}?lang={lang}"
    expect_lang = "zh-CN" if lang == "zh" else "en"
    expect_locale = "zh-CN" if lang == "zh" else "en-US"
    expect_title = titles[lang]

    hl = _html_lang(html)
    if hl != expect_lang:
        errs.append(f"{label}: <html lang>={hl!r} want {expect_lang!r}")

    loc = _ws_locale(html)
    if loc != expect_locale:
        errs.append(f"{label}: WS_LOCALE={loc!r} want {expect_locale!r}")

    title = _title(html)
    if not title.startswith(expect_title):
        errs.append(f"{label}: title={title!r} want prefix {expect_title!r}")

    for fn in ("window.wsFmtDate", "window.wsFmtTime", "window.wsFmtDateTime", "window.wsDateLocale"):
        if fn not in html:
            errs.append(f"{label}: missing {fn}")

    if "window.wsFmtTime(ts" not in html and path == "/workspace":
        # 收件箱消息/列表时间应走 wsFmt*（③-Q 硬化）
        if "toLocaleString('zh-CN'" in html or "toTimeString().slice" in html:
            errs.append(f"{label}: inbox still has legacy date formatters")

    nav_a, nav_b = NAV_MARKERS[lang]
    if nav_a not in html or nav_b not in html:
        errs.append(f"{label}: nav markers {nav_a!r}/{nav_b!r} not in HTML (i18n apply?)")

    bad = _forbidden_zh_cn_lines(html)
    if bad:
        errs.append(f"{label}: hardcoded zh-CN in JS ({len(bad)} line(s)) e.g. {bad[0]}")

    return errs


def _check_swept_admin_page(path: str, lang: str, code: int, html: str) -> list[str]:
    """③-S2：管理后台页运行期校验——能渲染(200) + wsFmt* 助手到位 + 无 legacy 日期写法 + lang 随语言。"""
    label = f"{path}?lang={lang}"
    if code != 200:
        return [f"{label}: HTTP {code}"]
    errs: list[str] = []
    for fn in ("window.wsFmtDate", "window.wsFmtTime", "window.wsFmtDateTime"):
        if fn not in html:
            errs.append(f"{label}: missing {fn}（bootstrap 未接上？）")
    for m in LEGACY_DATE_MARKERS:
        if m in html:
            errs.append(f"{label}: 残留 legacy 日期写法 {m!r}")
    if re.search(r"new Date\([^\n]*?\)\.toLocale", html):
        errs.append(f"{label}: 仍有内联 new Date().toLocale*（应走 wsFmt*）")
    expect_lang = "zh-CN" if lang == "zh" else "en"
    if _html_lang(html) != expect_lang:
        errs.append(f"{label}: <html lang>={_html_lang(html)!r} want {expect_lang!r}")
    return errs


def run(base: str, token: str, langs: list[str]) -> int:
    opener, base = _client(base)
    if not token:
        print("[err] 无 auth_token：传 --token 或 config/config.yaml::web_admin.auth_token")
        return 2
    if not _login(opener, base, token):
        return 2

    all_errs: list[str] = []
    for lang in langs:
        print(f"\n── lang={lang} ──")
        for path, _key, titles in PAGES:
            url = f"{path}?lang={lang}"
            code, html = _get(opener, base, url)
            print(f"  GET {url} → {code}")
            if code != 200:
                all_errs.append(f"{url}: HTTP {code}")
                continue
            all_errs.extend(_check_page(base, path, _key, titles, lang, html))

    # 管理后台外壳（base.html，③-S 共享 bootstrap partial）：/help 随 ?lang= 本地化
    for lang in langs:
        expect_lang = "zh-CN" if lang == "zh" else "en"
        expect_locale = "zh-CN" if lang == "zh" else "en-US"
        code, html = _get(opener, base, f"/help?lang={lang}")
        print(f"\n  GET /help?lang={lang} → {code} (admin shell)")
        if code != 200:
            all_errs.append(f"/help?lang={lang}: HTTP {code}")
            continue
        if _html_lang(html) != expect_lang:
            all_errs.append(f"/help?lang={lang}: <html lang>={_html_lang(html)!r} want {expect_lang!r}")
        if _ws_locale(html) != expect_locale:
            all_errs.append(f"/help?lang={lang}: WS_LOCALE={_ws_locale(html)!r} want {expect_locale!r}")
        for fn in ("window.wsFmtDate", "window.wsFmtDateTime", "window.T "):
            if fn not in html:
                all_errs.append(f"/help?lang={lang}: missing {fn}")

    # ③-S2：已扫日期的管理后台页（base.html + 共享 RPA 脚本）运行期抽查
    for lang in langs:
        for path in SWEPT_ADMIN_PAGES:
            url = f"{path}?lang={lang}"
            code, html = _get(opener, base, url)
            print(f"  GET {url} → {code} (swept admin page)")
            all_errs.extend(_check_swept_admin_page(path, lang, code, html))

    # /set_lang 重定向应带 lang cookie（下一请求无 ?lang= 仍 en）
    if "en" in langs:
        code, _ = _get(opener, base, "/set_lang?lang=en")
        print(f"\n  GET /set_lang?lang=en → {code}")
        code2, html2 = _get(opener, base, "/workspace")
        print(f"  GET /workspace (cookie) → {code2}")
        if code2 == 200:
            if _ws_locale(html2) != "en-US":
                all_errs.append(f"/workspace after set_lang: WS_LOCALE={_ws_locale(html2)!r} want en-US")
            if not _title(html2).startswith("Chat Workspace"):
                all_errs.append(f"/workspace after set_lang: title={_title(html2)!r}")
        else:
            all_errs.append(f"/workspace after set_lang: HTTP {code2}")

    print("\n" + ("=" * 48))
    if all_errs:
        print(f"FAIL — {len(all_errs)} issue(s):")
        for e in all_errs:
            print("  ·", e)
        return 1
    print("OK — workspace i18n smoke passed (title / lang / locale / wsFmt* / nav)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Workspace i18n live smoke")
    ap.add_argument("--base-url", default="http://127.0.0.1:18799")
    ap.add_argument("--token", default="", help="web_admin.auth_token（缺省读 config.yaml）")
    ap.add_argument("--lang", choices=("zh", "en", "both"), default="both")
    args = ap.parse_args()
    langs = ["zh", "en"] if args.lang == "both" else [args.lang]
    return run(args.base_url, _load_token(args.token or None), langs)


if __name__ == "__main__":
    sys.exit(main())
