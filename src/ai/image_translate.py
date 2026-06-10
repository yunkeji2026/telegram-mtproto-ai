"""P58：图片 OCR → 翻译 服务。

复用现有 vision 栈（VisionClient，Ollama→智谱故障转移）做**逐字 OCR**，
再把抽取文本喂给 TranslationService。OCR 后端用 P58 通用 ProviderStats 观测。

设计要点：
- ``ocr_fn`` 可注入（异步 ``(image_path) -> (text, tag)``），使单测无需真实 VLM。
- 永不抛异常给上层：失败返回 ``ok=False`` + reason，便于前端给明确提示。
- 隐私：不持久化图片字节；临时文件由调用方负责清理（见 ``decode_image_to_temp``）。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from src.ai.media_text_cache import get_media_text_cache, hash_file
from src.ai.provider_stats import get_provider_stats
from src.ai.translation_service import TranslationService, detect_language

logger = logging.getLogger(__name__)

# 逐字提取（非描述/非翻译），便于后续交给翻译引擎
OCR_PROMPT = (
    "提取图片中出现的所有文字，逐字原样输出，保留换行；"
    "不要翻译、不要描述、不要解释、不要加任何前后缀。若图中没有文字，只输出空。"
)

_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB 上限（与 vision_client 的 10MB 留余量）
_ALLOWED_MIME = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}

# ocr_fn: async (image_path) -> (text|None, tag)
OcrFn = Callable[[str], Awaitable[Tuple[Optional[str], str]]]


def decode_image_to_temp(image_b64: str) -> Tuple[Optional[str], str]:
    """把（可带 data URL 头的）base64 图片落临时文件。返回 (path|None, reason)。

    调用方在用完后必须 ``os.remove(path)``。超限/非图片/解码失败返回 (None, reason)。
    """
    raw_b64 = str(image_b64 or "").strip()
    if not raw_b64:
        return None, "empty"
    mime = "image/png"
    if raw_b64.startswith("data:"):
        header, _, payload = raw_b64.partition(",")
        if not payload:
            return None, "bad_data_url"
        mime = header[5:].split(";")[0].strip().lower() or mime
        raw_b64 = payload
    if mime not in _ALLOWED_MIME:
        return None, f"unsupported_mime:{mime}"
    try:
        data = base64.b64decode(raw_b64, validate=False)
    except Exception:
        return None, "decode_failed"
    if not data:
        return None, "empty_after_decode"
    if len(data) > _MAX_IMAGE_BYTES:
        return None, "too_large"
    suffix = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp",
    }.get(mime, ".png")
    try:
        fd, path = tempfile.mkstemp(prefix="imgxl_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path, "ok"
    except Exception as exc:  # noqa: BLE001
        return None, f"write_failed:{type(exc).__name__}"


def _provider_from_tag(tag: str) -> str:
    """从 vision fallback tag 推断实际后端名（用于用量统计）。"""
    t = (tag or "").lower()
    if "zhipu" in t:
        return "zhipu"
    if "ollama" in t or "vision_ok" in t:
        return "ollama"
    return "vision"


class ImageTranslateService:
    def __init__(self, translation_service: TranslationService, ocr_fn: OcrFn) -> None:
        self._xlate = translation_service
        self._ocr = ocr_fn

    async def translate_image(
        self,
        image_path: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
    ) -> Dict[str, Any]:
        stats = get_provider_stats("ocr", "ocr")
        # 媒体缓存：同图重复识别直接命中，跳过 VLM 调用
        cache = get_media_text_cache()
        h = hash_file(image_path)
        ck = f"ocr:{h}" if h else ""
        cached_text = cache.get(ck) if ck else None
        ocr_cached = False
        if cached_text is not None:
            ocr_text, tag, ocr_cached = cached_text, "cache", True
        else:
            t0 = time.monotonic()
            try:
                ocr_text, tag = await self._ocr(image_path)
            except Exception as exc:  # noqa: BLE001
                stats.record("vision", ok=False, latency_ms=int((time.monotonic() - t0) * 1000))
                logger.warning("OCR 调用异常: %s", exc)
                return {"ok": False, "reason": "ocr_error", "ocr_tag": f"error:{type(exc).__name__}"}

            lat = int((time.monotonic() - t0) * 1000)
            ocr_text = (ocr_text or "").strip()
            provider = _provider_from_tag(tag)
            stats.record(provider, ok=bool(ocr_text), latency_ms=lat)
            if "fallback" in (tag or "").lower():
                stats.record_fallback()
            if ocr_text and ck:
                cache.put(ck, ocr_text)

        ocr_text = (ocr_text or "").strip()
        if not ocr_text:
            return {"ok": False, "reason": "no_text", "ocr_tag": tag, "ocr_text": ""}

        src = source_lang or detect_language(ocr_text)
        result = await self._xlate.translate(
            ocr_text, target_lang=target_lang, source_lang=src, style=style,
        )
        return {
            "ok": bool(result.ok),
            "ocr_text": ocr_text,
            "ocr_tag": tag,
            "ocr_cached": ocr_cached,
            "source_lang": result.source_lang,
            "translation": result.to_dict(),
        }


def build_vision_ocr_fn(vision_cfg: Dict[str, Any], global_vision: Dict[str, Any]) -> OcrFn:
    """生产用 OCR fn：包一层 VisionClient 的 Ollama→智谱故障转移链 + OCR_PROMPT。"""

    async def _ocr(image_path: str) -> Tuple[Optional[str], str]:
        from src.vision_client import VisionClient
        return await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=OCR_PROMPT,
        )

    return _ocr


__all__ = [
    "ImageTranslateService",
    "decode_image_to_temp",
    "build_vision_ocr_fn",
    "OCR_PROMPT",
]
