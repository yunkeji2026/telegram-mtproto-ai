"""P40/P41/P42 — 剧本引擎 + 互动积分 + AI 副驾 单元测试。"""
import time

from src.inbox.conversation_script import ConversationScriptEngine
from src.inbox.engagement_scorer import EngagementScorer
from src.inbox.reply_copilot import ReplyCopilot


class TestConversationScript:
    def test_initial_stage_topics(self):
        r = ConversationScriptEngine().suggest_topics("initial", limit=5)
        assert r["stage"] == "initial"
        assert r["stage_label"] == "初识"
        assert len(r["topics"]) >= 2
        assert all(t.get("opener") for t in r["topics"])

    def test_custom_topics_merged(self):
        custom = [{
            "topic_id": "c1", "stage": "warming", "title": "自定义",
            "opener": "聊聊你最近的生活？", "hint": "测试", "tags": ["日常"],
            "enabled": True,
        }]
        r = ConversationScriptEngine().suggest_topics("warming", custom_topics=custom)
        ids = [t["topic_id"] for t in r["topics"]]
        assert "c1" in ids

    def test_reunion_uses_steady_greeting(self):
        r = ConversationScriptEngine().suggest_topics(
            "intimate", reunion=True, limit=5,
        )
        # reunion 时 stage 切换为 steady
        assert r["stage"] == "steady"
        assert r["reunion"] is True

    def test_derive_stage_from_intimacy(self):
        eng = ConversationScriptEngine()
        assert eng.derive_stage_from_signals(exchange_count=0, intimacy_score=10) == "initial"
        assert eng.derive_stage_from_signals(exchange_count=0, intimacy_score=60) == "intimate"
        assert eng.derive_stage_from_signals(exchange_count=20, intimacy_score=None) == "intimate"


class TestEngagementScorer:
    NOW = time.time()

    def _msgs(self, pairs):
        return [
            {"direction": d, "text": t, "ts": self.NOW - gap, "conversation_id": "c1"}
            for d, t, gap in pairs
        ]

    def test_empty_messages_zero_points(self):
        r = EngagementScorer().compute([])
        assert r["points"] == 0
        assert r["level"] == "new"

    def test_active_conversation_scores(self):
        msgs = self._msgs([
            ("in", "今天好开心，谢谢你陪我聊天！", 3600),
            ("out", "我也很开心能陪你，有什么想聊的都可以说。", 3500),
            ("in", "最近工作好累，但跟你聊完感觉好多了", 3400),
            ("out", "辛苦了，慢慢说，我在听。", 3300),
        ])
        r = EngagementScorer().compute(msgs)
        assert r["points"] > 0
        assert r["breakdown"]["sentiment"] > 0

    def test_deep_chat_achievement(self):
        msgs = [{"direction": "in", "text": "hi", "ts": self.NOW - i, "conversation_id": "c1"}
                for i in range(25)]
        r = EngagementScorer().compute(msgs)
        assert "first_deep_chat" in r["new_achievements"]

    def test_vip_at_600_points(self):
        # 构造高积分消息集
        msgs = []
        for day in range(20):
            for _ in range(5):
                msgs.append({
                    "direction": "in",
                    "text": "开心谢谢 great happy",
                    "ts": self.NOW - day * 86400,
                    "conversation_id": "c1",
                })
        r = EngagementScorer().compute(msgs)
        if r["points"] >= 600:
            assert "vip_companion" in r["new_achievements"]
            assert r["is_vip"] is True

    def test_mood_guardian_achievement(self):
        msgs = self._msgs([
            ("in", "好难过，好孤独", 100),
            ("out", "我能理解你的感受，愿意多跟我说说吗？", 50),
        ])
        r = EngagementScorer().compute(msgs)
        assert "mood_guardian" in r["new_achievements"]


class TestReplyCopilot:
    def _items(self, **kwargs):
        return ReplyCopilot().suggest(**kwargs)["suggestions"]

    def test_empty_input_full_suggestions(self):
        items = self._items(
            partial_text="",
            last_customer_msg="最近好烦，不知道怎么办",
            stage="warming",
            limit=3,
        )
        assert len(items) >= 1
        assert all("text" in i for i in items)

    def test_negative_sentiment_empathy_first(self):
        items = self._items(
            partial_text="",
            last_customer_msg="好难过，好孤独",
            stage="initial",
            limit=3,
        )
        sources = [i["source"] for i in items]
        assert "empathy" in sources

    def test_partial_input_template_match(self):
        templates = [{"content": "我能理解你的感受，慢慢来。", "title": "共情"}]
        items = self._items(
            partial_text="我能理解",
            templates=templates,
            limit=3,
        )
        assert any(i["source"] == "template" for i in items)

    def test_limit_respected(self):
        items = self._items(
            partial_text="",
            last_customer_msg="hello",
            stage="steady",
            limit=2,
        )
        assert len(items) <= 2

    def test_dedup_in_suggest(self):
        items = self._items(
            partial_text="",
            last_customer_msg="test",
            stage="initial",
            limit=5,
        )
        texts = [i["text"] for i in items]
        assert len(texts) == len(set(texts))

    def test_workflow_context_priority(self):
        result = ReplyCopilot().suggest(
            partial_text="",
            context={
                "trigger": "workflow_step",
                "workflow_text": "你好，最近怎么样？",
                "workflow_chain_name": "沉默挽回",
                "workflow_step": 1,
                "stage": "warming",
            },
            limit=4,
        )
        assert result["suggestions"][0]["source"] == "workflow_chain"
        assert "工作链" in result["context_header"]

    def test_mention_context(self):
        result = ReplyCopilot().suggest(
            partial_text="",
            context={
                "trigger": "mention",
                "mention_note": "请帮忙跟进这位客户",
                "mention_from": "Alice",
                "stage": "intimate",
            },
            limit=4,
        )
        sources = [s["source"] for s in result["suggestions"]]
        assert "mention_help" in sources

    def test_churn_recovery(self):
        result = ReplyCopilot().suggest(
            partial_text="",
            context={"trigger": "churn", "churn_level": "high", "stage": "intimate"},
            limit=4,
        )
        assert any(s["source"] == "churn_recovery" for s in result["suggestions"])
