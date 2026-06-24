"""桌面壳选择器覆写层（D1 热更新）后端模块单测。

覆盖 src/web/desktop_selectors.py 的纯函数：清洗/类型守卫、文件加载兜底、版本散列、端点响应体。
与 desktop/inject/profiles.js::OVERLAYABLE_KEYS 的契约对齐由 OVERLAYABLE_KEYS 断言守护。
"""

from __future__ import annotations

import json

from src.web.desktop_selectors import (
    OVERLAYABLE_KEYS,
    load_selector_overlay,
    overlay_version,
    selector_profiles_payload,
    _sanitize,
)


def test_sanitize_keeps_whitelisted_strings():
    out = _sanitize({"instagram": {"bubble": ".b", "composer": ".c"}})
    assert out == {"instagram": {"bubble": ".b", "composer": ".c"}}


def test_sanitize_accepts_profiles_wrapper():
    out = _sanitize({"profiles": {"x": {"sendBtn": ".s"}}})
    assert out == {"x": {"sendBtn": ".s"}}


def test_sanitize_bool_type_guard():
    # 布尔字段只收 bool；字符串当布尔填 → 丢弃
    assert _sanitize({"ig": {"canIngest": True}}) == {"ig": {"canIngest": True}}
    assert _sanitize({"ig": {"canIngest": "yes"}}) == {}


def test_sanitize_drops_empty_and_unknown():
    out = _sanitize({"ig": {"bubble": "", "nope": "x", "composer": "  "}})
    # 空串/空白串/未知键全丢 → 该平台无有效字段 → 不出现
    assert out == {}


def test_sanitize_non_dict_returns_empty():
    assert _sanitize(None) == {}
    assert _sanitize([1, 2, 3]) == {}
    assert _sanitize({"ig": "not-a-dict"}) == {}


def test_load_missing_file_returns_empty(tmp_path):
    assert load_selector_overlay(tmp_path / "does_not_exist.json") == {}


def test_load_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_selector_overlay(p) == {}


def test_load_valid_file(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(
        json.dumps({"profiles": {"instagram": {"bubble": ".ig-row", "canIngest": True}}}),
        encoding="utf-8",
    )
    out = load_selector_overlay(p)
    assert out == {"instagram": {"bubble": ".ig-row", "canIngest": True}}


def test_version_stable_and_empty():
    assert overlay_version({}) == "empty"
    v1 = overlay_version({"a": {"bubble": ".x"}})
    v2 = overlay_version({"a": {"bubble": ".x"}})
    v3 = overlay_version({"a": {"bubble": ".y"}})
    assert v1 == v2 and v1 != v3
    assert len(v1) == 16


def test_payload_shape(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"x": {"sendBtn": ".s"}}), encoding="utf-8")
    payload = selector_profiles_payload(p)
    assert payload["ok"] is True
    assert payload["profiles"] == {"x": {"sendBtn": ".s"}}
    assert payload["version"] != "empty"


def test_payload_empty_when_no_file(tmp_path):
    payload = selector_profiles_payload(tmp_path / "none.json")
    assert payload == {"ok": True, "version": "empty", "profiles": {}}


def test_overlayable_keys_contract():
    # 与 profiles.js::OVERLAYABLE_KEYS 对齐的关键字段（任何一端改动需两处同步）
    for key in ("bubble", "bubbleText", "composer", "sendBtn", "canIngest", "supported"):
        assert key in OVERLAYABLE_KEYS
