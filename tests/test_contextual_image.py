"""Stage B：对话上下文「按需生图」——意图判断 / 主体抽取 / prompt 构造（纯函数常驻门禁）。"""

from __future__ import annotations

import pytest

from src.ai.contextual_image import (
    build_llm_prompt_refine_instruction,
    build_object_image_prompt,
    detect_object_image_request,
    extract_image_subject,
    plan_contextual_image,
)


# ── 意图：是否在要「对话里提到的东西」的照片 ──────────────────────────────

@pytest.mark.parametrize("t", [
    "你煮的肯定很好吃,可以拍個照片給我看一下嗎?",  # 日志实录：要"你煮的"食物图
    "你做的蛋糕拍张照给我看看",
    "你买的裙子发张照片来",
])
def test_detect_object_request_positive(t):
    assert detect_object_image_request(t) is True


@pytest.mark.parametrize("t", [
    "",
    "你煮的真好吃",           # 只夸赞、没要图
    "今天天气不错",           # 无物体标记
    "发张照片看看你",          # 要自拍(Stage A)，无"你煮的…"物体标记
    "x" * 350,               # 超长
])
def test_detect_object_request_negative(t):
    assert detect_object_image_request(t) is False


# ── 主体抽取（词表匹配，杜绝把形容词误当主体） ────────────────────────────

def test_extract_subject_from_user_message():
    assert extract_image_subject("你煮的面拍张照给我看") == "面"


def test_extract_subject_longer_word_wins():
    assert extract_image_subject("你做的炒饭拍张照") == "炒饭"  # 不被"饭"抢先


def test_extract_subject_from_history_when_not_in_text():
    # 当前消息没点名主体("你煮的肯定很好吃")，从历史里人设说过的"煮了面"抽
    hist = [
        {"role": "user", "content": "你在干嘛"},
        {"role": "assistant", "content": "我今天煮了面呀，超香的"},
    ]
    assert extract_image_subject("你煮的肯定很好吃,可以拍個照片給我看嗎", hist) == "面"


def test_extract_subject_ignores_adjective_noise():
    # "你煮的肯定很好吃" 不含任何已知主体词 → 抽不到（不会把"肯定"误当主体）
    assert extract_image_subject("你煮的肯定很好吃拍张照", None) == ""


# ── prompt 构造（英文关键词 + 强制 SFW） ──────────────────────────────────

def test_build_prompt_maps_cn_to_en_and_sfw():
    out = build_object_image_prompt("面")
    assert "noodles" in out
    assert "safe-for-work" in out
    assert "photorealistic" in out


def test_build_prompt_fallback_when_unknown_subject():
    out = build_object_image_prompt("章鱼小丸子")  # 无映射 → 用原词
    assert "章鱼小丸子" in out and "safe-for-work" in out
    out2 = build_object_image_prompt("")            # 空 → 中性兜底
    assert "home-cooked dish" in out2


def test_build_prompt_appends_style():
    out = build_object_image_prompt("蛋糕", style="warm daylight, cozy")
    assert "warm daylight, cozy" in out and "cake" in out


# ── 一站式 plan ───────────────────────────────────────────────────────────

def test_plan_contextual_image_end_to_end():
    hist = [{"role": "assistant", "content": "我刚煮了面"}]
    plan = plan_contextual_image("你煮的拍张照给我看嗎", hist)
    assert plan is not None
    assert plan["kind"] == "object"
    assert plan["subject"] == "面"
    assert "noodles" in plan["prompt"]
    assert plan["base_image"] == ""  # 物体图不带人设的脸


def test_plan_returns_none_for_non_request():
    assert plan_contextual_image("你煮的真香啊", None) is None


# ── 可选 LLM 精炼指令（构造，不发起调用） ────────────────────────────────

def test_llm_refine_instruction_includes_recent_turns():
    hist = [{"role": "assistant", "content": "我煮了面"},
            {"role": "user", "content": "看起来好香"}]
    instr = build_llm_prompt_refine_instruction("拍张照给我看", hist)
    assert "我煮了面" in instr
    assert "拍张照给我看" in instr
    assert "ONLY the prompt" in instr  # 约束 LLM 只回 prompt
