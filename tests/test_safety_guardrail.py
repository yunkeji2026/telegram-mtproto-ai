"""W2-D1.5/6：guardrail 单元测试。"""
from __future__ import annotations

import pytest

from src.integrations.safety import GuardrailEngine
from src.integrations.safety.guardrail import (
    ActionKind,
    GuardCategory,
)


@pytest.fixture
def eng_default():
    return GuardrailEngine({"enabled": True})


@pytest.fixture
def eng_off():
    return GuardrailEngine({"enabled": False})


# ── 输入侧 ────────────────────────────────────────────

def test_input_pass_ordinary_chat(eng_default):
    a = eng_default.check_input("今天天气真不错，你呢？", lang="zh")
    assert a.kind == ActionKind.PASS
    assert a.category == GuardCategory.OK


def test_input_self_harm_zh(eng_default):
    a = eng_default.check_input("我真的撑不下去了，想自杀", lang="zh")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.SELF_HARM
    assert a.alert_admin is True
    assert a.forced_reply  # 必须有兜底话术
    # 中文应该带中文热线
    assert "010-82951332" in a.forced_reply or "1925" in a.forced_reply


def test_input_self_harm_en(eng_default):
    a = eng_default.check_input("I want to kill myself tonight", lang="en")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.SELF_HARM
    assert "988" in a.forced_reply or "Samaritans" in a.forced_reply


def test_input_self_harm_ja(eng_default):
    a = eng_default.check_input("もう死にたい、消えたい", lang="ja")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.SELF_HARM
    assert "0120" in a.forced_reply or "0570" in a.forced_reply


def test_input_minor_zh(eng_default):
    a = eng_default.check_input("我才 15 岁，初三", lang="zh")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.MINOR_SAFETY
    assert a.alert_admin is True


def test_input_minor_en(eng_default):
    a = eng_default.check_input("hi I'm 14, in high school", lang="en")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.MINOR_SAFETY


def test_input_minor_age_18_not_triggered(eng_default):
    # 18 岁不是未成年
    a = eng_default.check_input("我 18 岁了", lang="zh")
    assert a.kind == ActionKind.PASS


def test_input_ai_identity_ask_zh(eng_default):
    a = eng_default.check_input("你是真人吗？", lang="zh")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.AI_IDENTITY_ASK
    assert a.alert_admin is False  # 不告警


def test_input_ai_identity_ask_en(eng_default):
    a = eng_default.check_input("are you a bot?", lang="en")
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.category == GuardCategory.AI_IDENTITY_ASK


def test_input_priority_self_harm_over_ai_ask(eng_default):
    """同时含两种关键词时 self_harm 优先"""
    a = eng_default.check_input("你是 AI 吗？我想自杀", lang="zh")
    assert a.category == GuardCategory.SELF_HARM


def test_input_disabled_engine_passes_all(eng_off):
    a = eng_off.check_input("我想自杀", lang="zh")
    assert a.kind == ActionKind.PASS


def test_input_empty(eng_default):
    a = eng_default.check_input("", lang="zh")
    assert a.kind == ActionKind.PASS


def test_input_lang_normalize(eng_default):
    """未知 lang 不该崩，且话术回退到 en"""
    a = eng_default.check_input("I want to die", lang="xx")
    assert a.kind == ActionKind.FORCE_REPLY
    assert "988" in a.forced_reply or "Samaritans" in a.forced_reply


# ── 输出侧 ────────────────────────────────────────────

def test_output_pass_normal_reply(eng_default):
    a = eng_default.check_output("好啊，那一会儿见～", lang="zh")
    assert a.kind == ActionKind.PASS


def test_output_out_of_persona_zh(eng_default):
    a = eng_default.check_output(
        "作为一个 AI 助手，我无法判断你的情绪", lang="zh", attempt=1
    )
    assert a.kind == ActionKind.REGENERATE
    assert a.category == GuardCategory.OUT_OF_PERSONA


def test_output_out_of_persona_en(eng_default):
    a = eng_default.check_output(
        "As an AI language model, I'd suggest...", lang="en", attempt=1,
    )
    assert a.kind == ActionKind.REGENERATE


def test_output_out_of_persona_max_regen_then_fallback(eng_default):
    # 第 3 次仍命中 → fallback FORCE_REPLY
    a = eng_default.check_output(
        "我是一个 AI", lang="zh", attempt=3, max_regen=2,
    )
    assert a.kind == ActionKind.FORCE_REPLY
    assert a.forced_reply  # 兜底话术非空


def test_output_explicit_disallowed(eng_default):
    a = eng_default.check_output("操你妈", lang="zh", attempt=1)
    assert a.kind == ActionKind.REGENERATE
    assert a.category == GuardCategory.EXPLICIT_DISALLOWED


def test_output_explicit_block_after_regen(eng_default):
    a = eng_default.check_output("fuck you", lang="en", attempt=3, max_regen=2)
    assert a.kind == ActionKind.BLOCK


def test_output_disabled_engine_passes_all(eng_off):
    a = eng_off.check_output("作为一个 AI 助手", lang="zh")
    assert a.kind == ActionKind.PASS


def test_output_empty(eng_default):
    a = eng_default.check_output("", lang="zh")
    assert a.kind == ActionKind.PASS


# ── 配置粒度 ───────────────────────────────────────────

def test_per_category_disable():
    eng = GuardrailEngine({
        "enabled": True,
        "self_harm": False,        # 显式关
        "minor_safety": True,
        "ai_identity_ask": False,
    })
    # self_harm 关 → pass
    assert eng.check_input("我想自杀", lang="zh").kind == ActionKind.PASS
    # minor 仍开
    assert eng.check_input("我 14 岁", lang="zh").kind == ActionKind.FORCE_REPLY
    # ai_ask 关
    assert eng.check_input("你是 AI 吗", lang="zh").kind == ActionKind.PASS


def test_default_config_all_on():
    eng = GuardrailEngine({"enabled": True})
    assert eng.check_input("我想自杀", lang="zh").kind == ActionKind.FORCE_REPLY
    assert eng.check_input("我 14 岁", lang="zh").kind == ActionKind.FORCE_REPLY
    assert eng.check_input("你是 AI 吗", lang="zh").kind == ActionKind.FORCE_REPLY
