"""Stage O：主动外发文案 prompt 组装（build_proactive_prompt）按 mode 自适应框定。"""

from __future__ import annotations

from src.utils.proactive_prompt import build_proactive_prompt


def test_ritual_morning_framing_not_long_absence():
    p = build_proactive_prompt("小柔", {"mode": "ritual_morning", "directive": "道一句早安"})
    assert "早安" in p
    assert "每天都会惦记" in p
    assert "许久未联系" not in p  # 仪式问候不套「久别重逢」框定（Stage O 修复）
    assert "不超过30字" in p
    assert "道一句早安" in p  # directive 入 prompt
    assert "小柔" in p


def test_ritual_night_framing():
    p = build_proactive_prompt("小柔", {"mode": "ritual_night", "directive": "道一句晚安"})
    assert "晚安" in p
    assert "许久未联系" not in p


def test_milestone_anniversary_framing_not_long_absence():
    p = build_proactive_prompt(
        "小柔", {"mode": "milestone_anniversary", "directive": "认识第100天"})
    assert "特别" in p and "应景" in p
    assert "许久未联系" not in p  # 节点不套「久别重逢」框定（Stage P）
    assert "认识第100天" in p  # 具体场合由 directive 承载


def test_milestone_holiday_framing():
    p = build_proactive_prompt(
        "小柔", {"mode": "milestone_holiday", "directive": "圣诞快乐"})
    assert "许久未联系" not in p
    assert "圣诞快乐" in p


def test_silence_mode_keeps_long_absence_framing():
    p = build_proactive_prompt("小柔", {"mode": "follow_up", "directive": "回访备考"})
    assert "许久未联系" in p
    assert "不超过40字" in p
    assert "回访备考" in p


def test_context_facts_block_included():
    p = build_proactive_prompt(
        "她", {"mode": "follow_up", "directive": "x",
               "context_facts": ["养了只猫", "  ", "下月搬家"]})
    assert "养了只猫" in p and "下月搬家" in p
    assert "背景" in p


def test_context_facts_truncated_to_three():
    p = build_proactive_prompt(
        "她", {"mode": "follow_up", "directive": "x",
               "context_facts": ["a", "b", "c", "d"]})
    assert "- d" not in p  # 仅取前 3 条


def test_recent_context_and_few_shot_appended():
    p = build_proactive_prompt(
        "她", {"mode": "ritual_morning", "directive": "早安"},
        recent_context="昨天聊了考试", few_shot_block="\n【风格示范】...\n")
    assert "昨天聊了考试" in p
    assert "【风格示范】" in p


def test_empty_plan_safe():
    p = build_proactive_prompt("", {})
    assert isinstance(p, str) and "她" in p  # ai_name 缺省回落「她」


def test_no_optional_blocks_when_absent():
    p = build_proactive_prompt("她", {"mode": "ritual_night", "directive": "晚安"})
    assert "背景" not in p          # 无 context_facts
    assert "最近的聊天" not in p     # 无 recent_context
    assert "风格示范" not in p       # 无 few_shot
