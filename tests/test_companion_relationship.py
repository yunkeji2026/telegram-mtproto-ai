"""陪伴关系阶段逻辑单测。"""
import pytest

from src.utils.companion_relationship import (
    STAGE_ORDER,
    build_natural_dialogue_prompt_addon,
    build_relationship_prompt_block,
    downgrade_from_user_text,
    get_rel_state,
    reconcile_stage_after_assistant_reply,
)


def test_get_rel_state_per_chat():
    ctx = {}
    a = get_rel_state(ctx, "111")
    b = get_rel_state(ctx, "222")
    a["exchange_count"] = 5
    assert get_rel_state(ctx, "111")["exchange_count"] == 5
    assert get_rel_state(ctx, "222")["exchange_count"] == 0


def test_reconcile_advances_by_threshold():
    cfg = {
        "enabled": True,
        "thresholds": {
            "initial_to_warming_exchanges": 2,
            "warming_to_intimate_exchanges": 5,
            "intimate_to_steady_exchanges": 10,
        },
    }
    st = {"stage": "initial", "exchange_count": 0, "suppress_advance_until": 0}
    st["exchange_count"] = 2
    out = reconcile_stage_after_assistant_reply(st, cfg)
    assert out == "warming"
    st["exchange_count"] = 5
    reconcile_stage_after_assistant_reply(st, cfg)
    assert st["stage"] == "intimate"


def test_downgrade_and_suppress():
    cfg = {"enabled": True, "advance_suppress_after_downgrade": 3}
    st = {"stage": "intimate", "exchange_count": 20, "suppress_advance_until": 0}
    downgrade_from_user_text(st, "你正经点说话", cfg)
    assert st["stage"] == "warming"
    assert st["suppress_advance_until"] == 23
    st["exchange_count"] = 22
    assert reconcile_stage_after_assistant_reply(st, cfg) is None
    st["exchange_count"] = 24
    reconcile_stage_after_assistant_reply(st, cfg)
    assert st["stage"] in STAGE_ORDER


def test_prompt_block_contains_stage():
    cfg = {"enabled": True, "stages": {}}
    st = {"stage": "initial", "exchange_count": 0}
    b = build_relationship_prompt_block(st, cfg, ai_name="Sera")
    assert "初识" in b
    assert "Sera" not in b or "亲昵" in b or "距离" in b
    assert "对话自然化" in b
    assert "先接住" in b


def test_natural_addon_short_user_and_work():
    cfg = {"enabled": True, "natural_dialogue": {"enabled": True, "short_user_chars": 40}}
    st = {"stage": "steady", "exchange_count": 10}
    a = build_natural_dialogue_prompt_addon(st, cfg, user_message="好")
    assert "偏短" in a
    w = build_natural_dialogue_prompt_addon(st, cfg, user_message="帮我看下订单状态")
    assert "事务" in w or "平实" in w


def test_natural_addon_disabled():
    cfg = {"enabled": True, "natural_dialogue": {"enabled": False}}
    st = {"stage": "initial", "exchange_count": 0}
    assert build_natural_dialogue_prompt_addon(st, cfg, user_message="hi") == ""
