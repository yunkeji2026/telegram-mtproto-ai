"""line_rpa 截图 OCR 启发式单测（无设备、无 Tesseract）。"""

from io import BytesIO

from PIL import Image

from src.integrations.line_rpa.screen_ocr import (
    build_crop_for_ocr,
    fingerprint_crop_png,
    normalize_vision_peer_line,
    pick_peer_line_from_ocr,
    resolve_screenshot_ocr_cfg,
)


def test_pick_peer_line_prefers_long_in_tail():
    raw = "12:00\nCHATS\n你好啊今天天气不错\n嗯\n"
    peer, how = pick_peer_line_from_ocr(raw)
    assert peer == "你好啊今天天气不错"
    assert "tail" in how


def test_normalize_vision_none():
    assert normalize_vision_peer_line("NONE") is None
    assert normalize_vision_peer_line("  hello  ") == "hello"


def test_fingerprint_stable():
    buf = BytesIO()
    Image.new("RGB", (10, 10), color=(0, 0, 0)).save(buf, format="PNG")
    p = buf.getvalue()
    assert len(fingerprint_crop_png(p)) == 64


def test_resolve_preset_merge():
    r = resolve_screenshot_ocr_cfg(
        {"preset": "phone_default", "use_tesseract": False, "crop_bottom_ratio": 0.7}
    )
    assert r["use_tesseract"] is False
    assert r["crop_bottom_ratio"] == 0.7
    assert "peer_left_strip_ratio" in r


def test_build_crop_smaller_width():
    buf = BytesIO()
    Image.new("RGB", (100, 100), color=(255, 0, 0)).save(buf, format="PNG")
    png = buf.getvalue()
    out = build_crop_for_ocr(
        png,
        {"crop_bottom_ratio": 0.5, "peer_left_strip_ratio": 0.5},
    )
    im = Image.open(BytesIO(out))
    assert im.size == (50, 50)
