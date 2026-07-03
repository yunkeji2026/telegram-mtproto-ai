"""全站模板「动态点属性拼接」门禁——闭合又一类跨页静默 bug。

背景：`_rpa_shared_scripts.html::initSearch` 曾按 `inputId` 拼一个全局函数名再在生成的 inline
onclick 里用**点访问**它：`'(window.__rpaPick_'+opts.inputId+')(...)' `。当 inputId 含连字符
（`wa-ops-hist-q`/`lr-hist-q`/`mr-hist-q`——三个 RPA 调用方全是）时，浏览器把
`window.__rpaPick_wa-ops-hist-q` 解析成**减法**（`... - ops - hist - q`）→ `ReferenceError`，
搜索结果点击开抽屉在三页全坏（还触发 dead-click 红条兜底）。已改事件委托 + `data-rpa-ck`。

**窄不变量**：JS 里不得在**字符串字面量内**用「点访问 + 拼接扩展标识符」——即
`X.<ident>'+`（点后跟标识符、紧接字符串收尾再 `+`）。这个形状只可能出现在**把代码当字符串拼**
的场景（普通手写成员访问 `a.b` 不会紧跟 `'+`），因此几乎必然是「按变量拼点属性名」的 code-gen 陷阱。
正解：改 `obj[key]` 方括号访问，或彻底改事件委托 + `data-*` 属性（见 initSearch）。

`_ALLOWLIST` 记录极少数良性命中（如字面量恰以 `.ext` 结尾再拼变量的文件名串）；当前为空。
"""
import re
from pathlib import Path

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))

# 词/美元符 + 点 + 字母/下划线起头的标识符 + 收尾引号 + 拼接号。
# 前置 [\w$] 排除「以点开头的字符串」（如 CSS 选择器串 '.foo'+x，非成员访问）；
# 点后要求字母/下划线（排除版本串 '1.0'+patch）。
_DYN_DOT = re.compile(r"""[\w$]\.[A-Za-z_$][\w$]*['"]\s*\+""")

# 良性命中允许清单：{文件名: {命中片段, ...}}。命中片段取 _DYN_DOT.group(0)。当前为空。
_ALLOWLIST: dict[str, set[str]] = {}


def _violations(html: str) -> set:
    return {m.group(0) for m in _DYN_DOT.finditer(html)}


def test_no_dynamic_dot_access_concat():
    failures = {}
    for f in _ALL:
        hits = _violations(f.read_text(encoding="utf-8")) - _ALLOWLIST.get(f.name, set())
        if hits:
            failures[f.name] = sorted(hits)
    assert not failures, (
        "有模板在字符串里用「点访问 + 拼接扩展标识符」（`X.name'+var`）——若 var 含连字符/非法标识符字符，"
        "生成的代码会被当减法/语法错解析（如 `window.__rpaPick_wa-ops-hist-q`）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：改 `obj[key]` 方括号访问，或改事件委托 + `data-*` 属性（参考 initSearch）。"
    )


def test_allowlist_not_stale():
    """防 _ALLOWLIST 过期：命中已消失（代码已改）则提示回收，恢复门禁强度。"""
    stale = {}
    for name, frags in _ALLOWLIST.items():
        f = _TPL_DIR / name
        if not f.exists():
            continue
        gone = sorted(set(frags) - _violations(f.read_text(encoding="utf-8")))
        if gone:
            stale[name] = gone
    assert not stale, (
        "以下允许清单命中已消失（代码已改），请从 _ALLOWLIST 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_scanner_self_check():
    # 抓：window.__rpaPick_'+id（连字符陷阱原形）
    assert _violations("x='(window.__rpaPick_'+opts.inputId+')(y)';")
    # 抓：任意 obj.prop'+var
    assert _violations("s='a.foo'+bar;")
    # 不抓：以点开头的串（选择器）
    assert not _violations("s='.foo'+bar;")
    # 不抓：版本串（点后是数字）
    assert not _violations("s='1.0'+patch;")
    # 不抓：普通手写成员访问（点后无引号+拼接）
    assert not _violations("const v=window.location.href + '?x=1';")
    # 不抓：方括号动态访问（正解）
    assert not _violations("window['__rpaPick_'+id](ck);")
