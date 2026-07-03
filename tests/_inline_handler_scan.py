"""内联 handler「哑按钮」门禁的共享扫描器（含作用域分析）。

被 test_inbox_inline_handlers_exported.py / test_rpa_inline_handlers_exposed.py 复用。

三类信息：
  referenced(html)        —— 内联 on*="fn(...)" 引用的函数名。
  window_exposed(html)    —— window.X= 赋值 + Object.assign(window,{...}) 暴露块（全局可达）。
  global_scope_names(html)—— 在某个 <script> 顶层作用域（depth 0，非任何 IIFE/函数内）声明的
                             function / const|let|var 名 —— 这些本就是全局属性，内联可达。

作用域分析用一个会**跳过字符串/模板字面量/注释/正则**的掩码器算括号嵌套深度，
从而可靠区分「IIFE 内定义（不可达，除非显式挂 window）」与「顶层全局定义（可达）」——
这正是朴素 brace 计数会被模板字符串击穿、原门禁当初回避的部分。
"""
import re

_KEYWORDS = {
    "if", "for", "while", "switch", "return", "catch", "function", "typeof",
    "new", "void", "do", "else", "delete", "in", "of", "await", "yield",
    "throw", "case", "instanceof", "var", "let", "const",
}
BUILTINS = {
    "event", "window", "document", "console", "Number", "String", "Boolean",
    "Array", "Object", "JSON", "Math", "parseInt", "parseFloat", "setTimeout",
    "setInterval", "clearTimeout", "clearInterval", "alert", "confirm", "prompt",
    "Date", "Promise", "fetch", "encodeURIComponent", "decodeURIComponent",
    "isNaN", "RegExp", "Set", "Map", "requestAnimationFrame", "URLSearchParams",
    "location", "navigator", "localStorage", "sessionStorage",
}
# 共享脚本提供的全局：rpa（_rpa_shared_scripts.html）、T（_i18n_bootstrap.html）。
SHARED_GLOBALS = {"rpa", "T"}
# 模板字符串里生成 HTML 的插值/拼接辅助（生成期即时求值，非内联 handler，无需暴露）。
HELPERS = {"esc", "escAttr", "fn"}

_HANDLER_ATTR = re.compile(r"""\son[a-z]+\s*=\s*"([^"]*)\"""", re.IGNORECASE)
_HANDLER_ATTR_SQ = re.compile(r"""\son[a-z]+\s*=\s*'([^']*)'""", re.IGNORECASE)
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_INTERP = re.compile(r"\$\{[^}]*\}")
# JS 字符串拼接段：内联 handler 若写在生成期模板串里（innerHTML+='<i onclick="fn(\''+helper()+'\')">'），
# 被 `'+ ... +'` / `"+ ... +"` 夹住的是**生成期即时求值**的表达式（结果被拼进字面量），
# 运行时 handler 并不调用它们（如 bodyId/esc 只算 id/转义串）。剥掉这些段只留真正运行时执行的调用。
_CONCAT_SQ = re.compile(r"'\s*\+.*?\+\s*'")
_CONCAT_DQ = re.compile(r'"\s*\+.*?\+\s*"')
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _strip_html_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def referenced(html: str) -> set:
    html = _strip_html_comments(html)
    # 去 JS 块注释里的属性样例
    html = re.sub(r"/\*.*?\*/", "", html, flags=re.DOTALL)
    names = set()
    for m in list(_HANDLER_ATTR.finditer(html)) + list(_HANDLER_ATTR_SQ.finditer(html)):
        body = _INTERP.sub("", m.group(1))
        body = _CONCAT_SQ.sub("", body)
        body = _CONCAT_DQ.sub("", body)
        for c in _CALL.finditer(body):
            fn = c.group(1)
            if fn in _KEYWORDS or fn in BUILTINS:
                continue
            names.add(fn)
    return names


def window_exposed(html: str) -> set:
    e = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", html))
    for blk in re.finditer(r"Object\.assign\(\s*window\s*,\s*\{(.*?)\}\s*\)", html, re.DOTALL):
        e |= set(re.findall(r"([A-Za-z_$][\w$]*)", re.sub(r"//[^\n]*", "", blk.group(1))))
    return e


def defined(html: str) -> set:
    """一切「可作为可调用名解析」的定义（不区分作用域；宁滥勿缺）。"""
    d = set()
    d |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", html))
    d |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", html))
    d |= set(re.findall(r"(?:^|[^.\w$])([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)", html))
    d |= window_exposed(html)
    return d


def _script_bodies(html: str) -> list:
    """内联 <script>（无 src）的 JS 正文列表。"""
    bodies = []
    for m in _SCRIPT.finditer(html):
        attrs, body = m.group(1), m.group(2)
        if re.search(r"\bsrc\s*=", attrs or "", re.IGNORECASE):
            continue
        bodies.append(body)
    return bodies


def _mask(js: str) -> str:
    """把字符串/模板字面量/注释/正则替换为等长空白，保证括号计数只看真实代码。"""
    out = []
    i, n = 0, len(js)
    prev = ""  # 上一个非空白有效字符（正则位置判定用）
    while i < n:
        c = js[i]
        two = js[i:i + 2]
        if two == "//":
            j = js.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j; continue
        if two == "/*":
            j = js.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j; continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2; continue
                if js[j] == c:
                    j += 1; break
                j += 1
            out.append(" " * (j - i)); i = j; prev = "x"; continue
        if c == "`":
            j = i + 1
            tdepth = 0
            while j < n:
                if js[j] == "\\":
                    j += 2; continue
                if tdepth == 0 and js[j] == "`":
                    j += 1; break
                if js[j] == "$" and j + 1 < n and js[j + 1] == "{":
                    tdepth += 1; j += 2; continue
                if tdepth > 0 and js[j] == "}":
                    tdepth -= 1; j += 1; continue
                j += 1
            out.append(" " * (j - i)); i = j; prev = "x"; continue
        if c == "/" and prev in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?",
                                  "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^"):
            j = i + 1
            inclass = False; ok = False
            while j < n:
                ch = js[j]
                if ch == "\\":
                    j += 2; continue
                if ch == "[":
                    inclass = True
                elif ch == "]":
                    inclass = False
                elif ch == "/" and not inclass:
                    j += 1; ok = True; break
                elif ch == "\n":
                    break
                j += 1
            if ok:
                out.append(" " * (j - i)); i = j; prev = "x"; continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def _global_names_in_block(js: str) -> set:
    """某个 <script> 正文里，在顶层作用域（括号深度 0）声明的可调用名。"""
    masked = _mask(js)
    # 前缀深度：depth[k] = 第 k 个字符之前的括号嵌套深度
    depth = [0] * (len(masked) + 1)
    d = 0
    for idx, ch in enumerate(masked):
        depth[idx] = d
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d = d - 1 if d > 0 else 0
    depth[len(masked)] = d

    names = set()
    # function NAME —— 仅当处于 depth 0 且非「表达式位（前跟 = ( , : return 等）」
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", masked):
        s = m.start()
        if depth[s] != 0:
            continue
        # 往前找最近的非空白字符，排除具名函数表达式 / IIFE：`= function f`、`(function f`
        k = s - 1
        while k >= 0 and masked[k].isspace():
            k -= 1
        if k >= 0 and masked[k] in "=(,:?&|":
            continue
        names.add(m.group(1))
    # const|let|var NAME  在 depth 0
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", masked):
        if depth[m.start()] == 0:
            names.add(m.group(1))
    return names


def global_scope_names(html: str) -> set:
    names = set()
    for body in _script_bodies(html):
        names |= _global_names_in_block(body)
    return names


# ── 死代码（定义了却零引用的函数）检测 ──────────────────────────────────
#
# 与「哑按钮」门禁互为反向：那个抓「引用了没定义」，这个抓「定义了没人引用」。
# 引用计数刻意在**原始文本**上做（不 mask 字符串/模板）——因为大量函数只在生成期
# 模板字符串里的 onclick="fn()" 被调用，mask 掉字符串会把它们误判成死。
_FN_DEF_PATTERNS = [
    r"function\s+([A-Za-z_$][\w$]*)\s*\(",
    r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^;=]*=>",
    r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function",
    r"\blet\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^;=]*=>",
    r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?[^;=]*=>",
]


def dead_functions(html: str) -> list:
    """定义了但全文从未被引用（不可达死代码）的函数名。

    排除两类**合法的「仅现一次」**：
      - 具名 IIFE / 具名函数表达式（`(function NAME(){})()`、`= function NAME(){}`）——
        名字仅供堆栈跟踪，本就不会被再引用。
      - 定义所在行带 `dead-code-allow` 标记（有意保留的待接线/dormant 函数）。
    """
    dead, seen = [], set()
    for pat in _FN_DEF_PATTERNS:
        for m in re.finditer(pat, html):
            name, start = m.group(1), m.start()
            if name in seen:
                continue
            # 具名 IIFE / 函数表达式：`function NAME` 前一个非空白字符属表达式位
            if pat.startswith("function"):
                k = start - 1
                while k >= 0 and html[k].isspace():
                    k -= 1
                if k >= 0 and html[k] in "=(,:?&|":
                    seen.add(name)
                    continue
            # 定义所在行 dead-code-allow 豁免
            ls = html.rfind("\n", 0, start) + 1
            le = html.find("\n", start)
            le = len(html) if le < 0 else le
            if "dead-code-allow" in html[ls:le]:
                seen.add(name)
                continue
            cnt = len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", html))
            if cnt <= 1:
                dead.append(name)
            seen.add(name)
    return sorted(dead)


def ambient_globals(tpl_dir) -> set:
    """base/workspace_base + 所有 `_*.html` partial 里的顶层全局 + window.X=。

    子模板 `{% extends %}` / `{% include %}` 后，其内联 handler 可能调用这些跨文件全局；
    逐文件扫描看不到 → 汇入 ambient 防误报（宁可漏判个别真 bug，不制造假阳性）。
    """
    from pathlib import Path
    amb = set()
    for f in sorted(Path(tpl_dir).rglob("*.html")):
        if f.name.startswith("_") or "base" in f.name:
            h = f.read_text(encoding="utf-8")
            amb |= global_scope_names(h) | window_exposed(h)
    return amb


def unreachable_inline_handlers(html: str, extra_allow: set = frozenset()) -> list:
    """内联 handler 引用、但**全局作用域不可达**（既非 window 暴露、也非顶层全局声明）的函数名。

    这是「哑按钮」的权威判据，适用任何架构（单 IIFE / 全局 / 多块混合）。
    """
    ref = referenced(html)
    reachable = (window_exposed(html) | global_scope_names(html)
                 | BUILTINS | SHARED_GLOBALS | HELPERS | set(extra_allow))
    return sorted(ref - reachable)
