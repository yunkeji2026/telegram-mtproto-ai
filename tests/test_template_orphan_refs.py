"""全站模板「孤儿 DOM 引用」门禁——与哑按钮/重复 id 互补的第三条链路，闭合「引用↔定义」。

只守**高置信必崩**的窄不变量：JS 里 `getElementById('x').prop` / `$('x').prop`（结果**立即解引用**
成员/下标）——这写法意味作者笃定该元素存在，若 `id="x"` 全站（含 `<script>` 内 innerHTML 生成、
`el.id=`、`setAttribute('id',..)`、以及 base/`_*.html` partial 跨文件）都找不到 → 运行时 `null.prop` 必崩。

**刻意不管**防御式引用（`getElementById('x')?.`、`... || fallback`、`const p=..;if(!p)return`）——
那些故意容忍元素缺失，不崩，纳进来只会制造假阳性（这正是宽口径孤儿检测不可零假阳的原因）。

`_PENDING_ORPHAN_REFS` 记录当前在建/停用面板里的此类引用（markup 未落地，函数暂不可达=不崩），
CI 保绿+债务可见；`test_pending_orphan_refs_still_orphan` 防其过期（补上 markup 后强制回收）。
"""
import re
from pathlib import Path

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))

# id 定义（HTML + JS 串；允许 id=\"x\" 转义引号 / id='x'）
_ID_DEF = re.compile(r"""\bid\s*=\s*\\?["']([A-Za-z0-9_\-:]+)""")
_DOT_ID = re.compile(r"""\.id\s*=\s*\\?["']([A-Za-z0-9_\-:]+)""")          # el.id='x'
_SETATTR = re.compile(r"""setAttribute\(\s*\\?["']id\\?["']\s*,\s*\\?["']([A-Za-z0-9_\-:]+)""")
# 直接解引用：getElementById('x').  或  ('x')[ ；$ 版仅在 $ 是 getElementById 别名的文件里算
_GEBI_DEREF = re.compile(r"""getElementById\(\s*(['"])([A-Za-z0-9_\-:]+)\1\s*\)\s*[.\[]""")
_DOLLAR_DEREF = re.compile(r"""(?<![.\w$])\$\(\s*(['"])([A-Za-z0-9_\-:]+)\1\s*\)\s*[.\[]""")
_DOLLAR_IS_GEBI = re.compile(r"""\$\s*=\s*\(?\s*id\s*\)?\s*=>\s*document\.getElementById|\$\s*=\s*document\.getElementById""")

# 当前在建/停用面板：markup 未落地，触发它们的 UI 也不存在 → 函数不可达、暂不崩。
# 补上对应 markup（或移除死代码）后请从这里删除；test_pending_orphan_refs_still_orphan 会提醒。
# （P0-6/C8：unified_inbox 的「声纹自助登记」内联面板死代码已删除——markup 从未落地、
#   同能力由出货副驾组件 cp-voice.js 承接；其 ve-* 孤儿引用随之回收，门禁强度恢复。
#   personas 的 previewTTS 死代码更早已删。当前无 pending。）
_PENDING_ORPHAN_REFS = {}


def _defined_ids(html: str) -> set:
    ids = set(_ID_DEF.findall(html))
    ids |= set(_DOT_ID.findall(html))
    ids |= set(_SETATTR.findall(html))
    return ids


def _ambient_ids() -> set:
    amb = set()
    for f in _ALL:
        if f.name.startswith("_") or "base" in f.name:
            amb |= _defined_ids(f.read_text(encoding="utf-8"))
    return amb


_AMBIENT = _ambient_ids()


def _direct_deref_refs(html: str) -> set:
    refs = {m.group(2) for m in _GEBI_DEREF.finditer(html)}
    if _DOLLAR_IS_GEBI.search(html):
        refs |= {m.group(2) for m in _DOLLAR_DEREF.finditer(html)}
    return refs


def test_no_unexpected_orphan_direct_deref():
    failures = {}
    for f in _ALL:
        html = f.read_text(encoding="utf-8")
        defined = _defined_ids(html) | _AMBIENT
        orphan = sorted(_direct_deref_refs(html) - defined - _PENDING_ORPHAN_REFS.get(f.name, set()))
        if orphan:
            failures[f.name] = orphan
    assert not failures, (
        "有模板**直接解引用** getElementById/$ 的结果，但该 id 全站找不到定义（运行时 null.prop 必崩）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：补上对应 `id=` 元素 / 改正 id 拼写 / 或改防御式引用（`?.`、`||`、`if(el)`）。"
    )


def test_pending_orphan_refs_still_orphan():
    """防 _PENDING 过期：补上 markup 后该 id 不再孤儿，提示回收以恢复门禁强度。"""
    stale = {}
    for name, ids in _PENDING_ORPHAN_REFS.items():
        f = _TPL_DIR / name
        if not f.exists():
            continue
        html = f.read_text(encoding="utf-8")
        defined = _defined_ids(html) | _AMBIENT
        # 仍是孤儿 = 仍被直接解引用 且 仍无定义
        still = _direct_deref_refs(html) - defined
        fixed = sorted(set(ids) - still)
        if fixed:
            stale[name] = fixed
    assert not stale, (
        "以下 id 已不再是孤儿（markup 已补 / 引用已改），请从 _PENDING_ORPHAN_REFS 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_scanner_self_check():
    defined = _defined_ids('<div id="a"></div>x.innerHTML=\'<i id="b"></i>\';el.id="c";')
    assert {"a", "b", "c"} <= defined

    # 直接解引用才抓；防御式（?. / ||）不抓
    js = (
        "getElementById('crashy').textContent='x';"      # 抓
        "getElementById('safe1')?.classList.add('o');"    # 不抓（?.）
        "const p=getElementById('safe2')||{};"            # 不抓（||，且非直接 .）
    )
    refs = _direct_deref_refs(js)
    assert "crashy" in refs
    assert "safe1" not in refs and "safe2" not in refs
