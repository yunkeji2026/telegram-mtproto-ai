"""FourLayerTrigger 决策矩阵单测。

四层触发器（L1 规则 → L2 语义 → L3 上下文过滤 → L4 静默兜底）是 Telegram 群「该不该
回」的核心闸门，~976 行却长期零单测。本文件按层补「决策矩阵」：每层的命中/未命中分支、
缓存复用、统计计数、异常保守回退，以及 L3ContextProcessor.get_smart_cooldown 的回归守卫
（该函数曾误引用不存在的 self.trigger_config，一调即 AttributeError）。

注：asyncio_mode=auto（见 pytest.ini），async 测试直接跑，无需手写 event loop。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.trigger import four_layer_trigger as flt_mod
from src.trigger.four_layer_trigger import (
    FourLayerTrigger,
    L3ContextProcessor,
)

# 受控触发规则：固定阈值/关键词，使决策可预测，不依赖仓库真实 trigger_rules.yaml。
_RULES_YAML = textwrap.dedent(
    """
    l1_rule_trigger:
      enabled: true
      image_with_text:
        enabled: true
        min_text_length: 1
      high_frequency_keywords:
        enabled: true
        case_sensitive: false
        all_keywords: ["订单", "payment"]
      order_number_patterns:
        enabled: true
        patterns: ['\\d{6,}']
      mention_trigger:
        enabled: true
        usernames: ["@kefu", "camille"]
    l2_semantic_trigger:
      enabled: true
      confidence_thresholds:
        reply_threshold: 0.75
    l3_context_filter:
      enabled: true
      cooldown:
        enabled: true
        default_cooldown: 90
      small_talk_detection:
        enabled: false
      multi_user_filter:
        enabled: false
    l4_human_fallback:
      enabled: true
      logging:
        enabled: false
    global:
      enabled: true
    """
)


def _make_trigger(tmp_path, *, rules_yaml=_RULES_YAML, context_manager=None,
                  ai_client=None, intent=None):
    rules_file = tmp_path / "trigger_rules.yaml"
    rules_file.write_text(rules_yaml, encoding="utf-8")
    config = {"trigger": {"config_file": str(rules_file)}}
    if intent is not None:
        config["intent"] = intent
    return FourLayerTrigger(config, context_manager=context_manager,
                            ai_client=ai_client)


@pytest.fixture
def trig(tmp_path):
    return _make_trigger(tmp_path)


class FakeAI:
    """最小 ai_client：chat 返回固定字符串（L2 从中正则抓置信度）。"""

    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    async def chat(self, prompt, strategy_overrides=None):
        self.calls += 1
        return self._resp


class FakeContext:
    def __init__(self, recent=None):
        self._recent = recent or []

    def get_recent_messages(self, chat_id, limit=10):
        return self._recent[:limit]


# ── L1 规则层 ────────────────────────────────────────────
class TestL1:
    async def test_mention_bot_username_triggers(self, trig):
        ok, d = await trig.should_reply(
            "你好 @wookfaith 在吗", "c1", "u1", "alice",
            bot_username="wookfaith")
        assert ok is True
        assert d["layers"]["l1"]["trigger_reason"] == "@本账号"

    async def test_standalone_zai_triggers(self, trig):
        ok, d = await trig.should_reply("在", "c1", "u1", "alice")
        assert ok is True
        assert "在" in d["reason"]

    async def test_image_with_text_triggers(self, trig):
        ok, d = await trig.should_reply("看这个", "c1", "u1", "alice",
                                        has_image=True)
        assert ok is True
        assert "图片" in d["layers"]["l1"]["trigger_reason"]

    async def test_high_frequency_keyword_triggers(self, trig):
        ok, d = await trig.should_reply("我的订单怎么了", "c1", "u1", "alice")
        assert ok is True
        assert d["layers"]["l1"]["checks"]["matched_keyword"] == "订单"

    async def test_keyword_normalizes_fullwidth_space(self, trig):
        # 全角空格分隔 "pay ment" → 规范化后命中 "payment"
        ok, _ = await trig.should_reply("pay\u3000ment now", "c1", "u1", "a")
        assert ok is True

    async def test_order_number_pattern_triggers(self, trig):
        ok, d = await trig.should_reply("单号 1234567", "c1", "u1", "alice")
        assert ok is True
        assert d["layers"]["l1"]["checks"]["order_number_pattern"] is True

    async def test_mention_username_triggers(self, trig):
        ok, d = await trig.should_reply("找 camille 处理", "c1", "u1", "alice")
        assert ok is True
        assert "@提及" in d["layers"]["l1"]["trigger_reason"]

    async def test_l1_disabled_falls_through_to_l2(self, tmp_path):
        rules = _RULES_YAML.replace(
            "l1_rule_trigger:\n  enabled: true",
            "l1_rule_trigger:\n  enabled: false")
        t = _make_trigger(tmp_path, rules_yaml=rules)
        # "订单" 本是 L1 关键词；L1 关后由 L2 业务关键词兜底（0.95 ≥ 0.75）
        ok, d = await t.should_reply("订单", "c1", "u1", "alice")
        assert ok is True
        assert "l1" not in d["layers"] or not d["layers"]["l1"].get("triggered")
        assert d["reason"].startswith("L2")


# ── L2 语义层 ────────────────────────────────────────────
class TestL2:
    async def test_rule_confidence_proceeds_on_question(self, trig):
        # 非 L1 关键词，但问句标志 "怎么" → 规则置信度 0.90 ≥ 0.75 → 过
        ok, d = await trig.should_reply("这个怎么用", "c1", "u1", "alice")
        assert ok is True
        assert d["reason"] == "L2语义触发 + L3上下文通过"
        assert d["layers"]["l2"]["confidence"] >= 0.75

    async def test_low_confidence_silenced_to_l4(self, trig):
        ok, d = await trig.should_reply("qqq", "c1", "u1", "alice")
        assert ok is False
        assert d["layers"]["l2"]["should_proceed"] is False
        assert "L2置信度不足" in d["reason"]

    async def test_ai_client_high_confidence_proceeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flt_mod, "AI_CLIENT_AVAILABLE", True)
        t = _make_trigger(tmp_path, ai_client=FakeAI("0.9"))
        ok, d = await t.should_reply("zzz 无规则命中", "c1", "u1", "alice")
        assert ok is True
        assert d["layers"]["l2"]["confidence"] == pytest.approx(0.9)
        assert t.ai_client.calls == 1

    async def test_ai_client_low_confidence_silenced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flt_mod, "AI_CLIENT_AVAILABLE", True)
        t = _make_trigger(tmp_path, ai_client=FakeAI("0.10"))
        ok, d = await t.should_reply("这个怎么用", "c1", "u1", "alice")
        # AI 给 0.10 覆盖规则的 0.90 → 静默
        assert ok is False
        assert d["layers"]["l2"]["confidence"] == pytest.approx(0.10)

    async def test_check_l2_only_skips_l1_l3(self, trig):
        ok, reason = await trig.check_l2_only("这个怎么用", "c1", "u1")
        assert ok is True
        assert "置信度" in reason


# ── L3 上下文过滤层 ──────────────────────────────────────
class TestL3:
    async def test_cooldown_blocks(self, trig):
        trig.update_cooldown("c1", "u1")           # 先置冷却
        ok, d = await trig.should_reply("这个怎么用", "c1", "u1", "alice")
        assert ok is False
        assert "冷却" in d["reason"]
        assert d["layers"]["l3"]["should_reply"] is False

    async def test_cooldown_only_affects_same_user(self, trig):
        trig.update_cooldown("c1", "u1")
        ok, _ = await trig.should_reply("这个怎么用", "c1", "u2", "bob")
        assert ok is True            # 不同 user 不受 u1 冷却影响

    async def test_small_talk_filtered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flt_mod, "CONTEXT_MANAGER_AVAILABLE", True)
        rules = _RULES_YAML.replace(
            "small_talk_detection:\n    enabled: false",
            "small_talk_detection:\n    enabled: true\n"
            "    small_talk_keywords: [\"哈哈\"]\n"
            "    business_keywords: [\"订单\"]")
        t = _make_trigger(tmp_path, rules_yaml=rules,
                          context_manager=FakeContext([]))
        # "哈哈" 带语气词 → L2 0.76 过；L3 小话题命中且无业务词 → 过滤
        ok, d = await t.should_reply("哈哈", "c1", "u1", "alice")
        assert ok is False
        assert "闲聊" in d["reason"]

    async def test_multi_user_filtered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(flt_mod, "CONTEXT_MANAGER_AVAILABLE", True)
        rules = _RULES_YAML.replace(
            "multi_user_filter:\n    enabled: false",
            "multi_user_filter:\n    enabled: true\n"
            "    min_users_to_skip: 3\n    check_window: 10")
        recent = [{"user_id": f"u{i}", "text": "hi"} for i in range(3)]
        t = _make_trigger(tmp_path, rules_yaml=rules,
                          context_manager=FakeContext(recent))
        ok, d = await t.should_reply("这个怎么用", "c1", "u1", "alice")
        assert ok is False
        assert "多人聊天" in d["reason"]

    async def test_l3_disabled_passes(self, tmp_path):
        rules = _RULES_YAML.replace(
            "l3_context_filter:\n  enabled: true",
            "l3_context_filter:\n  enabled: false")
        t = _make_trigger(tmp_path, rules_yaml=rules)
        t.update_cooldown("c1", "u1")     # 即便有冷却，L3 关 → 不拦
        ok, _ = await t.should_reply("这个怎么用", "c1", "u1", "alice")
        assert ok is True


# ── 缓存 / 统计 / 异常 ───────────────────────────────────
class TestCacheStatsErrors:
    async def test_decision_cached_second_call_skips_layers(self, trig):
        await trig.should_reply("我的订单", "c1", "u1", "alice")
        assert trig.stats["l1_triggers"] == 1
        # 第二次同参 → 命中缓存，不再重跑 L1（计数不再增）
        ok, _ = await trig.should_reply("我的订单", "c1", "u1", "alice")
        assert ok is True
        assert trig.stats["l1_triggers"] == 1
        assert trig.stats["total_messages"] == 2

    async def test_get_stats_rates(self, trig):
        await trig.should_reply("我的订单", "c1", "u1", "a")    # L1
        await trig.should_reply("qqq", "c1", "u2", "b")          # L4 静默
        stats = trig.get_stats()
        assert stats["total_messages"] == 2
        assert stats["l1_triggers"] == 1
        assert stats["l4_silenced"] == 1
        assert stats["l1_trigger_rate"] == pytest.approx(0.5)
        assert stats["l4_silence_rate"] == pytest.approx(0.5)

    async def test_reset_stats(self, trig):
        await trig.should_reply("我的订单", "c1", "u1", "a")
        trig.reset_stats()
        assert trig.stats["total_messages"] == 0
        assert trig.stats["l1_triggers"] == 0

    async def test_exception_is_conservative_no_reply(self, trig, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("simulated")
        monkeypatch.setattr(trig, "_check_l1_rule_trigger", boom)
        ok, d = await trig.should_reply("我的订单", "c9", "u9", "alice")
        assert ok is False                       # 出错保守不回
        assert "决策出错" in d["reason"]
        assert d["error"] == "simulated"


# ── 回归守卫：get_smart_cooldown 曾因 self.trigger_config 不存在而崩 ──
class TestSmartCooldownRegression:
    def _proc(self, *, smart_enabled=True, context=None):
        cfg = {
            "l3_context_filter": {
                "cooldown": {
                    "default_cooldown": 90,
                    "smart_cooldown": {
                        "enabled": smart_enabled,
                        "business_conversation_cooldown": 30,
                        "small_talk_cooldown": 120,
                    },
                },
                "small_talk_detection": {
                    "business_keywords": ["订单"],
                    "small_talk_keywords": ["哈哈"],
                },
            }
        }
        return L3ContextProcessor(cfg, context_manager=context)

    def test_no_attribute_error_and_default_without_context(self):
        # 无 context_manager → 返回 default_cooldown（关键：不再 AttributeError）
        proc = self._proc(context=None)
        assert proc.get_smart_cooldown("c1", "u1") == 90

    def test_smart_disabled_returns_default(self):
        proc = self._proc(smart_enabled=False, context=None)
        assert proc.get_smart_cooldown("c1", "u1") == 90

    def test_business_heavy_recent_uses_business_cooldown(self):
        recent = [{"text": "我的订单"}, {"text": "订单到了吗"}]
        proc = self._proc(context=FakeContext(recent))
        assert proc.get_smart_cooldown("c1", "u1") == 30

    def test_small_talk_heavy_recent_uses_small_talk_cooldown(self):
        recent = [{"text": "哈哈"}, {"text": "哈哈哈"}]
        proc = self._proc(context=FakeContext(recent))
        assert proc.get_smart_cooldown("c1", "u1") == 120
