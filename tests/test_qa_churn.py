"""P34/P35 — QA Scorer + Churn Predictor 单元测试。"""
import time
import pytest

from src.inbox.qa_scorer import QAScorer, compute_qa_score
from src.inbox.churn_predictor import ChurnPredictor


# ── QA Scorer 测试 ─────────────────────────────────────────────────────────

def _make_msgs(pairs, base_ts=1_700_000_000.0):
    """helper：生成 [(direction, text, gap_sec)] 消息列表。"""
    msgs = []
    ts = base_ts
    for direction, text, gap in pairs:
        ts += gap
        msgs.append({"direction": direction, "text": text, "ts": ts})
    return msgs


class TestQAScorer:
    def test_empty_messages(self):
        result = compute_qa_score([])
        assert result["score"] == 0
        assert result["grade"] == "N/A"

    def test_good_conversation(self):
        """快速回复 + 以出站结束 + 长消息 + 无风险 → A 级。"""
        msgs = _make_msgs([
            ("in",  "你好，请问产品价格是多少？", 0),
            ("out", "您好！我们的产品价格为 299 元，包含保修和售后服务，全国包邮，欢迎下单！", 60),
            ("in",  "好的，请问可以开发票吗？", 300),
            ("out", "当然可以！我们提供增值税专用发票，请在下单时备注公司名称和税号，我们会在发货时随附。", 90),
        ])
        result = compute_qa_score(msgs)
        assert result["score"] >= 75
        assert result["grade"] in ("A", "B")
        assert result["avg_response_sec"] > 0

    def test_slow_response(self):
        """超长响应时间 → 响应速度分低。"""
        msgs = _make_msgs([
            ("in",  "请问什么时候发货？", 0),
            ("out", "好的，我们尽快处理。", 7200),   # 2 小时后才回
        ])
        result = compute_qa_score(msgs)
        bd = result["breakdown"]
        assert bd["response_speed"] <= 30   # 2h → ≤30分

    def test_no_outbound(self):
        """完全没有坐席回复 → 低分。"""
        msgs = _make_msgs([
            ("in", "你好", 0),
            ("in", "有人吗", 300),
        ])
        result = compute_qa_score(msgs)
        assert result["score"] < 50
        assert result["breakdown"]["resolution"] == 20

    def test_last_msg_outbound(self):
        """末条为出站 → 解决率满分。"""
        msgs = _make_msgs([
            ("in",  "投诉你们服务差！", 0),
            ("out", "非常抱歉！我们会立即处理，感谢您的反馈。", 120),
        ])
        result = compute_qa_score(msgs)
        assert result["breakdown"]["resolution"] == 95

    def test_risk_signals_lower_score(self):
        """含多个风险词 → 风险管控分低。"""
        msgs = _make_msgs([
            ("in",  "太差了，退款退货，我要投诉，我去报警！", 0),
            ("out", "好的", 60),
        ])
        result = compute_qa_score(msgs)
        assert result["breakdown"]["risk_control"] <= 50

    def test_short_messages_quality(self):
        """极短回复 → 消息质量分低。"""
        msgs = _make_msgs([
            ("in",  "请问有没有优惠？", 0),
            ("out", "有", 30),     # 1 字符
            ("in",  "什么优惠？", 300),
            ("out", "嗯", 60),     # 1 字符
        ])
        result = compute_qa_score(msgs)
        assert result["breakdown"]["message_quality"] <= 30

    def test_grade_mapping(self):
        scorer = QAScorer()
        assert scorer._score_to_grade(95) == "A"
        assert scorer._score_to_grade(80) == "B"
        assert scorer._score_to_grade(65) == "C"
        assert scorer._score_to_grade(50) == "D"
        assert scorer._score_to_grade(30) == "F"

    def test_response_gaps_not_exceeding_24h(self):
        """超过 24h 的响应间隔不应计入均值。"""
        msgs = _make_msgs([
            ("in",  "第一条消息", 0),
            ("out", "隔天才回", 90000),  # 25h
        ])
        scorer = QAScorer()
        gaps = scorer._compute_response_gaps(sorted(msgs, key=lambda m: m["ts"]))
        assert len(gaps) == 0  # 超 24h 不计

    def test_multiple_response_gaps(self):
        """多轮对话计算平均响应时间。"""
        msgs = _make_msgs([
            ("in",  "问题1", 0),
            ("out", "答复1", 120),    # gap=120s
            ("in",  "问题2", 300),
            ("out", "答复2", 180),    # gap=180s
        ])
        scorer = QAScorer()
        gaps = scorer._compute_response_gaps(sorted(msgs, key=lambda m: m["ts"]))
        assert len(gaps) == 2
        avg = sum(gaps) / len(gaps)
        assert 140 <= avg <= 160   # 平均 (120+180)/2 = 150


# ── Churn Predictor 测试 ─────────────────────────────────────────────────────

class TestChurnPredictor:
    NOW = 1_700_000_000.0

    def test_low_risk_recent_activity(self):
        """最近有活动 + 无风险词 → low。"""
        level, score, reasons = ChurnPredictor().predict(
            "conv1",
            last_ts=self.NOW - 3600,   # 1小时前
            last_msg_text="好的，谢谢！",
            last_msg_direction="in",
            now=self.NOW,
        )
        assert level == "low"
        assert score < 40

    def test_high_risk_long_silence_with_threat(self):
        """超长沉默 + 法律威胁词 → high。"""
        level, score, reasons = ChurnPredictor().predict(
            "conv2",
            last_ts=self.NOW - 20 * 86400,  # 20天前
            last_msg_text="我要找律师，这是骗局！",
            last_msg_direction="in",
            silence_threshold_days=7,
            now=self.NOW,
        )
        assert level == "high"
        assert score >= 70
        assert len(reasons) >= 2

    def test_medium_risk_silence_only(self):
        """沉默 10 天但无特殊词 → medium 左右。"""
        level, score, reasons = ChurnPredictor().predict(
            "conv3",
            last_ts=self.NOW - 10 * 86400,
            last_msg_text="好的，我再考虑一下。",
            last_msg_direction="in",
            silence_threshold_days=7,
            now=self.NOW,
        )
        # 沉默 >= 7天 + 坐席未回复 1天以上
        assert level in ("medium", "high")
        assert score >= 25

    def test_churn_keyword_trigger(self):
        """cancel / 取消 → 包含流失意图分。"""
        _, score, reasons = ChurnPredictor().predict(
            "conv4",
            last_ts=self.NOW - 2 * 86400,
            last_msg_text="I want to cancel my order.",
            last_msg_direction="in",
            now=self.NOW,
        )
        # cancel 词命中
        assert any("放弃购买" in r or "cancel" in r.lower() for r in reasons)

    def test_low_qa_score_adds_risk(self):
        """低 QA 评分 → 流失分增加。"""
        _, score_good, _ = ChurnPredictor().predict(
            "conv5",
            last_ts=self.NOW - 8 * 86400,
            qa_score=90,
            now=self.NOW,
        )
        _, score_bad, _ = ChurnPredictor().predict(
            "conv5",
            last_ts=self.NOW - 8 * 86400,
            qa_score=40,    # 低质检分
            now=self.NOW,
        )
        assert score_bad > score_good

    def test_batch_predict_filters_low(self):
        """batch_predict 不返回 low 风险。"""
        convs = [
            {"conversation_id": "a", "last_ts": self.NOW - 1800,  # 30分钟前
             "display_name": "A", "platform": "wapp"},
            {"conversation_id": "b", "last_ts": self.NOW - 15*86400,
             "display_name": "B", "platform": "line",
             "last_text": "取消订单", "last_dir": "in"},
        ]
        results = ChurnPredictor().batch_predict(
            convs, silence_threshold_days=7, now=self.NOW
        )
        cids = [r["conversation_id"] for r in results]
        assert "a" not in cids   # 近期活跃，low
        assert "b" in cids       # 高风险

    def test_batch_sorted_by_score(self):
        """batch 结果按 risk_score 降序。"""
        convs = [
            {"conversation_id": "x", "last_ts": self.NOW - 8*86400,
             "last_text": "我要投诉！退款！找律师！", "last_dir": "in",
             "display_name": "X", "platform": "tg"},
            {"conversation_id": "y", "last_ts": self.NOW - 8*86400,
             "last_text": "好的", "last_dir": "in",
             "display_name": "Y", "platform": "tg"},
        ]
        results = ChurnPredictor().batch_predict(
            convs, silence_threshold_days=7, now=self.NOW
        )
        if len(results) >= 2:
            assert results[0]["risk_score"] >= results[1]["risk_score"]
