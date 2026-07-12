# -*- coding: utf-8 -*-
"""bootstrap.logging_setup 抽取回归测试（Stage 5 收尾）。

setup_logging 配置 root logger + file handler,完整行为由 smoke_boot 验证;
此处守护:可导入性 + 签名 + log_config 为空(falsy)时整块跳过。"""
import inspect

from src.bootstrap.logging_setup import setup_logging


def test_signature():
    assert list(inspect.signature(setup_logging).parameters) == ["assistant", "log_config"]


def test_empty_config_is_noop():
    from unittest.mock import MagicMock
    setup_logging(MagicMock(), {})  # 空 config -> if log_config 为 falsy -> 跳过,不抛异常
