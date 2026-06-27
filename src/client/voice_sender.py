"""TelegramVoiceSender — send OGG/Opus voice notes via pyrogram.

Responsibilities:
- OGG/Opus format conversion from mp3/wav via ffmpeg (soft-fail if missing)
- pyrogram ``client.send_voice()`` wrapper
- Temporary file cleanup

Example::

    from src.client.voice_sender import send_telegram_voice
    ok = await send_telegram_voice(client, chat_id, "/tmp/reply.mp3", duration=8)
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── ffmpeg helpers ───────────────────────────────────────────────────────────

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_audio_duration_ms(path: str) -> Optional[int]:
    """用 ffprobe 探测音频时长（毫秒）。ffprobe 缺失/失败/无效返回 ``None``。

    LINE 音频消息（``audio``）要求 ``duration`` 毫秒整数；官方通道语音出站据此填值。
    """
    if shutil.which("ffprobe") is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(p),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return None
        secs = float((r.stdout or "").strip() or 0.0)
        if secs <= 0:
            return None
        return int(round(secs * 1000))
    except Exception:
        return None


def convert_to_ogg_opus(src_path: str, *, delete_src: bool = False) -> Optional[str]:
    """Convert an audio file to OGG/Opus using ffmpeg.

    Returns the path to the ``.ogg`` file on success, ``None`` on failure.
    If *delete_src* is ``True`` the original file is removed after conversion.
    If the source is already ``.ogg`` the path is returned unchanged.
    """
    if not _ffmpeg_available():
        logger.warning("[voice_sender] ffmpeg not found — OGG conversion skipped")
        return None

    src = Path(src_path)
    if not src.is_file():
        logger.warning("[voice_sender] source file not found: %s", src_path)
        return None

    if src.suffix.lower() == ".ogg":
        return src_path

    dst = src.with_suffix(".ogg")
    if dst == src:
        dst = src.parent / (src.stem + "_opus.ogg")

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-c:a", "libopus",
                "-b:a", "48k",
                "-vbr", "on",
                "-compression_level", "5",
                str(dst),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            logger.warning(
                "[voice_sender] ffmpeg rc=%d: %s",
                r.returncode, (r.stderr or "")[:300],
            )
            return None
        if not dst.is_file() or dst.stat().st_size == 0:
            logger.warning("[voice_sender] ffmpeg produced empty output: %s", dst)
            return None
        if delete_src:
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass
        logger.info("[voice_sender] converted %s → %s", src.name, dst.name)
        return str(dst)
    except subprocess.TimeoutExpired:
        logger.warning("[voice_sender] ffmpeg timed out for %s", src_path)
        return None
    except Exception as ex:
        logger.warning("[voice_sender] ffmpeg error: %s", ex)
        return None


# ── Main send helper ─────────────────────────────────────────────────────────

async def send_telegram_voice(
    client: Any,
    chat_id: Any,
    audio_path: str,
    *,
    duration: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
) -> bool:
    """Send a voice note via ``pyrogram client.send_voice()``.

    Converts to OGG/Opus if needed; falls back to sending the original file
    as an audio document if conversion is unavailable.

    Returns ``True`` on success, ``False`` on failure.
    """
    path = Path(audio_path)
    if not path.is_file():
        logger.error("[voice_sender] audio file not found: %s", audio_path)
        return False

    ogg_path: Optional[str] = None
    cleanup_ogg = False

    if path.suffix.lower() != ".ogg":
        converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path)
        if converted:
            ogg_path = converted
            cleanup_ogg = True
        else:
            ogg_path = audio_path
            logger.warning(
                "[voice_sender] no OGG conversion available, sending %s as-is", path.suffix
            )
    else:
        ogg_path = audio_path

    send_kw: dict = {"chat_id": chat_id, "voice": ogg_path}
    if duration is not None and duration > 0:
        send_kw["duration"] = int(duration)
    if reply_to_message_id is not None:
        send_kw["reply_to_message_id"] = int(reply_to_message_id)

    try:
        await client.send_voice(**send_kw)
        logger.info(
            "[voice_sender] sent voice chat_id=%s file=%s dur=%s",
            chat_id, Path(ogg_path).name, duration,
        )
        return True
    except Exception as ex:
        logger.error("[voice_sender] send_voice failed chat_id=%s: %s", chat_id, ex)
        return False
    finally:
        if cleanup_ogg and ogg_path != audio_path:
            try:
                Path(ogg_path).unlink(missing_ok=True)
            except Exception:
                pass
