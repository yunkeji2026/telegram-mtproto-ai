# -*- coding: utf-8 -*-
"""companion.proactive_topic 抽取回归测试（Stage 4）。

maybe_start_companion_proactive 主体 619 行、15 闭包,完整行为需大量真实服务;
整方法系原样迁出(仅 self->assistant,AST 守卫确认外部名只有 Path/time),
此处守护抽取最易回归处:可导入性 + 未就绪早返路径的 self->assistant 改名正确。"""
import asyncio
import inspect
from unittest.mock import MagicMock

from src.companion.proactive_topic import maybe_start_companion_proactive


def test_is_coroutine():
    assert inspect.iscoroutinefunction(maybe_start_companion_proactive)


def test_returns_when_inbox_not_ready():
    a = MagicMock()
    a.config.config = {"companion": {}}
    a.inbox_store = None  # 未就绪 -> 早返(连预览都不挂)
    asyncio.run(maybe_start_companion_proactive(a))
    a.logger.info.assert_called()  # 记了"跳过"日志


def test_returns_when_skill_manager_not_ready():
    a = MagicMock()
    a.config.config = {"companion": {"proactive_topic": {"enabled": True}}}
    a.skill_manager = None
    asyncio.run(maybe_start_companion_proactive(a))
    a.logger.info.assert_called()
