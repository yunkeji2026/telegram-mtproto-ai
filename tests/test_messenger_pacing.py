"""W2-D2.4：pacing 引擎单元测试。"""
from __future__ import annotations

import datetime as _dt

import pytest

from src.integrations.messenger_rpa.pacing import (
    PacingResult,
    calc_pacing_delay,
    short_send_threshold_sec,
)


# 固定 jitter 让测试可重复
@pytest.fixture(autouse=True)
def _fix_jitter(monkeypatch):
    monkeypatch.setattr("src.integrations.messenger_rpa.pacing.random.uniform",
                        lambda a, b: 1.0)


def test_short_reply_short_peer_min_delay():
    """短回复 + 短消息 → 大概 3-10 秒"""
    pr = calc_pacing_delay(
        reply_text="嗯～",
        peer_text="在吗",
        relationship_stage="warming",
    )
    assert isinstance(pr, PacingResult)
    assert 3.0 <= pr.delay_sec <= 10.0
    assert pr.typing_indicator is False  # < 8 秒不需要"在输入"


def test_long_reply_increases_delay():
    """长回复要打字更久"""
    short = calc_pacing_delay(
        reply_text="好",
        peer_text="嗯",
        relationship_stage="warming",
    )
    long = calc_pacing_delay(
        reply_text="今天去外面走了走，路过那家咖啡馆想到你之前提过想去，下次约一起？",
        peer_text="嗯",
        relationship_stage="warming",
    )
    assert long.delay_sec > short.delay_sec


def test_long_peer_message_triggers_extra_pause():
    """对方说长篇 → AI 多消化"""
    short_peer = calc_pacing_delay(
        reply_text="好的我懂",
        peer_text="嗯",
        relationship_stage="warming",
    )
    long_peer = calc_pacing_delay(
        reply_text="好的我懂",
        peer_text="今天工作真的太累了，开了三个会议，每个都没有任何结论的产出。"
                  "感觉自己一直坐在会议室里但什么实事都没做成。"
                  "晚上回家路上又跟室友打了好长时间电话吵了一架，"
                  "我感觉这种生活快要撑不下去了，所以才想着跟你说说话。",  # 100+ 字长篇倾诉
        relationship_stage="warming",
    )
    assert long_peer.delay_sec > short_peer.delay_sec + 10  # 至少多 10 秒


def test_very_long_peer_triggers_more_extra():
    medium_peer = calc_pacing_delay(
        reply_text="嗯",
        peer_text="x" * 100,  # 100 字
        relationship_stage="warming",
    )
    very_long_peer = calc_pacing_delay(
        reply_text="嗯",
        peer_text="x" * 250,  # 250 字 → very_long
        relationship_stage="warming",
    )
    assert very_long_peer.delay_sec > medium_peer.delay_sec


def test_intimate_stage_faster():
    """intimate 阶段 → 更快回"""
    intimate = calc_pacing_delay(
        reply_text="今天去那家店了～",
        peer_text="忙啥呢",
        relationship_stage="intimate",
    )
    initial = calc_pacing_delay(
        reply_text="今天去那家店了～",
        peer_text="忙啥呢",
        relationship_stage="initial",
    )
    assert intimate.delay_sec < initial.delay_sec


def test_max_sec_caps_long_replies():
    """max_sec 兜底，不超过配置上限"""
    pr = calc_pacing_delay(
        reply_text="x" * 5000,
        peer_text="x" * 500,
        relationship_stage="warming",
    )
    assert pr.delay_sec <= 180.0  # 默认 max_sec


def test_disabled_returns_zero():
    pr = calc_pacing_delay(
        reply_text="hi",
        peer_text="hi",
        config={"enabled": False},
    )
    assert pr.delay_sec == 0.0
    assert pr.reason == "disabled"


def test_typing_indicator_threshold():
    pr = calc_pacing_delay(
        reply_text="x" * 200,  # 长 → 大概率超 8 秒
        peer_text="嗯",
        relationship_stage="warming",
    )
    assert pr.typing_indicator is True


def test_config_override():
    pr = calc_pacing_delay(
        reply_text="x" * 100,
        peer_text="嗯",
        config={
            "enabled": True,
            "min_sec": 1.0,
            "max_sec": 10.0,
            "thinking_base_sec": 1.0,
            "per_char_typing_sec": 0.01,
            "long_msg_threshold_chars": 80,
            "long_msg_extra_sec": 0,
            "very_long_threshold_chars": 200,
            "very_long_extra_sec": 0,
            "stage_multiplier": {"warming": 1.0},
            "jitter_range": (1.0, 1.0),
            "typing_indicator_min_sec": 100.0,  # 高门槛 → 不指示
            "short_send_threshold_sec": 3.0,
        },
        relationship_stage="warming",
        # 固定到周二下午 14:00（hour_factor=1.0，weekday）— 否则 hour-of-day
        # 默认表会让该断言在凌晨/早晨/周末跑时漂移失败。
        now_dt=_dt.datetime(2026, 4, 28, 14, 0),
    )
    # 1.0 base + 100 * 0.01 = 2 秒 → 限到 max=10
    assert pr.delay_sec == 2.0
    assert pr.typing_indicator is False  # 2 < 100


def test_short_send_threshold_helper():
    assert short_send_threshold_sec({"short_send_threshold_sec": 5.5}) == 5.5
    # 默认值
    assert short_send_threshold_sec(None) == 3.0


# ── W2-D3.3 hour-of-day 因子 ─────────────────────────

def test_hour_multiplier_morning_slows_down():
    """早上 7 点 hour_factor=1.4 → 比 14 点慢"""
    morning = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 7, 30),  # 周二早上 7:30
        relationship_stage="warming",
    )
    afternoon = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 14, 0),  # 同周二下午
        relationship_stage="warming",
    )
    assert morning.hour_factor > afternoon.hour_factor
    assert morning.delay_sec > afternoon.delay_sec


def test_hour_multiplier_evening_speeds_up():
    """晚 8 点 hour_factor=0.85 → 比 14 点快"""
    evening = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 20, 0),  # 周二晚上 20:00
        relationship_stage="warming",
    )
    afternoon = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 14, 0),
        relationship_stage="warming",
    )
    assert evening.hour_factor < afternoon.hour_factor
    assert evening.delay_sec < afternoon.delay_sec


def test_weekend_multiplier_slows_down():
    """周日 14 点 vs 周二 14 点 → 周末慢一点"""
    saturday = calc_pacing_delay(
        reply_text="x" * 30,
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 5, 2, 14, 0),  # 周六
        relationship_stage="warming",
    )
    tuesday = calc_pacing_delay(
        reply_text="x" * 30,
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 14, 0),  # 周二
        relationship_stage="warming",
    )
    assert saturday.hour_factor > tuesday.hour_factor
    assert saturday.delay_sec > tuesday.delay_sec


def test_late_night_quietens_pre_quiet_hours():
    """晚上 22-23 hour_factor 高（"快睡了"），但还没进 quiet_hours gate"""
    late = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 23, 30),
        relationship_stage="warming",
    )
    afternoon = calc_pacing_delay(
        reply_text="hi",
        peer_text="嗯",
        now_dt=_dt.datetime(2026, 4, 28, 14, 0),
        relationship_stage="warming",
    )
    assert late.delay_sec > afternoon.delay_sec
