"""通道类短追问：防整段复述 JC/EP 成功率 —— 辅助函数单测。"""
from src.skills.skill_manager import (
    _is_channel_short_followup,
    _is_meaningless_interjection_only,
    _last_reply_looks_like_channel_summary,
)


class TestChannelFollowupHelpers:
    def test_short_followup_punctuation_and_short_phrases(self):
        assert _is_channel_short_followup("?") is True
        assert _is_channel_short_followup("？") is True
        assert _is_channel_short_followup("正常吗") is True
        assert _is_channel_short_followup("通道") is True
        assert _is_channel_short_followup("波动大吗") is True

    def test_pure_interjection_not_short_followup(self):
        assert _is_meaningless_interjection_only("啊") is True
        assert _is_meaningless_interjection_only("—啊—") is True
        assert _is_meaningless_interjection_only("嗯嗯") is True
        assert _is_channel_short_followup("啊") is False
        assert _is_channel_short_followup("—啊—") is False

    def test_substantive_still_short_followup(self):
        assert _is_meaningless_interjection_only("支付") is False
        assert _is_channel_short_followup("支付") is True

    def test_short_followup_not_long_question(self):
        long_q = "请详细介绍一下你们所有通道的成功率分别是多少以及有没有手续费"
        assert _is_channel_short_followup(long_q) is False

    def test_last_reply_channel_summary_positive(self):
        r = (
            "目前 JC 成功率约 55%，EP 约 50%，两条通道状态都正常可用，"
            "若单笔较大建议先小额测试。"
        )
        assert _last_reply_looks_like_channel_summary(r) is True

    def test_last_reply_channel_summary_too_short_or_no_metric(self):
        assert _last_reply_looks_like_channel_summary("JC 还行") is False
        assert _last_reply_looks_like_channel_summary("通道都正常，欢迎使用") is False
