"""P2 主动话题调度：plan_proactive_sends 决策 + CompanionProactiveLoop 派发。"""

from __future__ import annotations

import time

import pytest

from src.integrations.companion_proactive import (
    CompanionProactiveLoop,
    JsonCooldownStore,
    _in_quiet_hours,
    plan_proactive_sends,
)

# 固定一个白天时间戳（避开安静时段，使测试不受真实时钟影响）。
# 2026-06-19 是周五；选当天 14:00 本地时间。
_NOON = time.mktime(time.struct_time((2026, 6, 19, 14, 0, 0, 4, 170, -1)))
_H = 3600.0


def _opener_follow_up(*, memory_key, silent_hours, stage, intimacy, **_kw):
    if not memory_key:
        return {"mode": "", "directive": ""}
    return {"mode": "follow_up", "directive": f"回访 {memory_key}", "fact": "x"}


def _conv(cid, *, last_ts, direction="out", archived=False, memory_key="u:1"):
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "8244899900",
        "chat_key": "123", "last_ts": last_ts, "last_direction": direction,
        "archived": archived, "memory_key": memory_key, "stage": "warming",
        "intimacy": 40.0,
    }


# ── 安静时段 ────────────────────────────────────────────────────────────────

def test_quiet_hours_wrap_midnight():
    assert _in_quiet_hours(23, 23, 8) is True
    assert _in_quiet_hours(3, 23, 8) is True
    assert _in_quiet_hours(8, 23, 8) is False
    assert _in_quiet_hours(14, 23, 8) is False


def test_quiet_hours_same_day_window():
    assert _in_quiet_hours(13, 12, 14) is True
    assert _in_quiet_hours(14, 12, 14) is False


# ── 决策：基础放行 ────────────────────────────────────────────────────────────

def test_silent_long_enough_plans_send():
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert len(plans) == 1
    assert plans[0]["conversation_id"] == "c1"
    assert plans[0]["mode"] == "follow_up"
    assert plans[0]["silent_hours"] == pytest.approx(48.0, abs=0.5)


def test_not_silent_enough_skipped():
    convs = [_conv("c1", last_ts=_NOON - 2 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert plans == []


# ── 决策：护栏 ────────────────────────────────────────────────────────────────

def test_inbound_last_is_skipped():
    """对方最后发言（我欠回复）→ 不主动开场。"""
    convs = [_conv("c1", last_ts=_NOON - 48 * _H, direction="in")]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert plans == []


def test_archived_skipped():
    convs = [_conv("c1", last_ts=_NOON - 48 * _H, archived=True)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert plans == []


def test_cooldown_blocks_recent_proactive():
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    # 12 小时前刚主动开过场 → 冷却（默认 72h）内不再发
    plans = plan_proactive_sends(
        convs, cooldown_map={"c1": _NOON - 12 * _H},
        opener_fn=_opener_follow_up, now=_NOON)
    assert plans == []


def test_cooldown_expired_allows_send():
    convs = [_conv("c1", last_ts=_NOON - 100 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={"c1": _NOON - 96 * _H},
        opener_fn=_opener_follow_up, now=_NOON, cooldown_hours=72.0)
    assert len(plans) == 1


def test_no_opener_directive_skipped():
    # memory_key 为空 → opener 返回 mode=""，不开场
    convs = [_conv("c1", last_ts=_NOON - 48 * _H, memory_key="")]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert plans == []


def test_pending_care_conversation_skipped():
    """已被 proactive_care(Phase O) 排了关怀的会话 → 主动话题让路（去重）。"""
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON,
        has_pending_care=lambda cid: cid == "c1")
    assert plans == []


def test_pending_care_predicate_error_does_not_block():
    """has_pending_care 抛错 → 当作无待发关怀，照常放行（不误伤）。"""
    def _boom(cid):
        raise RuntimeError("care store down")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON,
        has_pending_care=_boom)
    assert len(plans) == 1


# ── 危机关怀升级（Phase ④续⁸）────────────────────────────────────────────────

def _opener_crisis_block(*, memory_key, silent_hours, stage, intimacy, **_kw):
    """模拟情绪护栏：severe 近期危机 → mode 空 + blocked=crisis_severe。"""
    return {"mode": "", "directive": "", "blocked": "crisis_severe"}


def test_crisis_block_escalates_to_care_and_not_sent():
    """severe 被护栏拦下：不进发送计划，但回调被调用（排进 care）。"""
    escalated = []
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_crisis_block, now=_NOON,
        on_crisis_block=lambda c: escalated.append(c["conversation_id"]))
    assert plans == []
    assert escalated == ["c1"]


def test_crisis_block_without_callback_just_skips():
    """未提供 on_crisis_block → 仅静默跳过（向后兼容，不报错）。"""
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_crisis_block, now=_NOON)
    assert plans == []


def test_normal_opener_does_not_escalate():
    """普通可发送会话 → 不触发危机升级回调。"""
    escalated = []
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON,
        on_crisis_block=lambda c: escalated.append(c["conversation_id"]))
    assert len(plans) == 1
    assert escalated == []


def test_crisis_block_callback_error_does_not_break_plan():
    """on_crisis_block 抛错 → 吞掉，不影响其余会话计划。"""
    def _boom(c):
        raise RuntimeError("care store down")
    convs = [
        _conv("c1", last_ts=_NOON - 48 * _H, memory_key="crisis"),
        _conv("c2", last_ts=_NOON - 36 * _H),
    ]

    def _opener(*, memory_key, silent_hours, stage, intimacy, **_kw):
        if memory_key == "crisis":
            return {"mode": "", "directive": "", "blocked": "crisis_severe"}
        return {"mode": "follow_up", "directive": "x", "fact": "y"}

    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener, now=_NOON,
        on_crisis_block=_boom)
    # c1 升级失败被吞，c2 正常计划
    assert [p["conversation_id"] for p in plans] == ["c2"]


def test_last_emotion_threaded_to_opener():
    """Phase ④续⁹：快照 last_emotion 透传进 opener_fn（供情绪护栏判低谷 soft 抑制）。"""
    seen = {}

    def _opener(*, memory_key, silent_hours, stage, intimacy, last_emotion="", **_kw):
        seen["last_emotion"] = last_emotion
        return {"mode": "follow_up", "directive": "x", "fact": "y"}

    conv = _conv("c1", last_ts=_NOON - 48 * _H)
    conv["last_emotion"] = "焦虑"
    plans = plan_proactive_sends(
        [conv], cooldown_map={}, opener_fn=_opener, now=_NOON)
    assert len(plans) == 1
    assert seen["last_emotion"] == "焦虑"


def test_last_emotion_defaults_empty_when_absent():
    """快照无 last_emotion 字段 → 透传空串，不报错。"""
    seen = {}

    def _opener(*, memory_key, silent_hours, stage, intimacy, last_emotion="x", **_kw):
        seen["last_emotion"] = last_emotion
        return {"mode": "follow_up", "directive": "x", "fact": "y"}

    plans = plan_proactive_sends(
        [_conv("c1", last_ts=_NOON - 48 * _H)],
        cooldown_map={}, opener_fn=_opener, now=_NOON)
    assert len(plans) == 1
    assert seen["last_emotion"] == ""


def test_context_facts_passthrough_and_sanitized():
    """opener 的 context_facts 透传进 plan，并过滤空白项。"""
    def _opener_rich(*, memory_key, silent_hours, stage, intimacy, **_kw):
        return {
            "mode": "follow_up", "directive": "回访", "fact": "在备考",
            "context_facts": ["养了只猫", "  ", "下月搬家"],
        }
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_rich, now=_NOON)
    assert plans[0]["context_facts"] == ["养了只猫", "下月搬家"]


def test_context_facts_default_empty_when_opener_omits():
    """opener 不带 context_facts（如旧实现）→ plan 给空 list，不报错。"""
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON)
    assert plans[0]["context_facts"] == []


def test_max_per_tick_and_longest_silence_first():
    convs = [
        _conv("c1", last_ts=_NOON - 30 * _H),
        _conv("c2", last_ts=_NOON - 90 * _H),
        _conv("c3", last_ts=_NOON - 60 * _H),
    ]
    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener_follow_up, now=_NOON,
        max_per_tick=2)
    assert [p["conversation_id"] for p in plans] == ["c2", "c3"]


# ── 循环 + 冷却存储 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_run_once_sends_and_marks_cooldown(tmp_path):
    sent_calls = []

    async def _send(plan):
        sent_calls.append(plan["conversation_id"])
        return True

    store = JsonCooldownStore(tmp_path / "cd.json")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener_follow_up,
        send_fn=_send,
        cooldown_store=store,
        now=lambda: _NOON,
    )
    res = await loop.run_once()
    assert res == {"planned": 1, "sent": 1}
    assert sent_calls == ["c1"]
    # 冷却已记录 → 再跑一次不再发
    res2 = await loop.run_once()
    assert res2 == {"planned": 0, "sent": 0}


@pytest.mark.asyncio
async def test_loop_dry_run_does_not_call_send(tmp_path):
    async def _send(plan):
        raise AssertionError("dry_run 不应真正发送")

    store = JsonCooldownStore(tmp_path / "cd.json")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener_follow_up,
        send_fn=_send,
        cooldown_store=store,
        dry_run=True,
        now=lambda: _NOON,
    )
    res = await loop.run_once()
    # dry_run 仍计入 sent（标记冷却），但不调用真实 send_fn
    assert res == {"planned": 1, "sent": 1}


@pytest.mark.asyncio
async def test_loop_send_failure_no_cooldown(tmp_path):
    async def _send(plan):
        return False  # 发送失败

    store = JsonCooldownStore(tmp_path / "cd.json")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener_follow_up,
        send_fn=_send,
        cooldown_store=store,
        now=lambda: _NOON,
    )
    res = await loop.run_once()
    assert res == {"planned": 1, "sent": 0}
    # 失败不记冷却 → 下次仍会重试
    assert store.snapshot() == {}


@pytest.mark.asyncio
async def test_loop_passes_pending_care_predicate(tmp_path):
    """循环把 has_pending_care 透传给计划：已排关怀的会话本轮不发。"""
    async def _send(plan):
        raise AssertionError("已排关怀会话不应发送")

    store = JsonCooldownStore(tmp_path / "cd.json")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener_follow_up,
        send_fn=_send,
        cooldown_store=store,
        has_pending_care=lambda cid: True,
        now=lambda: _NOON,
    )
    res = await loop.run_once()
    assert res == {"planned": 0, "sent": 0}


@pytest.mark.asyncio
async def test_loop_threads_crisis_block_callback(tmp_path):
    """循环把 on_crisis_block 透传给计划：severe 会话不发，但回调被触发。"""
    async def _send(plan):
        raise AssertionError("severe 会话不应发送")

    escalated = []
    store = JsonCooldownStore(tmp_path / "cd.json")
    convs = [_conv("c1", last_ts=_NOON - 48 * _H)]
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener_crisis_block,
        send_fn=_send,
        cooldown_store=store,
        on_crisis_block=lambda c: escalated.append(c["conversation_id"]),
        now=lambda: _NOON,
    )
    res = await loop.run_once()
    assert res == {"planned": 0, "sent": 0}
    assert escalated == ["c1"]


def test_json_cooldown_store_persists(tmp_path):
    p = tmp_path / "cd.json"
    s1 = JsonCooldownStore(p)
    s1.mark("c1", 123.0)
    s2 = JsonCooldownStore(p)  # 重新加载
    assert s2.snapshot().get("c1") == 123.0
