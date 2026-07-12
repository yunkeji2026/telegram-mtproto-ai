# -*- coding: utf-8 -*-
"""bootstrap.lifecycle 抽取回归测试（Stage 4）。

start/stop 编排真实服务,完整行为靠"直接全量启动 35s"验证(见 scripts/smoke_boot 之外的
直启检查);此处守护抽取最易回归处:可导入性 + 协程契约(wrapper 依此 await 委托)。"""
import inspect

from src.bootstrap.lifecycle import start_assistant, stop_assistant


def test_start_is_coroutine():
    assert inspect.iscoroutinefunction(start_assistant)


def test_stop_is_coroutine():
    assert inspect.iscoroutinefunction(stop_assistant)


def test_signatures_take_assistant():
    assert list(inspect.signature(start_assistant).parameters) == ["assistant"]
    assert list(inspect.signature(stop_assistant).parameters) == ["assistant"]
