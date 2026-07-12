# -*- coding: utf-8 -*-
"""bootstrap.web_app.setup_web_app 抽取回归测试（Stage 5）。

setup_web_app 主体 527 行、装配整个 FastAPI 后台,完整执行由 smoke_boot 验证
(端口就绪即证明本块跑通);此处守护:可导入性 + 签名 + web_admin 未启用时整块跳过。"""
import inspect
from unittest.mock import MagicMock

from src.bootstrap.web_app import setup_web_app


def test_signature():
    assert list(inspect.signature(setup_web_app).parameters) == ["assistant", "web_cfg"]


def test_disabled_is_noop():
    a = MagicMock()
    setup_web_app(a, {})  # enabled 缺省 falsy -> 整块跳过,不抛异常
    setup_web_app(a, {"enabled": False})
