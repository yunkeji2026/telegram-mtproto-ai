"""人性化注入门禁：口头禅 / 幽默 / 脾气 / 亲密脏话 是否被正确注入 + 受关系阶段闸控。

覆盖不变量：
  - quirks(口头禅) / humor / emoji_level:high 无条件注入（真人感基础，之前是死数据）。
  - temperament(脾气) 与 banter_profanity(口头脏话) **仅 intimate/steady 阶段**放开；
    生人/暖场阶段收着 / 完全不放开（防一上来就没大没小 / 骂人）。
  - banter 放开时，硬红线文案（不人身攻击 / 对方难过立即收起）必须一并注入。
纯函数式：直接调 PersonaManager._format_persona_instructions（构造轻量、无 IO 依赖）。
"""
import pytest

from src.utils.persona_manager import PersonaManager


def _fmt(persona, stage=""):
    pm = PersonaManager()
    return pm._format_persona_instructions(persona, funnel_stage=stage)


_PERSONA = {
    "name": "林小雨",
    "role": "大学生",
    "personality": {
        "traits": ["活泼"],
        "style": "轻松",
        "emoji_level": "high",
        "quirks": '喜欢说"哇！""啊对对对"',
        "humor": "爱自嘲、会玩梗",
        "temperament": "被冷落会吃醋闹小脾气",
    },
    "speaking": {"language_follow": True, "banter_profanity": True},
    "identity": {"deny_ai": True},
}


def test_quirks_humor_emoji_always_injected():
    out = _fmt(_PERSONA, stage="warming")
    assert "口头禅" in out and "啊对对对" in out
    assert "幽默感" in out and "玩梗" in out
    assert "emoji 用得很多" in out  # emoji_level:high 现已生效


def test_temperament_gated_open_when_intimate():
    out = _fmt(_PERSONA, stage="intimate")
    assert "真实性情" in out
    assert "吃醋" in out or "拌两句嘴" in out  # 放开的脾气描述
    assert "绝不冷暴力" in out


def test_temperament_reined_in_when_not_intimate():
    for stage in ("", "initial", "warming"):
        out = _fmt(_PERSONA, stage=stage)
        assert "真实性情" in out
        assert "先收着点脾气" in out
        # 未熟阶段不注入「放开脾气」的启用文案（区别于 temperament 描述本身）
        assert "可以像真人一样有小情绪" not in out
        assert "绝不冷暴力" not in out


def test_banter_profanity_open_only_intimate():
    out = _fmt(_PERSONA, stage="steady")
    assert "尺度·亲密闲聊" in out
    # 硬红线必须同时注入
    assert "不人身攻击" in out
    assert "情绪低落" in out and "立刻收起" in out


def test_banter_profanity_blocked_when_not_intimate():
    for stage in ("", "initial", "warming"):
        out = _fmt(_PERSONA, stage=stage)
        assert "尺度·亲密闲聊" not in out


def test_banter_requires_optin():
    """未开 banter_profanity 的人设，即使亲密阶段也不放开脏话。"""
    persona = {**_PERSONA, "speaking": {"language_follow": True}}
    out = _fmt(persona, stage="intimate")
    assert "尺度·亲密闲聊" not in out


def test_no_humanization_fields_is_safe():
    """无 quirks/humor/temperament 的极简人设不应崩、也不注入相关段。"""
    out = _fmt({"name": "A", "role": "助手"}, stage="intimate")
    assert "口头禅" not in out
    assert "真实性情" not in out
    assert "尺度·亲密闲聊" not in out
    assert "你是A" in out
