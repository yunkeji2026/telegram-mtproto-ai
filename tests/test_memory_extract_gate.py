"""Phase D：陪伴记忆闭环激活 —— 抽取意图闸 should_extract_intent 单测。

修复「记忆闭环未激活」：extract 默认开但 intents 空 → 永不抽。新增 match_all 显式开关
让陪伴产品「全记」，同时保持存量部署（未配置/空 intents）零回归。
"""
import pytest

from src.skills.skill_manager import CHAT_FAMILY_INTENTS, should_extract_intent


# ── 零回归：未配置/空 intents 且未开 match_all → 不抽（与历史行为一致）─────────

def test_missing_intents_skips():
    assert should_extract_intent("small_talk", {}) is False
    assert should_extract_intent("small_talk", {"enabled": True}) is False

def test_empty_intents_skips():
    assert should_extract_intent("small_talk", {"intents": []}) is False


# ── 白名单 ───────────────────────────────────────────────────────────────────

def test_whitelist_match():
    cfg = {"intents": ["small_talk", "greeting"]}
    assert should_extract_intent("small_talk", cfg) is True
    assert should_extract_intent("greeting", cfg) is True
    assert should_extract_intent("order_query", cfg) is False


# ── match_all：陪伴「全记」开关 ──────────────────────────────────────────────

def test_match_all_extracts_any_intent():
    cfg = {"match_all": True}
    for intent in ("small_talk", "order_query", "anything", "complaint"):
        assert should_extract_intent(intent, cfg) is True

def test_match_all_overrides_empty_intents():
    # match_all 优先于白名单（即便 intents 空）
    assert should_extract_intent("whatever", {"match_all": True, "intents": []}) is True


# ── 陪伴预设的 chat-family 全部可抽 ─────────────────────────────────────────

def test_companion_chat_family_all_extractable():
    cfg = {"intents": list(CHAT_FAMILY_INTENTS)}
    for intent in CHAT_FAMILY_INTENTS:
        assert should_extract_intent(intent, cfg) is True


def test_chat_family_constant_matches_p0g():
    # 与 skill_manager P0-G 的 chat family 一致（防漂移）
    assert CHAT_FAMILY_INTENTS == frozenset({
        "greeting", "small_talk", "direct_chat", "casual_chat", "chitchat", "free_chat",
    })
