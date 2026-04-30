from src.integrations.messenger_rpa.lead_qualification import (
    LeadQualificationEngine,
)


def _cfg(**overrides):
    base = {
        "enabled": True,
        "target": {
            "country": "JP",
            "language": "ja",
            "gender": "female",
            "age_min": 37,
            "age_max": 60,
        },
        "min_score_for_line": 80,
        "question_policy": {
            "min_turns_before_age": 4,
            "min_turns_before_budget": 6,
        },
        "handoff": {
            "line_id": "",
            "min_turns_before_send": 6,
            "resend_cooldown_days": 14,
        },
        "low_priority": {
            "score_below": 40,
            "stop_after_low_value_turns": 3,
        },
    }
    base.update(overrides)
    return base


def test_high_value_japanese_female_reaches_line_handoff():
    engine = LeadQualificationEngine(
        _cfg(handoff={"line_id": "@staff_line", "min_turns_before_send": 3})
    )
    profile = {}
    for text in [
        "日本の東京に住んでます。女性です。",
        "45歳で、会社を経営しています。",
        "ちゃんと相談したいです。予算もあります。",
    ]:
        decision = engine.evaluate(
            profile,
            peer_text=text,
            reply_lang="ja",
            chat_name="Akiko",
            now=1000.0,
        )
        profile = decision.profile

    assert decision.action == "handoff_line"
    assert "@staff_line" in decision.forced_reply
    assert decision.score >= 80
    assert decision.profile["stage"] == "line_sent"


def test_no_line_id_keeps_scoring_but_does_not_send_placeholder():
    engine = LeadQualificationEngine(_cfg(handoff={"line_id": ""}))
    profile = {}
    for text in [
        "日本にいます。女性です。",
        "50代で、医師をしています。",
        "詳しく相談したいです。費用は大丈夫です。",
        "お願いします。",
        "時間あります。",
        "話を聞いてください。",
    ]:
        decision = engine.evaluate(profile, peer_text=text, reply_lang="ja")
        profile = decision.profile

    assert decision.score >= 80
    assert decision.action != "handoff_line"
    assert decision.forced_reply == ""


def test_explicit_male_or_under_target_goes_low_priority_then_silent():
    engine = LeadQualificationEngine(
        _cfg(low_priority={"score_below": 40, "stop_after_low_value_turns": 2})
    )
    profile = {}
    d1 = engine.evaluate(profile, peer_text="僕は男です。25歳です。", reply_lang="ja")
    d2 = engine.evaluate(d1.profile, peer_text="無料なら話すよ", reply_lang="ja")

    assert d1.action == "low_priority"
    assert d2.action == "silent_stop"
    assert d2.profile["low_priority_reason"] in {"male_explicit", "age_below_target"}


def test_prompt_selects_occupation_before_age_in_early_turns():
    engine = LeadQualificationEngine(_cfg())
    decision = engine.evaluate({}, peer_text="日本に住んでます。", reply_lang="ja")

    assert decision.result["next_question"] == "occupation"
    assert "普段どんなお仕事" in decision.prompt_block
