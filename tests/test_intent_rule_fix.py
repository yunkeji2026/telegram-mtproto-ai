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


def test_time_and_reunion_greetings():
    # 评测难例：时段问候 / 久别问候（非白名单短语）
    for txt in ("早上好", "晚上好呀", "好久不见", "早安", "下午好"):
        assert _intent(txt) == "打招呼", txt


def test_stop_contact_more_phrasings():
    # 评测难例：「不要再发消息了」「勿扰」
    assert _intent("不要再发消息了") == "停止联系"
    assert _intent("勿扰，谢谢") == "停止联系"


def test_complaint_without_anger_words():
    # 评测难例：投诉/不满意但无显性愤怒词
    assert _intent("态度这么差，我要投诉") == "不满/投诉"
    assert _intent("我对你们的处理非常不满意") == "不满/投诉"


def test_low_mood_synonyms_need_comfort():
    # 评测难例：低落/失眠/疲惫 同义词
    assert _intent("心情很低落，什么都不想做") == "需要安抚"
    assert _intent("最近失眠很严重，整个人很疲惫") == "需要安抚"


def test_rule_baseline_meets_target_on_seed():
    report = evaluate_intent(rule_intent_predictor(), threshold=0.85)
    assert report["passed"] is True
    assert report["metrics"]["accuracy"] >= 0.95


def test_rule_baseline_meets_target_on_curated_dataset():
    # 策展集（57 例，含全部规则难例）必须 ≥85%——与 CI eval 门禁同一标尺。
    from src.eval.dataset import load_intent_samples

    samples = load_intent_samples("config/eval/intent_samples.yaml")
    report = evaluate_intent(rule_intent_predictor(), samples, threshold=0.85)
    assert report["passed"] is True, report["metrics"]
