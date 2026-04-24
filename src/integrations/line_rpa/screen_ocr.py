"""uiautomator 不可用时的读屏回退：adb screencap + 裁剪 + pytesseract OCR；可选智谱 Vision。"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 裁切预设：preset 名在 screenshot_ocr.preset 中引用；显式 ratio 仍覆盖预设同名字段
CROP_PRESETS: Dict[str, Dict[str, Any]] = {
    "phone_default": {
        "crop_bottom_ratio": 0.48,
        "peer_left_strip_ratio": 0.6,
    },
    "phone_tall_nav": {
        "crop_bottom_ratio": 0.55,
        "peer_left_strip_ratio": 0.62,
    },
    "max_chat_area": {
        "crop_bottom_ratio": 0.65,
        "peer_left_strip_ratio": 0.65,
    },
}


def resolve_screenshot_ocr_cfg(ocr_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """合并 preset 与显式字段（显式优先）。"""
    base = dict(ocr_cfg or {})
    name = (base.get("preset") or "").strip()
    if name and name in CROP_PRESETS:
        merged = {**CROP_PRESETS[name], **base}
        return merged
    return base


def capture_screen_png(serial: str, adb_module: Any) -> Optional[bytes]:
    """adb exec-out screencap -p → PNG bytes（须二进制管道，不可用 text stdout）。"""
    if hasattr(adb_module, "run_adb_binary"):
        raw, err, rc = adb_module.run_adb_binary(
            ["exec-out", "screencap", "-p"], serial=serial, timeout=45.0
        )
        if rc != 0 or not raw:
            logger.warning("screencap rc=%s err=%s", rc, (err or "")[:200])
            return None
    else:
        r = adb_module.run_adb(["exec-out", "screencap", "-p"], serial=serial, timeout=45.0)
        if r.returncode != 0 or not r.stdout:
            logger.warning("screencap rc=%s err=%s", r.returncode, (r.stderr or "")[:200])
            return None
        raw = r.stdout
        if isinstance(raw, str):
            raw = raw.encode("latin-1", errors="replace")
    if not raw.startswith(b"\x89PNG"):
        logger.warning("screencap 非 PNG 头")
        return None
    return raw


def crop_bottom_ratio(png_bytes: bytes, bottom_ratio: float) -> bytes:
    """裁剪屏幕底部区域。"""
    from PIL import Image

    br = min(0.92, max(0.12, bottom_ratio))
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    top = int(h * (1.0 - br))
    cropped = im.crop((0, top, w, h))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def crop_left_strip(png_bytes: bytes, left_width_ratio: float) -> bytes:
    """取图像左条带（LINE 中对方消息多在左侧）。"""
    from PIL import Image

    lr = min(0.98, max(0.15, left_width_ratio))
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    right = max(1, int(w * lr))
    strip = im.crop((0, 0, right, h))
    buf = io.BytesIO()
    strip.save(buf, format="PNG")
    return buf.getvalue()


def build_crop_for_ocr(png_bytes: bytes, ocr_cfg: Dict[str, Any]) -> bytes:
    """底部裁剪 + 可选左侧条带，减少己方右侧气泡干扰。"""
    ratio = float(ocr_cfg.get("crop_bottom_ratio", 0.4))
    out = crop_bottom_ratio(png_bytes, ratio)
    strip = ocr_cfg.get("peer_left_strip_ratio")
    if strip is None:
        return out
    sr = float(strip)
    if sr >= 0.999:
        return out
    return crop_left_strip(out, sr)


def ocr_png_bytes(png_bytes: bytes, lang: str) -> str:
    import pytesseract
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes))
    txt = pytesseract.image_to_string(im, lang=lang or "chi_sim+eng")
    return txt or ""


def pick_peer_line_from_ocr(text: str) -> Tuple[Optional[str], str]:
    """
    从 OCR 杂文中抽取最可能的一条「对方消息」。
    启发式：取非空行，去掉明显时间/UI，优先较长行。
    """
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if len(s) < 2:
            continue
        if re.fullmatch(r"[\d:/\sAPM上午下午]+", s):
            continue
        if s in ("CHATS", "VOOM", "LINE", "聊天", "好友"):
            continue
        lines.append(s)
    if not lines:
        return None, "no_lines"
    tail = lines[-5:] if len(lines) > 5 else lines
    best = max(tail, key=len)
    if len(best) < 2:
        return None, "too_short"
    return best, "heuristic_longest_in_tail"


def fingerprint_crop_png(cropped_png: bytes) -> str:
    return hashlib.sha256(cropped_png).hexdigest()


def capture_and_prepare_crop(
    serial: str,
    adb_module: Any,
    ocr_cfg: Dict[str, Any],
) -> Tuple[Optional[bytes], str, str]:
    """
    单次截屏并裁剪为 OCR/Vision 用图。
    返回 (cropped_png, sha256_hex, status)。失败时 cropped 为 None，sha256 为空串。
    """
    png = capture_screen_png(serial, adb_module)
    if not png:
        return None, "", "screencap_failed"
    try:
        cropped = build_crop_for_ocr(png, ocr_cfg)
    except Exception as e:
        return None, "", f"crop_error:{e}"
    return cropped, fingerprint_crop_png(cropped), "ok"


def ocr_peer_from_crop(
    cropped_png: bytes,
    ocr_cfg: Dict[str, Any],
) -> Tuple[Optional[str], str]:
    """对裁剪图 OCR，抽取对方话术一行。"""
    lang = str(ocr_cfg.get("tesseract_lang", "chi_sim+eng"))
    try:
        raw = ocr_png_bytes(cropped_png, lang)
    except Exception as e:
        logger.warning("pytesseract 失败: %s", e)
        return None, f"ocr_unavailable:{e}"
    peer, how = pick_peer_line_from_ocr(raw)
    if peer:
        return peer, f"screenshot_ocr:{how}"
    return None, f"ocr_no_peer:{how}:{raw[:120]!r}"


def peer_text_from_screenshot(
    serial: str,
    adb_module: Any,
    ocr_cfg: Dict[str, Any],
) -> Tuple[Optional[str], str, Optional[bytes], str]:
    """
    截图 → 裁剪 → OCR → 抽取一行（兼容封装）。
    返回 (peer_text, debug, crop_for_vision_if_ocr_failed, crop_sha256_or_empty)。
    """
    cropped, fp, st = capture_and_prepare_crop(serial, adb_module, ocr_cfg)
    if cropped is None:
        return None, st, None, ""
    peer, dbg = ocr_peer_from_crop(cropped, ocr_cfg)
    if peer:
        return peer, dbg, None, fp
    return None, dbg, cropped, fp


def normalize_vision_peer_line(text: str) -> Optional[str]:
    """Vision 可能返回 NONE / 空 / 带引号。"""
    if not text:
        return None
    t = text.strip()
    if t.upper() in ("NONE", "无", "NULL", "N/A"):
        return None
    t = t.strip('"“”')
    return t if len(t) >= 1 else None
