"""P14-B/P15-A/P15-B: Shared TTS approval-preview generator.

Extracted from LINE and WA runners to eliminate ~40 lines of duplicate code.
P15-A: lazy auto-cleanup of tmp_tts_preview/ (≤once per 3h, via run_in_executor).
P15-B: 120s timeout around synthesize() to prevent semaphore starvation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

try:
    from src.ai.tts_pipeline import get_tts_pipeline
except ImportError:
    get_tts_pipeline = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

AILANGS_TO_XTTS: dict[str, str] = {
    "zh": "zh-cn", "zh-cn": "zh-cn", "zh-tw": "zh-cn",
    "en": "en", "ja": "ja", "ko": "ko", "de": "de",
    "fr": "fr", "es": "es", "ru": "ru", "ar": "ar",
    "hi": "hi", "it": "it", "pt": "pt", "nl": "nl",
    "pl": "pl", "tr": "tr", "cs": "cs", "hu": "hu",
}

# P15-A: lazy cleanup state
_TTS_DIR = Path("tmp_tts_preview")
_CLEANUP_INTERVAL_SEC: float = 3 * 3600      # run at most once per 3 hours
_DEFAULT_MAX_AGE_SEC: float = 24 * 3600      # keep files up to 24 hours
_last_cleanup_ts: float = 0.0
_SYNTHESIZE_TIMEOUT_SEC: float = 120.0       # P15-B: hard timeout per synthesis


def cleanup_tts_previews(max_age_sec: float = _DEFAULT_MAX_AGE_SEC) -> int:
    """P15-A: Delete WAV files older than max_age_sec from tmp_tts_preview/.

    Safe to call concurrently — deletes are idempotent.
    Returns the number of files removed.
    """
    removed = 0
    if not _TTS_DIR.exists():
        return 0
    cutoff = time.time() - max_age_sec
    for f in _TTS_DIR.iterdir():
        if f.suffix in (".wav", ".mp3", ".ogg") and f.is_file():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
    if removed:
        logger.info("tts_preview cleanup: removed %d old file(s)", removed)
    return removed


def _maybe_trigger_cleanup() -> None:
    """P15-A: Trigger cleanup at most once per _CLEANUP_INTERVAL_SEC.

    Called synchronously from generate_approval_tts before the first await,
    so there is no yield-point between the check and the update — asyncio
    cooperative scheduling guarantees atomicity here.
    """
    global _last_cleanup_ts
    now = time.monotonic()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL_SEC:
        return
    _last_cleanup_ts = now  # mark before await so concurrent calls skip


async def generate_approval_tts(
    pending_id: int,
    reply_text: str,
    reply_lang: str,
    *,
    voice_cfg: dict,
    state_store: Any,
    semaphore: asyncio.Semaphore,
    fname_prefix: str = "tts",
) -> None:
    """Generate a TTS preview WAV for an approval-queue pending row.

    - Writes the file URL back via state_store.update_pending_tts_path()
    - On any failure writes "ERROR" sentinel so the UI can offer a retry button
    - Serialised via semaphore to prevent simultaneous GPU/CPU model contention
    - P15-B: synthesize() is wrapped with 120s timeout — timeout releases the
      semaphore immediately so subsequent tasks are never permanently blocked
    - P15-A: lazily triggers cleanup of old preview files (once per 3 hours)
    """
    # P15-A: lazy cleanup — no yield point between check+mark, so safe
    _should_clean = time.monotonic() - _last_cleanup_ts >= _CLEANUP_INTERVAL_SEC
    if _should_clean:
        _maybe_trigger_cleanup()

    try:
        import uuid as _uuid

        tts_lang = AILANGS_TO_XTTS.get(str(reply_lang or "zh").lower(), "zh-cn")
        pipeline = get_tts_pipeline(voice_cfg)  # type: ignore[misc]
        _TTS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{fname_prefix}-{_uuid.uuid4().hex[:10]}.wav"
        out_path = _TTS_DIR / fname
        text_trunc = str(reply_text or "")[:400].strip()
        if not text_trunc:
            return

        # P15-A: run cleanup in background after we have an executor reference
        loop = asyncio.get_event_loop()
        if _should_clean:
            asyncio.ensure_future(loop.run_in_executor(None, cleanup_tts_previews))

        async with semaphore:
            # P15-B: hard timeout — prevents starvation if model hangs
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: pipeline.synthesize(text_trunc, str(out_path), lang=tts_lang),
                    ),
                    timeout=_SYNTHESIZE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"TTS synthesis timed out (>{_SYNTHESIZE_TIMEOUT_SEC:.0f}s)"
                )

        _sz = out_path.stat().st_size if out_path.exists() else 0
        if _sz < 512:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"TTS output too small ({_sz}B), discarded")
        state_store.update_pending_tts_path(pending_id, f"/api/voice/tts-file/{fname}")
        logger.debug(
            "approval TTS generated: pending=%d lang=%s size=%d fname=%s",
            pending_id, tts_lang, _sz, fname,
        )
    except Exception as _e:
        # P20-A: classify error type for better UX
        err_str = str(_e).lower()
        if "timeout" in err_str or "timed out" in err_str:
            err_code = "ERROR:timeout"
        elif "disk" in err_str or "no space" in err_str or "i/o" in err_str or "permission" in err_str:
            err_code = "ERROR:disk"
        elif "model" in err_str or "cuda" in err_str or "gpu" in err_str or "cuda out of memory" in err_str:
            err_code = "ERROR:model"
        else:
            err_code = "ERROR:unknown"
        logger.debug("approval TTS generation failed: %s", err_code, exc_info=True)
        try:
            state_store.update_pending_tts_path(pending_id, err_code)
        except Exception:
            pass
