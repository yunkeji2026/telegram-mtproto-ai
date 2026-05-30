"""WhatsApp Media Vision — 截图裁剪 + VisionClient 描述媒体消息内容。

设计思路（相比 Messenger combined_vision 的精简化移植）：
- 不依赖 WhatsApp 媒体文件系统（避免路径/mtime 关联难题）
- 直接对聊天界面截图裁剪出媒体气泡区域
- 调用 VisionClient（Ollama→智谱 fallback）生成自然语言描述
- Vision 不可用时退回 placeholder，保证主流程不被阻断
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 按 kind 区分的中英双语提示词 ─────────────────────────────────────────────

_PROMPT_IMAGE_ZH = (
    "这是 WhatsApp 聊天界面截图，请聚焦**对方（左侧）发来的图片气泡**。\n"
    "用 1-2 句自然中文描述图片内容。\n"
    "要求：\n"
    "  - 描述画面主体（人物/物品/场景/文字截图/收据等）\n"
    "  - 自拍/人像：外貌/表情/环境，不描述细节面部特征\n"
    "  - 截图/文件：说明主要信息（金额/订单号/操作步骤等）\n"
    "  - 看不清就写「画面不清晰的图片」\n"
    "  - ≤80字，纯文字，不要 JSON / markdown / 前缀"
)

_PROMPT_IMAGE_EN = (
    "This is a WhatsApp chat screenshot. Focus on the image bubble sent by the OTHER party (left side).\n"
    "Describe the image content in 1-2 natural sentences.\n"
    "Rules:\n"
    "  - Describe the main subject (person, object, scene, screenshot, receipt)\n"
    "  - For selfies: gender/expression/background, no detailed facial features\n"
    "  - For screenshots: state the key info (amount, order ID, steps)\n"
    "  - If unclear, write 'unclear image'\n"
    "  - ≤80 chars, plain text, no JSON/markdown/prefix"
)

_PROMPT_STICKER_ZH = (
    "这是 WhatsApp 聊天界面截图，请聚焦**对方发来的贴纸或表情包**。\n"
    "用一句话描述贴纸的主体形象和传递的情绪或动作。\n"
    "  - ≤40字，纯文字"
)

_PROMPT_STICKER_EN = (
    "WhatsApp chat screenshot. Focus on the sticker/emoji sent by the other party.\n"
    "Describe the sticker subject and emotion in one sentence. ≤40 chars, plain text."
)

_PROMPT_VIDEO_ZH = (
    "这是 WhatsApp 聊天界面截图，请聚焦**对方发来的视频气泡缩略图**（可能有播放三角 ▶）。\n"
    "用一句话描述缩略图画面内容，如有时长标签请一并说明。\n"
    "  - 不要假装看完整个视频，仅基于缩略图\n"
    "  - ≤60字，纯文字"
)

_PROMPT_VIDEO_EN = (
    "WhatsApp chat screenshot. Focus on the video thumbnail sent by the other party (may have ▶ icon).\n"
    "Describe the thumbnail content in one sentence. Include duration if visible.\n"
    "Do NOT pretend to have watched the full video. ≤60 chars, plain text."
)

_PROMPT_GIF_ZH = (
    "这是 WhatsApp 聊天界面截图，请聚焦**对方发来的 GIF 动图气泡**（标有 GIF 角标）。\n"
    "用一句话描述动图的主体动作和情绪。≤40字，纯文字。"
)

_PROMPT_GIF_EN = (
    "WhatsApp chat screenshot. Focus on the GIF bubble sent by the other party.\n"
    "Describe the action/emotion in one sentence. ≤40 chars, plain text."
)

_PROMPT_FILE_ZH = "对方发来了一个文件。"
_PROMPT_FILE_EN = "The other party sent a file."


def _pick_prompt(kind: str, lang: str = "zh") -> str:
    zh = lang.startswith("zh")
    table = {
        "image":             (_PROMPT_IMAGE_ZH,   _PROMPT_IMAGE_EN),
        "sticker":           (_PROMPT_STICKER_ZH, _PROMPT_STICKER_EN),
        "animated_sticker":  (_PROMPT_STICKER_ZH, _PROMPT_STICKER_EN),
        "video":             (_PROMPT_VIDEO_ZH,   _PROMPT_VIDEO_EN),
        "gif":               (_PROMPT_GIF_ZH,     _PROMPT_GIF_EN),
        "file":              (_PROMPT_FILE_ZH,    _PROMPT_FILE_EN),
    }
    prompts = table.get(kind, (_PROMPT_IMAGE_ZH, _PROMPT_IMAGE_EN))
    return prompts[0] if zh else prompts[1]


# ── 截图裁剪工具 ─────────────────────────────────────────────────────────────

def _crop_png(
    png_bytes: bytes,
    bounds: Tuple[int, int, int, int],
    *,
    padding: int = 24,
    max_dim: int = 960,
) -> Optional[bytes]:
    """从截图 bytes 中裁剪媒体气泡区域，返回 JPEG bytes 或 None。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        iw, ih = img.size
        l, t, r, b = bounds
        l = max(0, l - padding)
        t = max(0, t - padding)
        r = min(iw, r + padding)
        b = min(ih, b + padding)
        if r <= l or b <= t:
            return None
        crop = img.crop((l, t, r, b))
        # 缩放不超过 max_dim
        cw, ch = crop.size
        if max(cw, ch) > max_dim:
            scale = max_dim / max(cw, ch)
            crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        logger.debug("[media_vision] crop 失败: %s", e)
        return None


# ── 核心描述函数 ─────────────────────────────────────────────────────────────

async def describe_wa_media(
    png_bytes: bytes,
    bounds: Tuple[int, int, int, int],
    kind: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    lang: str = "zh",
    padding: int = 24,
    max_image_dim: int = 960,
    timeout_sec: float = 30.0,
) -> Tuple[Optional[str], str]:
    """裁剪截图 + 调用 VisionClient 描述 WhatsApp 媒体消息。

    Returns
    -------
    (description_text, backend_tag)
      description_text: None 表示 Vision 不可用或失败
      backend_tag: 供 metrics 使用，如 "ollama_ok" / "zhipu_only" / "vision_fail"
    """
    if kind == "file":
        return _pick_prompt("file", lang), "placeholder_file"

    crop_bytes = _crop_png(png_bytes, bounds, padding=padding, max_dim=max_image_dim)
    if not crop_bytes:
        logger.debug("[media_vision] 裁剪失败，无法描述 kind=%s", kind)
        return None, "crop_fail"

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(crop_bytes)
            tmp_path = f.name

        try:
            from src.vision_client import VisionClient, has_any_vision_backend
        except ImportError:
            logger.debug("[media_vision] VisionClient 未安装")
            return None, "vision_client_unavailable"

        if not has_any_vision_backend(vision_cfg, global_vision):
            logger.debug("[media_vision] 无可用 Vision 后端")
            return None, "no_vision_backend"

        prompt = _pick_prompt(kind, lang)
        try:
            import asyncio
            desc, tag = await asyncio.wait_for(
                VisionClient.describe_image_with_ollama_zhipu_fallback(
                    vision_cfg, global_vision, tmp_path, prompt=prompt
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning("[media_vision] VisionClient 超时 kind=%s", kind)
            return None, "vision_timeout"

        if not (desc or "").strip():
            logger.debug("[media_vision] Vision 返回空 kind=%s tag=%s", kind, tag)
            return None, tag or "vision_empty"

        logger.info("[media_vision] 描述成功 kind=%s tag=%s text=%r", kind, tag, desc[:60])
        return desc.strip(), tag or "vision_ok"

    except Exception as e:
        logger.warning("[media_vision] describe_wa_media 异常: %s", e, exc_info=True)
        return None, f"vision_error:{e}"
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ── Placeholder 生成 ─────────────────────────────────────────────────────────

_PLACEHOLDER_ZH = {
    "image":            "[对方发送了一张图片，暂时无法识别内容]",
    "sticker":          "[对方发送了一个贴纸]",
    "animated_sticker": "[对方发送了一个动态贴纸]",
    "video":            "[对方发送了一段视频]",
    "gif":              "[对方发送了一个 GIF 动图]",
    "file":             "[对方发送了一个文件]",
    "other":            "[对方发送了一条媒体消息]",
}

_PLACEHOLDER_EN = {
    "image":            "[Peer sent an image (content unavailable)]",
    "sticker":          "[Peer sent a sticker]",
    "animated_sticker": "[Peer sent an animated sticker]",
    "video":            "[Peer sent a video]",
    "gif":              "[Peer sent a GIF]",
    "file":             "[Peer sent a file]",
    "other":            "[Peer sent a media message]",
}


def media_placeholder(kind: str, *, lang: str = "zh", duration_text: str = "") -> str:
    zh = lang.startswith("zh")
    table = _PLACEHOLDER_ZH if zh else _PLACEHOLDER_EN
    ph = table.get(kind, table["other"])
    if duration_text and kind == "video":
        if zh:
            ph = f"[对方发送了一段视频，时长 {duration_text}]"
        else:
            ph = f"[Peer sent a video (duration: {duration_text})]"
    return ph
