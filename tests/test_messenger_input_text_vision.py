"""``input_text_vision`` 模块单测：JSON 解析 + screencap 裁剪 + 集成路径。"""
from __future__ import annotations

from io import BytesIO

from src.integrations.messenger_rpa import input_text_vision as itv


# ── parse_input_text_response ─────────────────────────────

def test_parse_input_text_response_strict_json():
    assert itv.parse_input_text_response(
        '{"text":"今天天气怎么样"}'
    ) == "今天天气怎么样"


def test_parse_input_text_response_empty_string_means_hint_state():
    """LLM 明确返 text=""——表示输入框是 hint（'Message'）状态。"""
    # 不同于 None：空串是合法的"输入框为空"语义
    assert itv.parse_input_text_response('{"text":""}') == ""


def test_parse_input_text_response_with_markdown_fence():
    raw = '```json\n{"text":"hi there"}\n```'
    assert itv.parse_input_text_response(raw) == "hi there"


def test_parse_input_text_response_loose_field_in_garbage():
    raw = 'Sure, the input shows {"text":"嗨~刚才在忙"} and emoji icons.'
    assert itv.parse_input_text_response(raw) == "嗨~刚才在忙"


def test_parse_input_text_response_none_on_empty_string():
    assert itv.parse_input_text_response("") is None
    assert itv.parse_input_text_response("   \n  ") is None


def test_parse_input_text_response_treats_plain_line_as_typed_text():
    """新 prompt 协议：单行裸文本就是输入框文字（除非含 meta 词）。

    flash/plus 在某些情况下不返 EMPTY 也不返 JSON，直接返"在输入框看到的文字"——
    必须当成有效输入而不是 garbage。
    """
    assert (
        itv.parse_input_text_response("Just some random sentence")
        == "Just some random sentence"
    )
    assert itv.parse_input_text_response("hi there") == "hi there"
    assert itv.parse_input_text_response('"今天天气"') == "今天天气"   # 去 quote


def test_parse_input_text_response_rejects_meta_explanation():
    """LLM "解释"模式：含 'input field' 等元描述词 → 视为模型在描述而非输入。"""
    assert itv.parse_input_text_response(
        "The input field is empty showing only Message"
    ) is None
    assert itv.parse_input_text_response(
        "I can see the composer at the bottom"
    ) is None


def test_parse_input_text_response_rejects_too_long_paragraph():
    """超长输出 → 模型在描述而非给输入文字。"""
    long = "This is a long explanation about what I see in the screenshot " * 5
    assert itv.parse_input_text_response(long) is None


# ── screencap_bottom_strip ────────────────────────────────

def _make_test_png(w: int = 720, h: int = 1600) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (w, h), color=(20, 60, 100))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_screencap_bottom_strip_crops_bottom_band(monkeypatch):
    png = _make_test_png(720, 1600)
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (png, "", 0),
    )
    out = itv.screencap_bottom_strip("abc", bottom_ratio=0.30)
    assert out is not None
    assert out.exists()
    from PIL import Image
    cropped = Image.open(out)
    cw, ch = cropped.size
    assert cw == 720
    # 30% 的 1600 = 480
    assert 470 <= ch <= 490, f"unexpected crop height: {ch}"
    cropped.close()
    out.unlink(missing_ok=True)


def test_screencap_bottom_strip_returns_none_on_adb_failure(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"", "device offline", 1),
    )
    assert itv.screencap_bottom_strip("abc") is None


def test_screencap_bottom_strip_rejects_non_png(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"\x00\x00garbage", "", 0),
    )
    assert itv.screencap_bottom_strip("abc") is None


# ── read_input_text_via_vision (集成) ─────────────────────

def test_read_input_text_via_vision_screencap_failed(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (b"", "no device", 1),
    )
    r = itv.read_input_text_via_vision(
        "abc", {"provider": "zhipu", "api_key": "k"},
    )
    assert r.text is None
    assert r.debug == "screencap_failed"


def test_read_input_text_via_vision_happy(monkeypatch):
    png = _make_test_png()
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
            return '{"text":"今天天气不错"}'

    monkeypatch.setattr("src.vision_client.VisionClient", _OkVC)
    r = itv.read_input_text_via_vision(
        "abc",
        {"provider": "zhipu", "api_key": "k", "model": "glm-4v-flash"},
    )
    assert r.text == "今天天气不错"
    assert r.debug == "ok"


def test_read_input_text_via_vision_init_failure_no_zhipu(monkeypatch):
    png = _make_test_png()
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (png, "", 0),
    )

    class _FailVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return False

    monkeypatch.setattr("src.vision_client.VisionClient", _FailVC)
    r = itv.read_input_text_via_vision(
        "abc",
        {"provider": "ollama", "base_url": "http://nowhere"},
        global_vision={},
    )
    assert r.text is None
    assert r.debug in ("vision_init_fail_no_zhipu", "vision_init_fail")


def test_read_input_text_via_vision_empty_field(monkeypatch):
    """LLM 看到输入框是 hint 状态（'Message'）→ 返 text=""，不应当 None。"""
    png = _make_test_png()
    monkeypatch.setattr(
        "src.integrations.line_rpa.adb_helpers.run_adb_binary",
        lambda args, *, serial, timeout: (png, "", 0),
    )

    class _EmptyVC:
        def __init__(self, cfg):
            self.cfg = cfg

        def initialize(self):
            return True

        def describe_image_sync(self, path, prompt=None):
            return '{"text":""}'

    monkeypatch.setattr("src.vision_client.VisionClient", _EmptyVC)
    r = itv.read_input_text_via_vision(
        "abc", {"provider": "zhipu", "api_key": "k"},
    )
    assert r.text == ""    # 合法空串
    assert r.debug == "ok"
