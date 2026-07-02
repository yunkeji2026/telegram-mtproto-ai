"""unified_inbox.html 的「哑按钮」门禁（P5-4；2026-07 归一到共享作用域分析器）。

`unified_inbox.html` 整段脚本包在一个大 IIFE 内，内联 on*="fn(...)" 在全局作用域求值，
函数必须挂到 window（末尾 `Object.assign(window,{...})`）才可达；否则抛 ReferenceError =
静默哑按钮（历史上 setMode 正是如此：有定义却没暴露）。

扫描/作用域分析逻辑统一在 `tests/_inline_handler_scan.py`（与 RPA 页门禁共用单一事实来源）。

两层断言：
  层① 每个内联 handler 引用的函数都能找到定义（抓 拼写错/漏改名/删函数留孤儿 handler）。
  层② 每个内联 handler 引用的函数都**全局作用域可达**（抓「定义了没暴露」的 IIFE 内哑按钮）。
另含运行时兜底守卫存在性检查（防守卫被误删）。
"""
from pathlib import Path

from tests import _inline_handler_scan as scan

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html"


def _load() -> str:
    return _TPL.read_text(encoding="utf-8")


def test_layer1_inline_handlers_resolve_to_a_definition():
    html = _load()
    referenced = scan.referenced(html)
    known = scan.defined(html) | scan.BUILTINS | scan.SHARED_GLOBALS | scan.HELPERS
    missing = sorted(referenced - known)
    assert not missing, (
        "unified_inbox.html 有内联 handler 引用了脚本里**找不到定义**的函数"
        f"（哑按钮：拼写错/漏改名/删函数）。\n未定义：{missing}"
    )


def test_layer2_inline_handlers_globally_reachable():
    """层②：内联 handler 引用的函数必须挂到 window（抓 setMode 那类「定义了没暴露」）。"""
    html = _load()
    unreachable = scan.unreachable_inline_handlers(html)
    assert not unreachable, (
        "unified_inbox.html 有内联 handler 引用的函数**已定义但全局不可达**（IIFE 内没挂 window）。\n"
        "请把它们补进末尾 `Object.assign(window,{...})` 暴露块；\n"
        "若为生成期辅助登记到 _inline_handler_scan.HELPERS，外部脚本全局登记到 SHARED_GLOBALS。\n"
        f"不可达：{unreachable}"
    )


def test_deadclick_guard_present():
    """运行时兜底守卫必须在位（捕获 ReferenceError 弹红条），防被误删回到静默失败。"""
    html = _load()
    assert "_wireDeadClickGuard" in html
    assert "is not defined" in html
    assert "inbox.fallback.unavailable" in html


def test_no_dead_functions():
    """P5-5：反向哑按钮门禁——定义了却全文零引用的死函数（不可达代码）。

    与层①/② 互补：那两层抓「引用了没定义/没暴露」，本层抓「定义了没人用」。
    误报防护：具名 IIFE / 函数表达式自动排除；有意保留的 dormant 函数在定义行加
    `dead-code-allow` 豁免。命中即需删除或接线，防脚本随迭代堆积僵尸函数。
    """
    html = _load()
    dead = scan.dead_functions(html)
    assert not dead, (
        "unified_inbox.html 存在**定义了却从未被引用**的死函数（不可达代码，应删除或接线）：\n"
        f"  {dead}\n"
        "（若为有意保留的待接线/dormant 函数，在其定义行尾加 `dead-code-allow` 注释豁免）"
    )
