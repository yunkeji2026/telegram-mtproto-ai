"""web_i18n 热加载（2026-07 前端开发免重启闭环）。

真实源文件不动——用 monkeypatch 把 ``_SRC_PATH`` 指到临时文件验证：
- mtime 变化 → 新键生效（get_translations / t / tr 三入口同源）；
- 坏保存态（语法错 / 缺语言）→ 保留旧字典 + 不崩；修好后自动恢复；
- 节流窗口内不重复 stat（_next_check_ts 前移）。
"""

import time
from types import SimpleNamespace

import pytest

import src.web.web_i18n as W


@pytest.fixture(autouse=True)
def _preserve_i18n_state():
    """保存/恢复模块级状态，防测试替换的 mini 字典污染全局。"""
    saved = (W._TRANSLATIONS, W._SRC_PATH, W._loaded_mtime,
             W._next_check_ts, W._last_err_mtime)
    yield
    (W._TRANSLATIONS, W._SRC_PATH, W._loaded_mtime,
     W._next_check_ts, W._last_err_mtime) = saved


def _mini_src(zh_greet: str) -> str:
    return (
        "_TRANSLATIONS = {\n"
        f"  'zh': {{'greet': '{zh_greet}'}},\n"
        "  'en': {'greet': 'hello'},\n"
        "}\n"
    )


def _point_to(tmp_path, monkeypatch, body: str, *, loaded_mtime=1.0):
    f = tmp_path / "i18n_stub.py"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(W, "_SRC_PATH", f)
    monkeypatch.setattr(W, "_loaded_mtime", loaded_mtime)   # 非零 → 非启动装载态
    monkeypatch.setattr(W, "_next_check_ts", 0.0)           # 立即探测
    monkeypatch.setattr(W, "_last_err_mtime", -1.0)
    return f


def test_reload_picks_up_new_keys(tmp_path, monkeypatch):
    _point_to(tmp_path, monkeypatch, _mini_src("你好新键"))
    assert W.get_translations("zh").get("greet") == "你好新键"
    # t / tr 同源生效
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    assert W.t("greet", "zh") == "你好新键"
    req = SimpleNamespace(state=SimpleNamespace(ui_lang="en"))
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    assert W.tr(req, "greet") == "hello"


def test_bad_save_keeps_old_dict_then_recovers(tmp_path, monkeypatch):
    old = W._TRANSLATIONS
    f = _point_to(tmp_path, monkeypatch, "def broken(:\n")   # 语法错误
    W.get_translations("zh")
    assert W._TRANSLATIONS is old                             # 坏态保留旧字典

    # 缺语言的字典也拒绝
    f.write_text("_TRANSLATIONS = {'zh': {'x': '1'}}", encoding="utf-8")
    _bump_mtime(f)
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    W.get_translations("zh")
    assert W._TRANSLATIONS is old

    # 修好 → 自动恢复
    f.write_text(_mini_src("修好了"), encoding="utf-8")
    _bump_mtime(f)
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    assert W.get_translations("zh").get("greet") == "修好了"


def test_throttle_skips_stat_within_window(tmp_path, monkeypatch):
    f = _point_to(tmp_path, monkeypatch, _mini_src("A"))
    assert W.get_translations("zh").get("greet") == "A"
    # 窗口内改文件：不应被看见（_next_check_ts 已前移 2s）
    f.write_text(_mini_src("B"), encoding="utf-8")
    _bump_mtime(f)
    assert W.get_translations("zh").get("greet") == "A"
    # 窗口过期 → 生效
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    assert W.get_translations("zh").get("greet") == "B"


def test_baseline_recorded_at_import():
    """基线在 import 时记录（非首次调用）——启动后、首个请求前的改动不会被吞。"""
    assert W._loaded_mtime > 0               # import 即有基线（真实源文件 mtime）


def test_bom_saved_source_still_reloads(tmp_path, monkeypatch):
    """带 BOM 的保存态（PS 5.1 Set-Content 等写出）也能热加载（utf-8-sig 剥 BOM）。"""
    f = _point_to(tmp_path, monkeypatch, _mini_src("旧"))
    W.get_translations("zh")
    f.write_bytes(b"\xef\xbb\xbf" + _mini_src("BOM\u4e5f\u884c").encode("utf-8"))
    _bump_mtime(f)
    monkeypatch.setattr(W, "_next_check_ts", 0.0)
    assert W.get_translations("zh").get("greet") == "BOM也行"


def _bump_mtime(f):
    """确保 mtime 前进（文件系统时间粒度粗时 touch 未必变化）。"""
    st = f.stat()
    import os
    os.utime(f, (st.st_atime, st.st_mtime + 2))
