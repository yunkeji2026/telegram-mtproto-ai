# -*- coding: utf-8 -*-
"""bootstrap.background_tasks 抽取回归测试（Stage 4 批量）。

6 个 startup 辅助方法编排真实服务,完整行为靠"直接全量启动"验证(start() 会调它们);
此处守护抽取最易回归处:可导入性 + 首参为 assistant + async 契约(wrapper 依此 [await] 委托)。"""
import inspect

from src.bootstrap import background_tasks as bt

ASYNC_FUNCS = {
    "maybe_start_proactive_care",
    "maybe_start_reactivation_loop",
    "warmup_embeddings",
    "episodic_backfill_periodic",
}
SYNC_FUNCS = {"ensure_deferred_outbox", "maybe_init_monetization"}


def test_all_importable_and_first_param_assistant():
    for name in ASYNC_FUNCS | SYNC_FUNCS:
        f = getattr(bt, name)
        assert callable(f), name
        assert list(inspect.signature(f).parameters)[0] == "assistant", name


def test_async_sync_contract():
    for name in ASYNC_FUNCS:
        assert inspect.iscoroutinefunction(getattr(bt, name)), name
    for name in SYNC_FUNCS:
        assert not inspect.iscoroutinefunction(getattr(bt, name)), name
