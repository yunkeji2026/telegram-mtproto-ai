"""messenger_rpa.escalation —— 纯函数单测。

模块 docstring 明写"容易单元测试"却历来零覆盖。本文件锁 5 类触发器
（human_request / complaint / contract / money / repeat）+ 配置开关 +
边界条件 + 优先级。所有测试 input/output 纯函数，零 I/O。
"""

from __future__ import annotations

import pytest

from src.integrations.messenger_rpa.escalation import (
    EscalationDecision,
    _rough_similarity,
    evaluate,
)


# ───────────────── 基础 / 配置门 ─────────────────


def test_none_factory_returns_non_escalating_decision() -> None:
    d = EscalationDecision.none()
    assert d.should_escalate is False
    assert d.reason == ""
    assert d.human_message == ""


def test_escalation_disabled_config_short_circuits() -> None:
    """顶层 enabled=False 时任何 keyword 都不触发。"""
    d = evaluate(
        peer_text="转人工 refund $500",
        config={"escalation": {"enabled": False}},
    )
    assert d.should_escalate is False


def test_default_config_enables_all_triggers() -> None:
    """config=None 走 enabled=True 默认，human_request 命中应触发。"""
    d = evaluate(peer_text="I want to talk to a human", config=None)
    assert d.should_escalate is True
    assert d.reason.startswith("keyword:human_request:")


def test_empty_peer_text_returns_none() -> None:
    assert evaluate(peer_text="").should_escalate is False
    assert evaluate(peer_text="   ").should_escalate is False


# ───────────────── human_request 触发器 ─────────────────


@pytest.mark.parametrize("text,hit", [
    ("talk to a human please", "talk to a human"),
    ("I need a real person", "real person"),
    ("customer service NOW", "customer service"),
    ("你好 我要人工", "人工"),
    ("请转真人", "真人"),
    ("オペレーター お願いします", "オペレーター"),
])
def test_human_request_keyword_fires(text: str, hit: str) -> None:
    d = evaluate(peer_text=text)
    assert d.should_escalate is True
    assert d.reason == f"keyword:human_request:{hit}"
    assert hit in d.human_message


def test_human_request_disabled_via_config() -> None:
    d = evaluate(
        peer_text="talk to a human",
        config={"escalation": {"keyword_human_request": False}},
    )
    assert d.should_escalate is False


# ───────────────── complaint 触发器 ─────────────────


@pytest.mark.parametrize("text", [
    "I want a refund",
    "File a complaint against your service",
    "cancel order 12345",
    "我要退款",
    "投诉你们客服",  # 注意：投诉先命中，客服也是 human_request 命中；顺序见 priority 测试
])
def test_complaint_keywords_fire(text: str) -> None:
    d = evaluate(peer_text=text)
    assert d.should_escalate is True


def test_complaint_disabled_with_human_request_also_disabled() -> None:
    """只关 complaint 保持 human_request 默认 enabled，refund 不触发。"""
    d = evaluate(
        peer_text="refund please",
        config={"escalation": {"keyword_complaint": False}},
    )
    assert d.should_escalate is False


# ───────────────── contract 触发器 ─────────────────


@pytest.mark.parametrize("text,expect_hit", [
    ("I'll get my lawyer involved", "lawyer"),
    ("Read the contract carefully", "contract"),
    ("请看合同第三条", "合同"),
    ("律师函准备好了", "律师"),
])
def test_contract_keywords_fire(text: str, expect_hit: str) -> None:
    d = evaluate(peer_text=text)
    assert d.should_escalate is True
    assert d.reason == f"keyword:contract:{expect_hit}"


def test_contract_disabled_via_config() -> None:
    d = evaluate(
        peer_text="see the contract",
        config={"escalation": {"keyword_contract": False}},
    )
    assert d.should_escalate is False


# ───────────────── money_mention 触发器 ─────────────────


@pytest.mark.parametrize("text", [
    "the price is $100",
    "¥500 for this",
    "€1,200.50 per month",
    "500 USD total",
    "1000 人民币 押金",
    "500元",
])
def test_money_pattern_fires(text: str) -> None:
    d = evaluate(peer_text=text)
    assert d.should_escalate is True
    assert d.reason.startswith("money_mention:")


@pytest.mark.parametrize("text", [
    "just saying hi",
    "the weather is fine",
    "我来问个问题",
    "5 minutes please",  # 数字但无货币词
])
def test_non_money_text_does_not_fire_money_rule(text: str) -> None:
    d = evaluate(
        peer_text=text,
        config={"escalation": {
            # 只关 money 之前的 3 个关键词类，避免撞车
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
        }},
    )
    assert d.should_escalate is False


def test_money_disabled_via_config() -> None:
    d = evaluate(
        peer_text="pay $500 now",
        config={"escalation": {"money_mention": False}},
    )
    assert d.should_escalate is False


# ───────────────── repeat 触发器 ─────────────────


def test_repeat_detection_fires_on_similar_peers() -> None:
    """repeat_threshold=3：历史 2 条 + 当前 1 条 = 3 条高度相似 → 触发。"""
    d = evaluate(
        peer_text="what is the ETA for my order abc123",
        recent_peer_texts=[
            "ETA for my order abc123 please",
            "where is my order abc123",
        ],
        config={"escalation": {
            # 关掉其它触发器避免干扰
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
            "money_mention": False,
            "repeat_threshold": 3,
        }},
    )
    assert d.should_escalate is True
    assert d.reason == "repeat:unresolved"


def test_repeat_detection_threshold_zero_disables() -> None:
    d = evaluate(
        peer_text="same question",
        recent_peer_texts=["same question", "same question"],
        config={"escalation": {
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
            "money_mention": False,
            "repeat_threshold": 0,
        }},
    )
    assert d.should_escalate is False


def test_repeat_detection_dissimilar_peers_does_not_fire() -> None:
    d = evaluate(
        peer_text="completely new question about shipping",
        recent_peer_texts=[
            "hello there",
            "how are you",
        ],
        config={"escalation": {
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
            "money_mention": False,
            "repeat_threshold": 3,
        }},
    )
    assert d.should_escalate is False


def test_repeat_detection_insufficient_history_no_fire() -> None:
    """历史只有 1 条，凑不够 threshold-1 个相似 → 不触发。"""
    d = evaluate(
        peer_text="order abc123 status",
        recent_peer_texts=["order abc123 status"],
        config={"escalation": {
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
            "money_mention": False,
            "repeat_threshold": 3,
        }},
    )
    assert d.should_escalate is False


def test_repeat_detection_no_history_no_fire() -> None:
    d = evaluate(peer_text="question",
                 recent_peer_texts=None,
                 config={"escalation": {
                     "keyword_human_request": False,
                     "keyword_complaint": False,
                     "keyword_contract": False,
                     "money_mention": False,
                 }})
    assert d.should_escalate is False


# ───────────────── 优先级顺序 ─────────────────


def test_human_request_wins_over_complaint() -> None:
    """文本同时命中 human_request 和 complaint 时，human_request 先判定。"""
    d = evaluate(peer_text="customer service for refund")
    assert d.reason.startswith("keyword:human_request:")


def test_complaint_wins_over_money() -> None:
    """refund + $500：complaint 先判定（money 在后）。"""
    d = evaluate(
        peer_text="refund $500 now",
        config={"escalation": {"keyword_human_request": False}},
    )
    assert d.reason.startswith("keyword:complaint:")


def test_money_wins_over_repeat() -> None:
    """peer_text 同时 money 命中 + 与历史相似 → money 先判定。"""
    d = evaluate(
        peer_text="pay $500 now",
        recent_peer_texts=["pay $500 now", "pay $500 now"],
        config={"escalation": {
            "keyword_human_request": False,
            "keyword_complaint": False,
            "keyword_contract": False,
            "repeat_threshold": 3,
        }},
    )
    assert d.reason.startswith("money_mention:")


# ───────────────── _rough_similarity helper ─────────────────


def test_rough_similarity_identical() -> None:
    assert _rough_similarity("hello", "hello") == 1.0


def test_rough_similarity_disjoint() -> None:
    """a={h,i} b={x,y,z}：Jaccard = 0/5 = 0。"""
    assert _rough_similarity("hi", "xyz") == 0.0


def test_rough_similarity_empty_strings_return_zero() -> None:
    assert _rough_similarity("", "anything") == 0.0
    assert _rough_similarity("anything", "") == 0.0
    assert _rough_similarity("", "") == 0.0


def test_rough_similarity_case_insensitive() -> None:
    assert _rough_similarity("HELLO", "hello") == 1.0
