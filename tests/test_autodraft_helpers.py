# -*- coding: utf-8 -*-
"""autodraft_helpers 抽取回归测试（Stage 2）。

enrich_auto_draft 主体 261 行、重依赖 persona/draft_svc/inbox_store，
完整行为测试需大量 mock；此处守护抽取最易回归的两点：
可导入性 与 签名契约（wrapper 依此绑定 self/draft_svc/_ad_app/_ad_store）。"""
import inspect

from src.inbox.autodraft_helpers import enrich_auto_draft


def test_enrich_is_coroutine():
    assert inspect.iscoroutinefunction(enrich_auto_draft)


def test_enrich_signature_contract():
    params = list(inspect.signature(enrich_auto_draft).parameters)
    assert params == [
        "assistant", "draft_svc", "_ad_app", "_ad_store",
        "conv", "text", "draft_id", "mode",
    ]
