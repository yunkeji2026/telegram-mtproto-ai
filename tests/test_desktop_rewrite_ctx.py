"""P4.1 AI 重写助手：inbox store 行 → 会话上下文映射（纯函数）单测。

锁定：
  - direction 归一为 in/out
  - 空文本行被丢弃（不让脏数据进 normalize_history）
  - text 缺失时回落 original_text
  - 异常/非 dict 行被跳过、不抛
"""

from __future__ import annotations

from src.web.routes.unified_inbox_desktop_routes import (
    _msgs_from_store_rows, _sla_config,
)


# ── P6 人审 SLA 阈值解析（_sla_config）────────────────────────────────────────
def test_sla_config_defaults():
    assert _sla_config({}) == (300, 900)
    assert _sla_config(None) == (300, 900)


def test_sla_config_custom():
    assert _sla_config({"review_sla_sec": 120, "review_sla_urgent_sec": 600}) == (120, 600)


def test_sla_config_urgent_clamped_to_warn():
    # urgent < warn → 抬到 warn
    assert _sla_config({"review_sla_sec": 600, "review_sla_urgent_sec": 100}) == (600, 600)


def test_sla_config_invalid_falls_back():
    assert _sla_config({"review_sla_sec": "x", "review_sla_urgent_sec": -5}) == (300, 900)


def test_maps_direction_and_text():
    rows = [
        {"direction": "in", "text": "客户问"},
        {"direction": "out", "text": "客服答"},
    ]
    assert _msgs_from_store_rows(rows) == [
        {"direction": "in", "text": "客户问"},
        {"direction": "out", "text": "客服答"},
    ]


def test_drops_empty_text():
    rows = [
        {"direction": "in", "text": "  "},
        {"direction": "in", "text": "有内容"},
        {"direction": "in"},
    ]
    assert _msgs_from_store_rows(rows) == [{"direction": "in", "text": "有内容"}]


def test_falls_back_to_original_text():
    rows = [{"direction": "in", "text": "", "original_text": "原文"}]
    assert _msgs_from_store_rows(rows) == [{"direction": "in", "text": "原文"}]


def test_unknown_direction_defaults_in():
    rows = [{"direction": "weird", "text": "x"}, {"text": "y"}]
    out = _msgs_from_store_rows(rows)
    assert all(m["direction"] == "in" for m in out)
    assert [m["text"] for m in out] == ["x", "y"]


def test_none_and_bad_rows_safe():
    assert _msgs_from_store_rows(None) == []
    assert _msgs_from_store_rows([]) == []
    # 非 dict 行不抛
    assert _msgs_from_store_rows(["bad", 123, {"direction": "in", "text": "ok"}]) == [
        {"direction": "in", "text": "ok"}
    ]
