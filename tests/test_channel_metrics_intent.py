"""
成功率/费率/限额 与 KB 跳过策略的轻量集成测试（不启动 Bot、不访问 knowledge_base.db）。

Phase 0A 重构后：
- _recognize_intent 仅做通用意图识别（基于 config 关键词）
- 支付行业特定的意图重映射（费率→channel_info 等）由 PaymentDomainHook.on_intent_resolved 负责
- 测试分两层：(1) 静态方法 _is_channel_metrics_query (2) Hook 级别的意图重映射
"""

import asyncio
import pytest
from unittest.mock import MagicMock

from src.skills.skill_manager import SkillManager
from src.hooks.base import HookContext
from src.hooks.registry import HookRegistry


def _bare_sm_for_intent() -> SkillManager:
    """最小 SkillManager：仅用于 _recognize_intent / 静态方法。"""
    sm = object.__new__(SkillManager)
    sm.config = MagicMock()
    sm.config.get_telegram_config.return_value = {"gxp_commands": {"enabled": False}}
    sm.skills = {}
    sm.intent_keywords = {
        "price_check": ["价格", "多少钱", "价钱"],
        "channel_info": ["通道", "额度"],
        "greeting": ["你好"],
    }
    sm.intent_patterns = {}
    return sm


class TestIsChannelMetricsQuery:
    def test_success_rate_zh(self):
        assert SkillManager._is_channel_metrics_query("EP 成功率多少")

    def test_success_rate_en(self):
        assert SkillManager._is_channel_metrics_query("what is the success rate")

    def test_fee_words(self):
        assert SkillManager._is_channel_metrics_query("手续费怎么算")
        assert SkillManager._is_channel_metrics_query("今天费率")

    def test_quota_only_not_metrics(self):
        assert not SkillManager._is_channel_metrics_query("单笔限额多少")
        assert not SkillManager._is_channel_metrics_query("限额")


class TestRecognizeIntentChannelInfo:
    """Phase 0A: payment-specific intent overrides now live in PaymentDomainHook."""

    def setup_method(self):
        HookRegistry.reset()
        from domains.payment.hooks import PaymentDomainHook
        HookRegistry.get_instance().register(PaymentDomainHook(), "payment")

    def teardown_method(self):
        HookRegistry.reset()

    @pytest.mark.asyncio
    async def test_fee_routes_channel_info_via_hook(self):
        """费率/手续费 → hook overrides base intent to channel_info."""
        from domains.payment.hooks import PaymentDomainHook
        hook = PaymentDomainHook()
        ctx = HookContext(text="今天费率多少钱", extra={"available_skills": set()})
        result = await hook.on_intent_resolved("price_check", ctx)
        assert result == "channel_info"

    @pytest.mark.asyncio
    async def test_success_rate_via_hook(self):
        hook = _get_payment_hook()
        ctx = HookContext(text="EP 成功率稳吗", extra={"available_skills": set()})
        result = await hook.on_intent_resolved("direct_chat", ctx)
        assert result == "channel_info"

    @pytest.mark.asyncio
    async def test_handling_fee_via_hook(self):
        hook = _get_payment_hook()
        ctx = HookContext(text="代收手续费多少", extra={"available_skills": set()})
        result = await hook.on_intent_resolved("price_check", ctx)
        assert result == "channel_info"

    @pytest.mark.asyncio
    async def test_single_transaction_via_hook(self):
        hook = _get_payment_hook()
        ctx = HookContext(text="单笔最多多少", extra={"available_skills": set()})
        result = await hook.on_intent_resolved("direct_chat", ctx)
        assert result == "channel_info"

    def test_price_stays_price_check_without_hook(self):
        """Without payment hook, 价格 stays as price_check."""
        sm = _bare_sm_for_intent()
        assert sm._recognize_intent("今天价格多少钱") == "price_check"


class TestKbSkipCondition:
    """复现 process_message 中 KB 跳过条件 — 现在通过 hook dispatch。"""

    @pytest.mark.asyncio
    async def test_skip_when_channel_info_and_metrics_query(self):
        hook = _get_payment_hook()
        ctx = HookContext(text="费率多少", intent="channel_info", user_context={})
        _, skip = await hook.on_kb_pre_search("费率多少", ctx)
        assert skip

    @pytest.mark.asyncio
    async def test_no_skip_quota_only(self):
        hook = _get_payment_hook()
        ctx = HookContext(text="限额多少", intent="channel_info", user_context={})
        _, skip = await hook.on_kb_pre_search("限额多少", ctx)
        assert not skip


def _get_payment_hook():
    from domains.payment.hooks import PaymentDomainHook
    return PaymentDomainHook()
