"""R1 情感弧线 ↔ 应对策略去重：build_emotion_arc_hint(strategy_active=...) 行为。

策略开启时，arc 只保留"跨情绪转折"独有指引，当前情绪应对让给策略块（省 token、不重复
叮嘱）；策略关闭时回退完整指引，保证不丢情绪引导。集成层验证负向首轮不再两块重复。
"""

from __future__ import annotations

from src.utils.emotional_context import (
    build_emotion_arc_hint,
    build_emotional_context_block,
)


def _emo(emotion="sad", valence=-0.5, dimension="negative", intensity=0.5):
    return {
        "primary_emotion": emotion,
        "valence": valence,
        "dimension": dimension,
        "primary_intensity": intensity,
        "arousal": 0.3,
    }


# ── strategy_active=True：只留转折，当前情绪应对交策略块 ──────────────────

def test_no_history_negative_silent_when_strategy_active():
    # 首轮负向、策略开 → arc 不再重复"先共情"，交给策略块
    assert build_emotion_arc_hint(_emo(), "", 0.0, strategy_active=True) == ""


def test_no_history_low_energy_silent_when_strategy_active():
    e = _emo(emotion="tired", valence=-0.1, dimension="low_energy")
    assert build_emotion_arc_hint(e, "", 0.0, strategy_active=True) == ""


def test_plain_negative_silent_when_strategy_active():
    # 有历史但非转折（负→负，未恶化）→ 策略开则静默
    assert build_emotion_arc_hint(_emo(valence=-0.4), "sad", -0.4, strategy_active=True) == ""


def test_worsening_within_negative_silent_when_strategy_active():
    # 负向恶化：策略块会选 validate，arc 不再重复
    assert build_emotion_arc_hint(_emo(valence=-0.6), "sad", -0.1, strategy_active=True) == ""


# ── 转折独有价值：无论策略是否开都保留 ──────────────────────────────────

def test_improving_transition_kept_when_strategy_active():
    e = _emo(emotion="happy", valence=0.4, dimension="positive")
    out = build_emotion_arc_hint(e, "sad", -0.5, strategy_active=True)
    assert "好多了" in out


def test_pos_to_neg_transition_kept_when_strategy_active():
    out = build_emotion_arc_hint(_emo(valence=-0.4), "happy", 0.5, strategy_active=True)
    assert "怎么了" in out


# ── strategy_active=False：回退完整指引（向后兼容） ──────────────────────

def test_no_history_negative_full_when_strategy_off():
    out = build_emotion_arc_hint(_emo(), "", 0.0, strategy_active=False)
    assert "心情不太好" in out


def test_plain_negative_full_when_strategy_off():
    out = build_emotion_arc_hint(_emo(valence=-0.4), "sad", -0.4, strategy_active=False)
    assert "先共情" in out


def test_default_strategy_active_false_keeps_legacy():
    # 默认参数（不传 strategy_active）= 旧行为，首轮负向给完整指引
    out = build_emotion_arc_hint(_emo(), "", 0.0)
    assert "心情不太好" in out


# ── 集成：负向首轮不再 arc+策略 两块重复 ────────────────────────────────

def test_integration_negative_first_turn_no_dup_arc():
    block = build_emotional_context_block("好烦啊烦死了！", {"reply_count": 3}, "")
    # 策略块在；情感感知不再为当前负向情绪重复叮嘱
    assert "应对策略" in block
    assert "【情感感知】" not in block


def test_integration_strategy_off_keeps_arc():
    block = build_emotional_context_block(
        "好烦啊烦死了！", {"reply_count": 3}, "", enable_strategy=False
    )
    assert "应对策略" not in block
    assert "【情感感知】" in block
