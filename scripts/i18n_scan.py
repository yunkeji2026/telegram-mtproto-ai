"""坐席工作台 i18n 覆盖扫描器。

扫描工作台模板里**实际用到**的 i18n key —— HTML 的 ``data-i18n="..."`` 与
JS 的 ``T('...')`` —— 校验它们在 zh / en 两套字典里都存在。缺失会让前端静默
回落成 key 字符串（半成品翻译上线，且没人报错），这正是本门禁要堵的洞。

另给一个**粗略覆盖度**参考（模板里仍硬编码的连续中文文本量），仅作信息，不作硬门禁
（一次性把上千条中文全标完不现实，靠这个数看「还剩多少没收口」即可）。

用法::

    python -m scripts.i18n_scan          # 人读报告；有缺失翻译则退出码 1
    python -m scripts.i18n_scan --json   # 机读 JSON

门禁：``tests/test_i18n_coverage.py`` 调 :func:`scan_workspace_i18n` 断言 0 缺失。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 仓库根（scripts/ 的上一级）
_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"

# 扫描范围：坐席工作台外壳 + 继承它的高频页（都已接 WS_I18N / T() / data-i18n 地基）。
# 新增工作台页面接入 i18n 后，把模板名加进来即纳入门禁。
WORKSPACE_TEMPLATES = [
    "workspace_base.html",
    "workspace_dashboard.html",
    "unified_inbox.html",
    "draft_review.html",
    # ③-S9i：坐席绩效看板（首个收口的工作台内容页）。静态层走服务端 (i18n or {}).get()（直出
    # 当前语言、无「先中文后 JS 换」闪烁、免 JS 亦可读），JS 层走 window.T/Tf——与外壳同读一份
    # i18n/WS_I18N，切换语言经 /set_lang 整页重载后由服务端重渲，两条路径都产出目标语言。
    "agent_perf.html",
    # ③-S9v：工作台经营看板（extends workspace_base.html，已 cap=0 + SEALED_PAGES）。
    "workspace_roi.html",
    "workspace_usage.html",
]

# 「外壳页」：``{% extends "base.html" %}`` 或 ``workspace_base.html`` 的页面——都经
# _i18n_bootstrap.html 拿到 window.wsFmt*（日期本地化）+ window.T/Tf（文案）。
# ③-S2 日期扫荡后这些页只走 wsFmt*，不得再硬编码 toLocale*('zh-CN')。用「真的 extends 了
# 外壳」而非手列白名单：新页只要继承外壳就自动进日期门禁，无需改测试；standalone 自带
# <html> 的 ops 页（无 wsFmt*）不会被命中，由各自门禁单列。
_RE_EXTENDS_SHELL = re.compile(
    r"""\{%\s*extends\s+["'](?:base\.html|workspace_base\.html)["']"""
)


def shelled_templates() -> list[str]:
    """templates/ 下所有继承 base.html / workspace_base.html 的页面（相对 posix 名，含 ops/ 子目录）。"""
    out: list[str] = []
    for p in sorted(_TPL_DIR.rglob("*.html")):
        try:
            head = p.read_text(encoding="utf-8")[:400]
        except OSError:
            continue
        if _RE_EXTENDS_SHELL.search(head):
            out.append(p.relative_to(_TPL_DIR).as_posix())
    return out

# data-i18n="key" / data-i18n='key'
_RE_DATA_I18N = re.compile(r"""data-i18n\s*=\s*["']([^"']+)["']""")
# data-i18n-placeholder="key" / data-i18n-title="key"（属性翻译：输入框占位符 / 悬浮提示）
_RE_DATA_I18N_ATTR = re.compile(r"""data-i18n-(?:placeholder|title)\s*=\s*["']([^"']+)["']""")
# T('key') / Tf('key',{..}) / window.T('key', ...) —— 仅当首参是字面量字符串
# Tf? 同时覆盖插值版 Tf()，否则只经 Tf() 用到的键会逃过门禁（静默漏翻）。
_RE_T_CALL = re.compile(r"""\bTf?\(\s*["']([^"']+)["']""")
# Jinja 服务端 i18n：i18n.get('key', '回落') / i18n['key'] / (i18n or {}).get('key', '回落')
# —— <title>/<head> 等「JS 跑前就需正确语言」的场景走服务端渲染（首屏零闪烁），与 data-i18n/T()
# 同为 i18n 锚点。``(?:\s+or\s+\{\}\))?`` 兼容 admin 页 base.html/cases.html 的防御式
# ``(i18n or {}).get(...)`` 写法（否则其回落中文被误计为 untagged 裸串、键也漏抽，③-S5 实测踩坑）。
# 大小写敏感：不会误命中客户端 window.WS_I18N[k]（大写 I18N）或 i18n|tojson（整包注入）。
_RE_I18N_JINJA = re.compile(
    r"""i18n(?:\s+or\s+\{\}\))?(?:\.get\(\s*|\[\s*)["']([^"']+)["']"""
)
# i18n key 形态：小写字母起头，点/下划线分段（dash.card.due_mine / lang_toggle）。
# 用来过滤掉 T(中文文案) / T(变量) 之类的误命中。
_RE_KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
# 连续中日韩统一表意文字（粗略覆盖度参考）
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _strip_comments(text: str) -> str:
    """剥离 HTML(``<!-- -->``)、Jinja(``{# #}``)、JS/CSS 块(``/* */``) 与行(``//``) 注释
    ——量化 untagged 时排除注释里的中文，使数字真实反映「用户可见、未接 i18n」的残余。

    用正则删除而非字符级 token 化：JS 正则字面量里的引号（如 ``.replace(/'/g, …)``）会让
    朴素 token 器误判字符串起止、吞掉大段注释致计数失真，正则删除更稳。行注释 ``//`` 仅在
    **前置为空白**时删——真注释一定有缩进/空格在前；而 ``https://`` / ``"//"`` 前置非空白，
    不会误伤。取舍：漏删=虚高（可接受方向性偏差），误删字符串内中文=漏计（更糟），故从严。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # 行注释 ``//``：前置为空白**或 ``;``**时删——``stmt();// 中文`` 这类语句尾注释
    # 也是真注释（曾让 untagged 虚高）；``https://`` 前置是 ``:`` 不在集合内，不误伤。
    text = re.sub(r"(?<=[\s;])//[^\n]*", "", text)
    return text


def _count_untagged_cjk(text: str) -> int:
    """统计「裸中文」CJK runs：**剥离注释后**，仍含中文、但该行既无 ``data-i18n`` 锚点、
    也无 ``T()/Tf()`` 调用的行。信息级覆盖度参考（lower=更本地化），**非硬门禁**——
    把「凭感觉还剩多少用户可见中文没接 i18n」变成可下降、可信的数字。

    剥离注释（字符串感知）后，trailing ``// 中文``、内联/多行 ``/* */``、``<!-- -->``、
    Jinja ``{# #}`` 里的中文不再虚高计数；只剩真正「用户可见、未接 i18n」的字符串字面量。
    粗略之处（可接受，方向性指标）：同一行若同时有 T() 调用与另一段硬编码中文，会整行跳过而少计。
    """
    n = 0
    # 跨行开标签状态机：属性多到换行的开标签（``<button …\n  data-i18n=…\n  …>文本``），其文本
    # 是该 i18n 元素的内联回落文案，不算「未接 i18n」。``tag_i18n`` 只在真正出现 ``data-i18n``
    # （纯 HTML 上下文）时置位，故 JS 里的 ``a<b`` / ``=>`` 即便置 ``in_open_tag`` 也永不遮蔽
    # 真实裸串（避免漏计）。
    in_open_tag = False   # 处于一个跨行未闭合的开标签内
    tag_i18n = False      # 该未闭合开标签（含其续行）是否出现过 data-i18n
    for line in _strip_comments(text).splitlines():
        has_cjk = bool(_RE_CJK_RUN.search(line))
        inline_tagged = (
            "data-i18n" in line
            or _RE_T_CALL.search(line)
            or _RE_I18N_JINJA.search(line)
        )
        covered = in_open_tag and tag_i18n  # 本行中文被续行中的 i18n 开标签覆盖
        if has_cjk and not inline_tagged and not covered:
            n += len(_RE_CJK_RUN.findall(line))
        # —— 维护跨行开标签状态 ——
        lt, gt = line.rfind("<"), line.rfind(">")
        opens = lt > gt  # 行尾停在一个未闭合的 '<...' 里
        if in_open_tag and not (gt >= 0 and not opens):
            tag_i18n = tag_i18n or ("data-i18n" in line)  # 续行：累计 data-i18n
            in_open_tag = True
        elif opens:
            in_open_tag = True  # 新开一个跨行标签
            tag_i18n = "data-i18n" in line
        else:
            in_open_tag = False
            tag_i18n = False
    return n


# ── 后端路由响应文案 CJK 扫描（P37：API detail/error 请求级本地化的防回潮账本）──
# 前端大量 ``d.detail || window.T(fallback)`` / ``d.error || …`` 会 verbatim 直显后端
# 返回的 detail/error；后端硬编码中文 → EN 用户报错时仍看中文。把「每个路由文件还剩多少条
# 硬编码中文响应文案」量化成可下降的数字，配合 per-file 天花板做棘轮门禁（只减不增）。
_ROUTES_DIR = _ROOT / "src" / "web" / "routes"
_RE_ROUTE_CJK_PROSE = [
    # HTTPException(..., "中文") / HTTPException(400, detail="中文")
    re.compile(r"""HTTPException\([^)]*['"][^'"]*[\u4e00-\u9fff]"""),
    # detail="中文" / detail=f"中文…"
    re.compile(r"""detail\s*=\s*f?['"][^'"]*[\u4e00-\u9fff]"""),
    # 返回 dict 的 "detail"/"error" 值含中文字面量
    re.compile(r"""['"](?:detail|error)['"]\s*:\s*f?['"][^'"]*[\u4e00-\u9fff]"""),
]


def iter_route_cjk_prose(text: str):
    """产出 ``(lineno, stripped_line)`` —— 路由源码里硬编码中文的**响应文案**
    （``HTTPException`` detail / 返回 dict 的 ``detail``/``error`` 值）。纯注释行(``#`` 起头)
    跳过，避免文档字符串/注释里的中文虚高。仅命中「用户可见、前端会直显」的响应 prose。"""
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if any(p.search(line) for p in _RE_ROUTE_CJK_PROSE):
            yield i, line.strip()


def count_route_cjk_prose(text: str) -> int:
    """路由源码里硬编码中文响应文案的行数（见 :func:`iter_route_cjk_prose`）。"""
    return sum(1 for _ in iter_route_cjk_prose(text))


def scan_routes_response_cjk() -> dict[str, int]:
    """扫描 ``src/web/routes/*.py``，返回 ``{文件名: 硬编码中文响应文案行数}``（仅非零项）。"""
    out: dict[str, int] = {}
    if not _ROUTES_DIR.exists():
        return out
    for p in sorted(_ROUTES_DIR.glob("*.py")):
        n = count_route_cjk_prose(p.read_text(encoding="utf-8"))
        if n:
            out[p.name] = n
    return out


# window.T( / window.Tf( 首参手扫器——供「裸键解析」门禁用。
# 比朴素正则更正确：正确处理跨行调用、字面量里的转义引号（\' \"），并把模板字面量(``)
# 与拼接键(`'p.'+code`)判为**动态**（运行时才成形，静态无法校验→跳过）。
# 与门禁 test_template_window_t_keys_resolve 同源：门禁扫「静态键必在译表」，此函数是其唯一取键口径。
_RE_WINDOW_T_OPEN = re.compile(r"window\.Tf?\(")


def iter_window_t_calls(text: str):
    """产出 ``(key, is_dynamic)``：扫描每个 ``window.T(`` / ``window.Tf(`` 调用的首参。

    - 首参非字符串字面量（变量/表达式）→ 跳过（本就动态，无静态键可校验）。
    - 首参是 ``'..'`` / ``".."`` 且其后紧跟 ``+``（跨空白/换行）→ ``is_dynamic=True``（拼接前缀键）。
    - 首参是模板字面量 `` `..` `` → ``is_dynamic=True``（可能内插）。
    - 其余 → ``is_dynamic=False``（静态键，须在译表存在）。
    """
    n = len(text)
    for m in _RE_WINDOW_T_OPEN.finditer(text):
        i = m.end()
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            continue
        q = text[i]
        if q not in "'\"`":
            continue  # 首参非字面量 → 动态，无静态键
        j = i + 1
        buf: list[str] = []
        while j < n:
            c = text[j]
            if c == "\\" and j + 1 < n:      # 转义：吞下被转义字符，不当作收尾引号
                buf.append(text[j + 1]); j += 2; continue
            if c == q:
                break
            buf.append(c); j += 1
        key = "".join(buf)
        k = j + 1
        while k < n and text[k] in " \t\r\n":
            k += 1
        nxt = text[k] if k < n else ""
        yield key, (q == "`" or nxt == "+")


def window_t_static_keys(text: str) -> set[str]:
    """``window.T/Tf`` 调用里的**静态**首参键集合（动态拼接/模板字面量已排除）。"""
    return {k for k, dyn in iter_window_t_calls(text) if not dyn}


def _iter_used_keys(text: str):
    """产出模板里用到的所有 i18n key（data-i18n 锚点 + JS 的 T() 调用）。"""
    for m in _RE_DATA_I18N.finditer(text):
        yield m.group(1)
    for m in _RE_DATA_I18N_ATTR.finditer(text):
        yield m.group(1)
    for m in _RE_T_CALL.finditer(text):
        k = m.group(1)
        if _RE_KEY_SHAPE.match(k):
            yield k
    for m in _RE_I18N_JINJA.finditer(text):
        k = m.group(1)
        if _RE_KEY_SHAPE.match(k):
            yield k


def scan_workspace_i18n(templates: list[str] | None = None) -> dict:
    """扫描并校验工作台 i18n key 覆盖。返回可机读的报告 dict。"""
    from src.web.web_i18n import get_translations

    zh = get_translations("zh")
    en = get_translations("en")
    tpls = templates or WORKSPACE_TEMPLATES

    used: dict[str, list[str]] = {}  # key -> 出现的模板列表
    cjk_runs_total = 0
    untagged_cjk_total = 0
    per_template: dict[str, dict] = {}
    for name in tpls:
        p = _TPL_DIR / name
        if not p.exists():
            per_template[name] = {"exists": False}
            continue
        txt = p.read_text(encoding="utf-8")
        keys = sorted(set(_iter_used_keys(txt)))
        for k in keys:
            used.setdefault(k, []).append(name)
        runs = len(_RE_CJK_RUN.findall(txt))
        untagged = _count_untagged_cjk(txt)
        cjk_runs_total += runs
        untagged_cjk_total += untagged
        per_template[name] = {
            "exists": True,
            "keys": len(keys),
            "cjk_runs": runs,
            "untagged_cjk": untagged,
        }

    missing_zh = sorted(k for k in used if k not in zh)
    missing_en = sorted(k for k in used if k not in en)
    return {
        "used_keys": sorted(used),
        "used_count": len(used),
        "missing_zh": missing_zh,
        "missing_en": missing_en,
        "ok": not missing_zh and not missing_en,
        "cjk_runs_total": cjk_runs_total,
        "untagged_cjk_total": untagged_cjk_total,
        "per_template": per_template,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Workspace i18n coverage scan")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--routes", action="store_true",
                    help="改为扫描后端路由响应文案硬编码中文账本（P37 棘轮门禁数据源）")
    args = ap.parse_args(argv)

    if args.routes:
        led = scan_routes_response_cjk()
        if args.json:
            print(json.dumps(led, ensure_ascii=False, indent=2))
            return 0
        print(f"[i18n] routes with hardcoded CJK response prose: {len(led)} files, "
              f"{sum(led.values())} lines (lower=more localized)")
        for name, n in sorted(led.items(), key=lambda kv: -kv[1]):
            print(f"  - {name}: {n}")
        return 0

    rep = scan_workspace_i18n()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    print(f"[i18n] workspace templates scanned: {len(rep['per_template'])}")
    for name, info in rep["per_template"].items():
        if not info.get("exists"):
            print(f"  - {name}: (missing file, skipped)")
        else:
            print(
                f"  - {name}: {info['keys']} i18n keys used · {info['cjk_runs']} CJK runs"
                f" · {info['untagged_cjk']} untagged (bare CJK, no i18n hook)"
            )
    print(
        f"[i18n] distinct keys used: {rep['used_count']} · "
        f"hardcoded CJK runs (info): {rep['cjk_runs_total']} · "
        f"untagged bare CJK (info, lower=more localized): {rep['untagged_cjk_total']}"
    )
    if rep["missing_zh"]:
        print(f"[i18n] !! missing in zh ({len(rep['missing_zh'])}): {', '.join(rep['missing_zh'])}")
    if rep["missing_en"]:
        print(f"[i18n] !! missing in en ({len(rep['missing_en'])}): {', '.join(rep['missing_en'])}")
    if rep["ok"]:
        print("[i18n] OK — every used key is translated in both zh & en.")
        return 0
    print("[i18n] FAIL — fill the missing keys in src/web/web_i18n.py.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
