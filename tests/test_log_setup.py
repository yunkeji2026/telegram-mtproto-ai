"""src.* 命名空间日志落盘（2026-07-12 排障盲区修复）。

钉住三条语义：
- src.* 的 INFO 落进 file handler（此前 root=WARNING 全体隐身）；
- 同一记录不重复写（propagate=False，root 也挂同文件 handler 的场景）；
- 幂等：重复 attach 不叠 handler。
"""

import logging

from src.utils.log_setup import attach_src_file_handler


def _mk_handler(tmp_path, name="app.log"):
    f = tmp_path / name
    h = logging.FileHandler(str(f), encoding="utf-8")
    h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    return f, h


def _cleanup(handlers):
    src = logging.getLogger("src")
    root = logging.getLogger()
    for h in handlers:
        src.removeHandler(h)
        root.removeHandler(h)
        h.close()
    src.propagate = True
    src.setLevel(logging.NOTSET)


def test_src_info_reaches_file(tmp_path):
    f, h = _mk_handler(tmp_path)
    try:
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("src.utils.config_manager").info("配置热重载完成 probe")
        h.flush()
        assert "配置热重载完成 probe" in f.read_text(encoding="utf-8")
    finally:
        _cleanup([h])


def test_no_duplicate_lines_when_root_has_same_file(tmp_path):
    """root 也挂同文件 handler（main.py 现状）：src 的 WARNING 只写一行。"""
    f, h = _mk_handler(tmp_path)
    root = logging.getLogger()
    try:
        root.addHandler(h)
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("src.foo").warning("只此一行 probe")
        h.flush()
        assert f.read_text(encoding="utf-8").count("只此一行 probe") == 1
    finally:
        _cleanup([h])


def test_attach_idempotent(tmp_path):
    f, h = _mk_handler(tmp_path)
    try:
        attach_src_file_handler(h)
        attach_src_file_handler(h)
        src = logging.getLogger("src")
        same = [x for x in src.handlers
                if getattr(x, "baseFilename", None) == getattr(h, "baseFilename", None)]
        assert len(same) == 1
        logging.getLogger("src.bar").info("幂等 probe")
        h.flush()
        assert f.read_text(encoding="utf-8").count("幂等 probe") == 1
    finally:
        _cleanup([h])


def test_third_party_not_affected(tmp_path):
    """三方库（非 src.*）不因本修复获得 INFO 落盘（root 仍 WARNING 口径）。"""
    f, h = _mk_handler(tmp_path)
    root = logging.getLogger()
    old_level = root.level
    try:
        root.setLevel(logging.WARNING)
        root.addHandler(h)
        attach_src_file_handler(h, level=logging.INFO)
        logging.getLogger("httpx").info("三方 INFO 不该出现 probe")
        h.flush()
        assert "三方 INFO 不该出现 probe" not in f.read_text(encoding="utf-8")
    finally:
        root.setLevel(old_level)
        _cleanup([h])
