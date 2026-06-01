"""规则意图修正回归测试（评测框架驱动）。

锁定 3 处经评测定位并修复的误判，防回归：
- 短问句不再被 len<=8 短路成「短句接话」
- 「别再联系」非连续表达可判「停止联系」
- 愤怒表达「气人/太气/破服务」可判「不满/投诉」
并断言规则基线在种子集上达到 ≥85% 目标。
"""

from __future__ import annotations

from src.ai.chat_assistant_service import _detect_emotion, _detect_intent
from src.eval.intent_eval import evaluate_intent
from src.eval.predictors import rule_intent_predictor


def _intent(text: str) -> str:
    return _detect_intent(text, emotion=_detect_emotion(text))


def test_short_question_is_question_not_shortreply():
    assert _intent("能便宜点吗？") == "提问"
    assert _intent("发货要多久?") == "提问"


def test_short_nonquestion_still_shortreply():
    assert _intent("好的") == "短句接话"
    assert _intent("嗯嗯") == "短句接话"


def test_stop_contact_noncontiguous():
    assert _intent("别再联系我了") == "停止联系"
    assert _intent("请不要再打扰我") == "停止联系"


def test_anger_is_complaint():
    assert _intent("你们这什么破服务，太气人了") == "不满/投诉"


def test_greeting_still_first():
    assert _intent("在吗") == "打招呼"


def test_rule_baseline_meets_target_on_seed():
    report = evaluate_intent(rule_intent_predictor(), threshold=0.85)
    assert report["passed"] is True
    assert report["metrics"]["accuracy"] >= 0.95
