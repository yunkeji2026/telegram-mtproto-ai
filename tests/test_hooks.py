"""
Tests for the Hook system — Phase 0A core framework decoupling.

Tests:
1. Base DomainHook defaults (no-op behavior)
2. HookRegistry singleton lifecycle
3. PaymentDomainHook specific detection methods
4. Hook dispatch error handling
5. DomainLoader hook loading
"""

import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.hooks.base import DomainHook, HookContext, _default_is_meaningless_interjection
from src.hooks.registry import HookRegistry


class TestHookContext:
    def test_default_values(self):
        ctx = HookContext()
        assert ctx.text == ""
        assert ctx.user_id == ""
        assert ctx.intent == ""
        assert ctx.user_context == {}
        assert ctx.extra == {}

    def test_custom_values(self):
        ctx = HookContext(text="hello", user_id="123", intent="greeting")
        assert ctx.text == "hello"
        assert ctx.user_id == "123"
        assert ctx.intent == "greeting"


class TestBaseDomainHook:
    def setup_method(self):
        self.hook = DomainHook()

    @pytest.mark.asyncio
    async def test_on_message_pre_process_noop(self):
        ctx = HookContext(text="test")
        result = await self.hook.on_message_pre_process(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_on_intent_resolved_passthrough(self):
        ctx = HookContext(text="test")
        result = await self.hook.on_intent_resolved("greeting", ctx)
        assert result == "greeting"

    @pytest.mark.asyncio
    async def test_on_kb_pre_search_passthrough(self):
        ctx = HookContext(text="test")
        query, skip = await self.hook.on_kb_pre_search("test query", ctx)
        assert query == "test query"
        assert skip is False

    @pytest.mark.asyncio
    async def test_on_reply_generated_passthrough(self):
        ctx = HookContext(text="test")
        result = await self.hook.on_reply_generated("hello reply", ctx)
        assert result == "hello reply"

    @pytest.mark.asyncio
    async def test_on_reply_post_process_passthrough(self):
        ctx = HookContext(text="test")
        result = await self.hook.on_reply_post_process("final reply", ctx)
        assert result == "final reply"

    def test_get_narrow_reply_config_none(self):
        assert self.hook.get_narrow_reply_config() is None

    def test_get_intent_override_rules_empty(self):
        assert self.hook.get_intent_override_rules() == []

    def test_get_followup_config_empty(self):
        assert self.hook.get_followup_config() == {}

    def test_get_ambiguous_tokens_empty(self):
        assert self.hook.get_ambiguous_tokens() == set()

    def test_get_channel_status_info_none(self):
        assert self.hook.get_channel_status_info() is None

    def test_get_reply_angle_rotation_empty(self):
        assert self.hook.get_reply_angle_rotation() == {}

    def test_get_escalation_line(self):
        line = self.hook.get_escalation_line()
        assert "人工" in line

    def test_is_ambiguous_token_false(self):
        assert self.hook.is_ambiguous_token_message("hello") is False

    def test_is_short_followup_false(self):
        assert self.hook.is_short_followup("some text") is False

    def test_last_reply_looks_like_summary_false(self):
        assert self.hook.last_reply_looks_like_summary("hello") is False

    def test_is_domain_metrics_query_false(self):
        assert self.hook.is_domain_metrics_query("what is the weather") is False

    def test_get_extra_intent_keywords_empty(self):
        assert self.hook.get_extra_intent_keywords() == {}


class TestMeaninglessInterjection:
    def test_empty(self):
        assert _default_is_meaningless_interjection("") is True

    def test_pure_interjection(self):
        assert _default_is_meaningless_interjection("啊") is True
        assert _default_is_meaningless_interjection("嗯嗯") is True
        assert _default_is_meaningless_interjection("哦哦") is True

    def test_question_mark_not_meaningless(self):
        assert _default_is_meaningless_interjection("啊？") is False

    def test_digits_not_meaningless(self):
        assert _default_is_meaningless_interjection("3") is False

    def test_latin_not_meaningless(self):
        assert _default_is_meaningless_interjection("ok") is False

    def test_long_text_not_meaningless(self):
        assert _default_is_meaningless_interjection("这是一段比较长的有意义的话") is False


class TestHookRegistry:
    def setup_method(self):
        HookRegistry.reset()

    def test_singleton(self):
        r1 = HookRegistry.get_instance()
        r2 = HookRegistry.get_instance()
        assert r1 is r2

    def test_default_hook_is_base(self):
        reg = HookRegistry.get_instance()
        assert isinstance(reg.hook, DomainHook)
        assert not reg.has_custom_hook

    def test_register_custom_hook(self):
        reg = HookRegistry.get_instance()

        class MyHook(DomainHook):
            pass

        hook = MyHook()
        reg.register(hook, "test_domain")
        assert reg.hook is hook
        assert reg.domain_name == "test_domain"
        assert reg.has_custom_hook

    def test_reset(self):
        r1 = HookRegistry.get_instance()
        HookRegistry.reset()
        r2 = HookRegistry.get_instance()
        assert r1 is not r2

    @pytest.mark.asyncio
    async def test_dispatch_error_handling(self):
        reg = HookRegistry.get_instance()

        class BrokenHook(DomainHook):
            async def on_intent_resolved(self, intent, ctx):
                raise RuntimeError("boom")

        reg.register(BrokenHook())
        ctx = HookContext(text="test")
        result = await reg.dispatch_intent_resolved("greeting", ctx)
        assert result == "greeting"

    @pytest.mark.asyncio
    async def test_dispatch_pre_process(self):
        reg = HookRegistry.get_instance()
        ctx = HookContext(text="test")
        result = await reg.dispatch_pre_process(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_kb_pre_search(self):
        reg = HookRegistry.get_instance()
        ctx = HookContext(text="test")
        query, skip = await reg.dispatch_kb_pre_search("test", ctx)
        assert query == "test"
        assert skip is False

    def test_sync_config_dispatch(self):
        reg = HookRegistry.get_instance()
        assert reg.get_narrow_reply_config() is None
        assert reg.get_followup_config() == {}
        assert reg.get_ambiguous_tokens() == set()
        assert reg.get_reply_angle_rotation() == {}
        assert "人工" in reg.get_escalation_line()


class TestPaymentDomainHook:
    def setup_method(self):
        from domains.payment.hooks import PaymentDomainHook
        self.hook = PaymentDomainHook()

    def test_ambiguous_tokens(self):
        assert self.hook.is_ambiguous_token_message("ep") is True
        assert self.hook.is_ambiguous_token_message("jc") is True
        assert self.hook.is_ambiguous_token_message("EP") is True
        assert self.hook.is_ambiguous_token_message("ep jc") is True
        assert self.hook.is_ambiguous_token_message("hello world") is False
        assert self.hook.is_ambiguous_token_message("") is False

    def test_short_followup(self):
        assert self.hook.is_short_followup("正常吗") is True
        assert self.hook.is_short_followup("波动") is True
        assert self.hook.is_short_followup("ok?") is True
        assert self.hook.is_short_followup("") is False
        assert self.hook.is_short_followup("嗯") is False  # interjection

    def test_last_reply_looks_like_summary(self):
        assert self.hook.last_reply_looks_like_summary(
            "JC 代收成功率 95%，EP 代收成功率 88%"
        ) is True
        assert self.hook.last_reply_looks_like_summary("你好") is False
        assert self.hook.last_reply_looks_like_summary("") is False

    def test_domain_metrics_query(self):
        assert self.hook.is_domain_metrics_query("成功率多少") is True
        assert self.hook.is_domain_metrics_query("费率是多少") is True
        assert self.hook.is_domain_metrics_query("success rate") is True
        assert self.hook.is_domain_metrics_query("你好") is False

    @pytest.mark.asyncio
    async def test_intent_override_gxp_digit(self):
        ctx = HookContext(
            text="3",
            user_context={"gxp_last_ask": "what"},
            extra={"available_skills": {"gxp_command"}},
        )
        result = await self.hook.on_intent_resolved("small_talk", ctx)
        assert result == "gxp_command"

    @pytest.mark.asyncio
    async def test_intent_override_channel_name_reply(self):
        import time
        ctx = HookContext(
            text="jc",
            user_context={"_bot_question_ts": time.time()},
            extra={"available_skills": set()},
        )
        result = await self.hook.on_intent_resolved("small_talk", ctx)
        assert result == "channel_info"

    @pytest.mark.asyncio
    async def test_kb_pre_search_skip_metrics(self):
        ctx = HookContext(text="成功率多少", intent="channel_info", user_context={})
        _, skip = await self.hook.on_kb_pre_search("成功率多少", ctx)
        assert skip is True

    @pytest.mark.asyncio
    async def test_kb_pre_search_no_skip(self):
        ctx = HookContext(text="你好", intent="greeting", user_context={})
        _, skip = await self.hook.on_kb_pre_search("你好", ctx)
        assert skip is False

    def test_narrow_reply_config(self):
        cfg = self.hook.get_narrow_reply_config()
        assert cfg is not None
        assert "cs_online_substrings" in cfg
        assert "channel_topic_substrings" in cfg
        assert "deny_substrings" in cfg

    def test_followup_config(self):
        fc = self.hook.get_followup_config()
        assert "followup_intents" in fc
        assert "channel_info" in fc["followup_intents"]

    def test_reply_angle_rotation(self):
        rot = self.hook.get_reply_angle_rotation()
        assert "channel_info" in rot
        assert "order_query" in rot
        assert len(rot["channel_info"]) >= 2

    def test_escalation_line(self):
        line = self.hook.get_escalation_line()
        assert "人工客服" in line

    def test_get_ambiguous_tokens_set(self):
        tokens = self.hook.get_ambiguous_tokens()
        assert "ep" in tokens
        assert "jc" in tokens

    def test_bare_order_no(self):
        assert self.hook._is_bare_order_no("123456")[0] is True
        assert self.hook._is_bare_order_no("12345")[0] is False
        assert self.hook._is_bare_order_no("查询代收123456")[0] is False


class TestDomainLoaderHooks:
    def test_load_payment_hooks(self):
        from src.utils.domain_loader import DomainLoader, DomainPack
        project_root = Path(__file__).parent.parent
        domains_dir = project_root / "domains"
        loader = DomainLoader(domains_dir)

        manifest_path = domains_dir / "payment" / "manifest.yaml"
        assert manifest_path.exists(), "payment manifest.yaml must exist"

        import yaml
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert manifest.get("hooks") is True

        pack = DomainPack("payment", domains_dir / "payment", manifest)
        loader._load_hooks(pack, None)
        assert pack.hook_class is not None
        assert pack.hook_class.__name__ == "PaymentDomainHook"


class TestHookRegistryIntegration:
    """Integration test: register payment hook and verify full dispatch chain."""

    def setup_method(self):
        HookRegistry.reset()

    @pytest.mark.asyncio
    async def test_full_payment_dispatch(self):
        from domains.payment.hooks import PaymentDomainHook
        reg = HookRegistry.get_instance()
        reg.register(PaymentDomainHook(), "payment")

        assert reg.has_custom_hook
        assert reg.domain_name == "payment"

        # Ambiguous token
        assert reg.is_ambiguous_token_message("ep") is True
        assert reg.is_ambiguous_token_message("hello") is False

        # Short followup
        assert reg.is_short_followup("正常吗") is True

        # Metrics query
        assert reg.is_domain_metrics_query("成功率多少") is True

        # Intent dispatch
        import time
        ctx = HookContext(
            text="jc",
            user_context={"_bot_question_ts": time.time()},
            extra={"available_skills": set()},
        )
        intent = await reg.dispatch_intent_resolved("small_talk", ctx)
        assert intent == "channel_info"

        # KB pre-search dispatch
        ctx2 = HookContext(text="费率", intent="channel_info", user_context={})
        _, skip = await reg.dispatch_kb_pre_search("费率", ctx2)
        assert skip is True

    def teardown_method(self):
        HookRegistry.reset()
