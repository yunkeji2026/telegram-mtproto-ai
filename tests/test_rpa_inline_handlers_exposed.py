"""全站模板「哑按钮」门禁（内联 on*="fn()" 引用的函数须①有定义 ②全局作用域可达）。

覆盖 `src/web/templates/**.html` 全部模板（unified_inbox 有独立同源门禁
tests/test_inbox_inline_handlers_exported.py，此处不重复断言其内容，但也纳入全量扫描无妨）。

各模板架构不一（顶层全局 / 单 IIFE / 多块混合），故用 `tests/_inline_handler_scan.py` 的
**作用域分析**（跳过字符串/模板/注释/正则的掩码器算括号深度）判定可达性，对任意架构零假阳性。
子模板 `{% extends %}`/`{% include %}` 的跨文件全局汇入 `ambient`（base + `_*.html` partial）防误报。

两层：层① 定义存在；层② 全局作用域可达。
另：`_PENDING_ORPHANS` 记录**已知但待产品决策**的真 bug（引用了根本不存在的函数）——
既不阻断 CI，又保留债务可见；并有 `test_pending_orphans_are_still_broken` 防该清单过期。
"""
from pathlib import Path

import pytest

from tests import _inline_handler_scan as scan

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))
_AMBIENT = scan.ambient_globals(_TPL_DIR)

# 已知真 bug（内联按钮引用了**未定义**的函数）待「实现功能 or 删按钮」决策。
# 修好后请从这里删除（test_pending_orphans_are_still_broken 会在它变可达时提醒）。
# 当前为空：personas 的 exportProfiles/importProfiles 哑按钮已删（真面板见 #import-panel）。
_PENDING_ORPHANS = {}


def _scan_targets():
    return [f for f in _ALL if scan.referenced(f.read_text(encoding="utf-8"))]


def test_layer1_all_templates_inline_handlers_defined():
    """层①：全模板——内联 handler 引用的函数都能找到定义。"""
    failures = {}
    for f in _scan_targets():
        html = f.read_text(encoding="utf-8")
        known = (scan.defined(html) | scan.BUILTINS | scan.SHARED_GLOBALS
                 | scan.HELPERS | _AMBIENT)
        missing = sorted(scan.referenced(html) - known - _PENDING_ORPHANS.get(f.name, set()))
        if missing:
            failures[f.name] = missing
    assert not failures, (
        "有模板内联 handler 引用了**找不到定义**的函数（哑按钮：拼写错/漏改名/删函数）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
    )


def test_layer2_all_templates_inline_handlers_reachable():
    """层②：全模板——内联 handler 引用的函数都**全局作用域可达**（window 暴露 或 顶层全局）。"""
    failures = {}
    for f in _scan_targets():
        html = f.read_text(encoding="utf-8")
        unreachable = sorted(set(scan.unreachable_inline_handlers(html, extra_allow=_AMBIENT))
                             - _PENDING_ORPHANS.get(f.name, set()))
        if unreachable:
            failures[f.name] = unreachable
    assert not failures, (
        "有模板内联 handler 引用的函数**全局不可达**（多为 IIFE 内裸 function 漏挂 window）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：补进该 IIFE 收尾前 `Object.assign(window,{...})`（或 window.X=）。"
    )


def test_pending_orphans_are_still_broken():
    """防 _PENDING_ORPHANS 过期：若某项已修（变可达/有定义），提示从清单移除。"""
    stale = {}
    for name, orphans in _PENDING_ORPHANS.items():
        f = _TPL_DIR / name
        if not f.exists():
            continue
        html = f.read_text(encoding="utf-8")
        still_unreachable = set(scan.unreachable_inline_handlers(html, extra_allow=_AMBIENT))
        fixed = sorted(orphans - still_unreachable)
        if fixed:
            stale[name] = fixed
    assert not stale, (
        "以下已不再是哑按钮（已修复），请从 _PENDING_ORPHANS 移除以恢复门禁强度：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_deadclick_guard_present_in_shared_scripts():
    """运行时兜底守卫必须在 _rpa_shared_scripts.html 里（一次覆盖 4 个 RPA 页），防被误删。"""
    shared = (_TPL_DIR / "_rpa_shared_scripts.html").read_text(encoding="utf-8")
    assert "is not defined" in shared
    assert "rpa.toast" in shared
    assert "addEventListener('error'" in shared


def test_scanner_self_check():
    """自测：抽取/暴露/作用域分析识别典型形态，防扫描器悄悄失效变假绿。"""
    sample = (
        '<span onclick="waRefreshAll()"></span>'
        '<input oninput="onSearch(this.value)">'
        '<i onclick="if(x){foo()}"></i>'
    )
    ref = scan.referenced(sample)
    assert {"waRefreshAll", "onSearch", "foo"} <= ref
    assert "if" not in ref and "value" not in ref

    # 生成期拼接：'+helper()+' 段是拼 HTML 时即时求值（结果被拼进字面量），运行时 handler 不调用它 →
    # 应只抓真正运行时执行的 tplSearch，不抓 bodyId/esc（否则会逼着无谓暴露生成期辅助）。
    gen = "x.innerHTML='<i oninput=\"tplSearch(this,\\''+bodyId(row)+'\\',\\''+esc(id)+'\\')\">'"
    gref = scan.referenced(gen)
    assert "tplSearch" in gref
    assert "bodyId" not in gref and "esc" not in gref

    html = (
        "<script>(function(){\n"
        "  function iifeLocal(){}\n"
        "  window.exposedA = function(){};\n"
        "  Object.assign(window, { exposedB });\n"
        "})();\n"
        "function topLevel(){}\n"
        "</script>"
    )
    g = scan.global_scope_names(html)
    assert "topLevel" in g and "iifeLocal" not in g
    assert {"exposedA", "exposedB"} <= scan.window_exposed(html)

    tricky = (
        "<script>(function(){\n"
        "  const s = '}{'; const t = `x${ {a:1} }y`; // }\n"
        "  function trapped(){}\n"
        "})();</script>"
    )
    assert "trapped" not in scan.global_scope_names(tricky)
