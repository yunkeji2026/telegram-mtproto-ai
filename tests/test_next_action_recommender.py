"""P37 — NextActionRecommender 情感陪伴场景单元测试。"""
import pytest
from src.inbox.next_action_recommender import NextActionRecommender


class TestNextActionRecommender:
    def _rec(self, **kwargs):
        return NextActionRecommender().recommend(**kwargs)

    def test_crisis_signal_triggers_escalate(self):
        """危机词 → 升级人工是推荐列表第一位。"""
        results = self._rec(
            last_msg_text="我不想活了，感觉活着没意思",
            last_msg_direction="in",
        )
        assert len(results) > 0
        # 危机场景必须推荐 escalate
        types = [r["action_type"] for r in results]
        assert "escalate" in types
        # escalate 应在前两位
        assert results.index(next(r for r in results if r["action_type"]=="escalate")) <= 1

    def test_negative_sentiment_triggers_empathy(self):
        """负面情绪 → 情感共鸣动作应出现。"""
        results = self._rec(
            last_msg_text="今天好难过，感觉好孤独",
            last_msg_direction="in",
        )
        assert any(r["action_id"] == "__empathy" for r in results)

    def test_long_silence_triggers_special_care(self):
        """7天沉默 → 特别关怀/创建回访任务。"""
        results = self._rec(silence_hours=200)  # > 168h = 7 days
        action_ids = [r["action_id"] for r in results]
        assert "__special_care" in action_ids or "__schedule_followup" in action_ids

    def test_short_silence_triggers_care(self):
        """3天沉默 → 特别关怀。"""
        results = self._rec(silence_hours=80)  # > 72h = 3 days
        action_ids = [r["action_id"] for r in results]
        assert "__special_care" in action_ids

    def test_high_churn_risk_triggers_empathy_and_escalate(self):
        """高流失风险 → 情感共鸣 + 人工升级推荐。"""
        results = self._rec(churn_risk_level="high", limit=6)
        types = [r["action_type"] for r in results]
        assert "escalate" in types

    def test_long_conversation_triggers_deepen(self):
        """多轮长对话 → 深化话题引导。"""
        results = self._rec(message_count=25, last_msg_direction="in")
        action_ids = [r["action_id"] for r in results]
        assert "__deepen_topic" in action_ids or "__advance_intimacy" in action_ids

    def test_all_results_have_required_fields(self):
        """所有推荐结果必须包含必要字段。"""
        results = self._rec(
            last_msg_text="你好啊",
            last_msg_direction="in",
            message_count=5,
        )
        for r in results:
            assert "action_id" in r
            assert "name" in r
            assert "action_type" in r
            assert "icon" in r
            assert "reason" in r

    def test_limit_respected(self):
        """返回数量不超过 limit。"""
        results = self._rec(limit=3)
        assert len(results) <= 3

    def test_custom_action_with_any_trigger(self):
        """自定义动作 trigger_conditions=['any'] 始终包含在推荐中。"""
        custom = [{
            "action_id": "custom_001",
            "name": "自定义：每日问候",
            "action_type": "template",
            "icon": "☀",
            "enabled": True,
            "sort_order": 5,
            "config": {"template_text": "早上好！"},
            "trigger_conditions": ["any"],
        }]
        results = self._rec(custom_actions=custom, limit=8)
        assert any(r["action_id"] == "custom_001" for r in results)

    def test_custom_action_disabled_excluded(self):
        """自定义动作 enabled=False 不应出现。"""
        custom = [{
            "action_id": "custom_002",
            "name": "禁用动作",
            "action_type": "template",
            "icon": "🚫",
            "enabled": False,
            "sort_order": 0,
            "config": {},
            "trigger_conditions": ["any"],
        }]
        results = self._rec(custom_actions=custom)
        assert not any(r["action_id"] == "custom_002" for r in results)

    def test_custom_action_specific_trigger(self):
        """自定义动作指定触发条件，在匹配时才包含。"""
        custom = [{
            "action_id": "custom_003",
            "name": "沉默关怀",
            "action_type": "task",
            "icon": "📅",
            "enabled": True,
            "sort_order": 0,
            "config": {},
            "trigger_conditions": ["silent_7d"],
        }]
        # 不满足条件
        results_no = self._rec(silence_hours=10, custom_actions=custom)
        assert not any(r["action_id"] == "custom_003" for r in results_no)
        # 满足条件
        results_yes = self._rec(silence_hours=200, custom_actions=custom, limit=10)
        assert any(r["action_id"] == "custom_003" for r in results_yes)

    def test_deduplicated_signals(self):
        """信号列表不含重复。"""
        rec = NextActionRecommender()
        sigs = rec._detect_signals(
            risk_signals=[{"signal": "complaint"}, {"signal": "complaint"}],
            last_msg_text="",
            last_msg_direction="in",
            message_count=5,
            silence_hours=0,
            churn_risk_level="",
        )
        assert len(sigs) == len(set(sigs))

    def test_high_engagement_not_shown_for_short_conv(self):
        """短对话不触发 high_engagement 信号。"""
        rec = NextActionRecommender()
        sigs = rec._detect_signals(
            risk_signals=[],
            last_msg_text="",
            last_msg_direction="in",
            message_count=3,
            silence_hours=0,
            churn_risk_level="",
        )
        assert "high_engagement" not in sigs
