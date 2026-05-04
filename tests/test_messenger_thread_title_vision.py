"""``thread_title_vision`` 单测：JSON 解析 + screencap+裁剪流程。

不调真 vision API，不连真机；adb 二进制流通过 monkeypatch 注入。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from src.integrations.messenger_rpa import thread_title_vision as ttv


# ── parse_title_response ──────────────────────────────────

def test_parse_title_response_strict_json():
    assert ttv.parse_title_response('{"title":"Victor Zan"}') == "Victor Zan"


def test_parse_title_response_with_markdown_fence():
    raw = '```json\n{"title":"佐藤"}\n```'
    assert ttv.parse_title_response(raw) == "佐藤"


def test_parse_title_response_empty_title_means_none():
    """LLM 明确说 'no chat header visible' → 返 None，不当作有效命名。"""
    assert ttv.parse_title_response('{"title":""}') is None


def test_parse_title_response_loose_field_in_garbage():
    raw = 'sure here is the title: {"title":"Jane Doe"} and other stuff'
    assert ttv.parse_title_response(raw) == "Jane Doe"


def test_parse_title_response_plain_one_liner():
    """没 JSON、单行短文本 → 当成 title（兼容偷懒的 LLM）。"""
    assert ttv.parse_title_response("Victor Zan") == "Victor Zan"


def test_parse_title_response_too_long_plain_text_returns_none():
    """整段长文本 → 不当 title，避免误把 description 当人名。"""
    long = "This is a very long sentence that is not a name at all" * 3
    assert ttv.parse_title_response(long) is None


def test_parse_title_response_none_or_empty():
    assert ttv.parse_title_response("") is None
    assert ttv.parse_title_response("   \n  ") is None


def test_parse_title_response_empty_json_object_not_returned_as_title():
    """Regression: LLM 偶发回 "{}" → 之前会被末尾"裸首行"分支返回字面 '{}'，
    最终落到 actual_title 触发 wrong_chat_rollback。修复后应返 None。"""
    assert ttv.parse_title_response("{}") is None
    assert ttv.parse_title_response("```json\n{}\n```") is None
    assert ttv.parse_title_response('{"other_field": 1}') is None
    # 字面 token 防御
    assert ttv.parse_title_response("[]") is None
    assert ttv.parse_title_response("null") is None


def test_parse_title_response_rejects_explanatory_prefix():
    """P3: LLM 偶发解释性前缀输出，不能被当合法 peer 名（会触发 wrong_chat）。"""
    # 拒绝
    assert ttv.parse_title_response("Sure, the chat title is 野末") is None
    assert ttv.parse_title_response("Here's the title: 野末") is None
    assert ttv.parse_title_response("The title is 野末") is None
    assert ttv.parse_title_response("I see the chat header shows 野末") is None
    assert ttv.parse_title_response("Based on the screenshot, 野末") is None
    assert ttv.parse_title_response("Looking at the header: Maipon Senda") is None
    # LLM self-reference 关键词
    assert ttv.parse_title_response("野末 (shown in the header)") is None
    # 注意：放过合法人名（哪怕含部分关键词）—— 比如 "Sure Tanaka" 是真名
    # 但模式锁定 \b(sure)\b 后跟标点或 'is/here'，所以 "Sure Tanaka" 不会被误杀
    assert ttv.parse_title_response("Sure Tanaka") == "Sure Tanaka"
    # 合法人名不被影响
    assert ttv.parse_title_response("野末") == "野末"
    assert ttv.parse_title_response("Maipon Senda") == "Maipon Senda"
    assert ttv.parse_title_response("Victor Zan") == "Victor Zan"


# ── screencap_top_strip ───────────────────────────────────

def _make_test_png_bytes(w: int = 720, h: int = 1600) -> bytes:
    """生成一张可读的纯色 PNG（PIL 内置）。"""
    from PIL import Image
    img = Image.new("RGB", (w, h), color=(40, 80, 120))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_screencap_top_strip_crops_top_band(monkeypatch, tmp_path):
    png = _make_test_png_bytes(720, 1600)

    def _fake_run_adb_binary(args, *, serial, timeout):
        return png, "", 0

    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        _fake_run_adb_binary,
    )
    out = ttv.screencap_top_strip("abc", top_ratio=0.13)
    assert out is not None
    assert out.exists()
    # 验证裁出的图确实更短
    from PIL import Image
    cropped = Image.open(out)
    cw, ch = cropped.size
    assert cw == 720
    # 13% 的 1600 = 208，允许小误差
    assert 200 <= ch <= 215, f"unexpected crop height: {ch}"
    cropped.close()
    out.unlink(missing_ok=True)


def test_screencap_top_strip_returns_none_on_adb_failure(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"", "device offline", 1),
    )
    assert ttv.screencap_top_strip("abc") is None


def test_screencap_top_strip_returns_none_on_non_png_payload(monkeypatch):
    """exec-out screencap 偶尔会返非 PNG 头（adb 协议 race）→ 不当 OK。"""
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"\x00\x00garbage", "", 0),
    )
    assert ttv.screencap_top_strip("abc") is None


# ── read_thread_title_via_vision (集成) ───────────────────

def test_read_thread_title_via_vision_screencap_failed(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"", "no device", 1),
    )
    r = ttv.read_thread_title_via_vision(
        "abc", {"provider": "zhipu", "api_key": "k"},
    )
    assert r.title is None
    assert r.debug == "screencap_failed"


def test_read_thread_title_via_vision_init_failure_no_zhipu(monkeypatch):
    """vision 主端起不来、又没 zhipu key → vision_init_fail_no_zhipu。"""
    png = _make_test_png_bytes()
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (png, "", 0),
    )

    class _DummyVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return False

    monkeypatch.setattr("src.vision_client.VisionClient", _DummyVC)
    r = ttv.read_thread_title_via_vision(
        "abc",
        {"provider": "ollama", "base_url": "http://nowhere"},
        global_vision={},
    )
    assert r.title is None
    assert r.debug in ("vision_init_fail_no_zhipu", "vision_init_fail")


def test_read_thread_title_via_vision_happy(monkeypatch):
    png = _make_test_png_bytes()
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (png, "", 0),
    )

    class _OkVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return True

        def describe_image_sync(self, path, prompt=None):
            return '{"title":"Victor Zan"}'

    monkeypatch.setattr("src.vision_client.VisionClient", _OkVC)
    r = ttv.read_thread_title_via_vision(
        "abc",
        {"provider": "zhipu", "api_key": "k", "model": "glm-4v-flash"},
    )
    assert r.title == "Victor Zan"
    assert r.debug == "ok"
