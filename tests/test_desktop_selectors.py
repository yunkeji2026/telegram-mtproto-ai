"""桌面壳选择器覆写层（D1 热更新）后端模块单测。

覆盖 src/web/desktop_selectors.py 的纯函数：清洗/类型守卫、文件加载兜底、版本散列、端点响应体。
与 desktop/inject/profiles.js::OVERLAYABLE_KEYS 的契约对齐由 OVERLAYABLE_KEYS 断言守护。
"""

from __future__ import annotations

import json

from src.web.desktop_selectors import (
    OVERLAYABLE_KEYS,
    ensure_overlay_file,
    load_selector_overlay,
    overlay_version,
    selector_overlay_path,
    selector_profiles_payload,
    validate_overlay_file,
    _OVERLAY_FILENAME,
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


# ── P1.2 一键热修：覆写文件路径解析 + 模板自播种 ──────────────────────────────
def test_overlay_path_uses_config_dir(tmp_path):
    p = selector_overlay_path(tmp_path)
    assert p == tmp_path / _OVERLAY_FILENAME


def test_overlay_path_fallback_when_no_config_dir():
    # 无 config_dir → 回落仓库默认（绝对路径、文件名正确）
    p = selector_overlay_path(None)
    assert p.name == _OVERLAY_FILENAME and p.is_absolute()


def test_ensure_overlay_file_creates_template(tmp_path):
    target = tmp_path / "cfgdir" / _OVERLAY_FILENAME  # 父目录不存在 → 应被创建
    created = ensure_overlay_file(target)
    assert created is True
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "profiles" in data and data["profiles"] == {}
    assert "_README" in data  # 含说明，引导运营填写


def test_ensure_overlay_file_idempotent_no_overwrite(tmp_path):
    target = tmp_path / _OVERLAY_FILENAME
    target.write_text(
        json.dumps({"profiles": {"telegram": {"composer": ".keep"}}}),
        encoding="utf-8",
    )
    created = ensure_overlay_file(target)
    assert created is False  # 已存在 → 不动
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["profiles"]["telegram"]["composer"] == ".keep"


def test_template_example_ignored_by_sanitize(tmp_path):
    # 模板里的 _README/_example 不应被 sanitize 当成真覆写（profiles 为空 → 空覆写）
    target = tmp_path / _OVERLAY_FILENAME
    ensure_overlay_file(target)
    assert load_selector_overlay(target) == {}


# ── P1.3 校验：给运营显式反馈（解析失败 / 被忽略字段）──────────────────────────
def test_validate_missing_file_is_valid(tmp_path):
    r = validate_overlay_file(tmp_path / "none.json")
    assert r["exists"] is False and r["valid"] is True and r["profiles"] == 0


def test_validate_bad_json(tmp_path):
    p = tmp_path / _OVERLAY_FILENAME
    p.write_text("{bad json,,,}", encoding="utf-8")
    r = validate_overlay_file(p)
    assert r["exists"] is True and r["valid"] is False and r.get("error")


def test_validate_good_file_counts_platforms(tmp_path):
    p = tmp_path / _OVERLAY_FILENAME
    p.write_text(
        json.dumps({"profiles": {
            "telegram": {"composer": ".c"},
            "whatsapp": {"sendBtn": ".s", "bubble": ".b"},
        }}),
        encoding="utf-8",
    )
    r = validate_overlay_file(p)
    assert r["valid"] is True and r["profiles"] == 2
    assert r["platforms"] == ["telegram", "whatsapp"] and r["dropped"] == []


def test_validate_reports_dropped_fields(tmp_path):
    p = tmp_path / _OVERLAY_FILENAME
    p.write_text(
        json.dumps({"profiles": {
            "telegram": {"composer": ".c", "nope": "x", "canIngest": "yes"},
        }}),
        encoding="utf-8",
    )
    r = validate_overlay_file(p)
    # composer 有效；nope 未知键、canIngest 类型错（应为 bool）→ 均进 dropped
    assert r["valid"] is True and r["profiles"] == 1
    assert "telegram.nope" in r["dropped"] and "telegram.canIngest" in r["dropped"]


def test_validate_template_no_false_dropped(tmp_path):
    # 自播种模板：_README/_example 是 meta，不应被算作 dropped；profiles 空 → 0 平台
    target = tmp_path / _OVERLAY_FILENAME
    ensure_overlay_file(target)
    r = validate_overlay_file(target)
    assert r["valid"] is True and r["profiles"] == 0 and r["dropped"] == []
