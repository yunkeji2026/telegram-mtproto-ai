"""P15-E: Unit tests for src/integrations/shared/tts_preview.py

Covers:
  - cleanup_tts_previews() — age filtering, non-audio files ignored, missing dir
  - generate_approval_tts() — success / small-file failure / synthesize exception /
    timeout / voice disabled (caller check) / empty text early-exit
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

import src.integrations.shared.tts_preview as _mod
from src.integrations.shared.tts_preview import (
    cleanup_tts_previews,
    generate_approval_tts,
    _TTS_DIR,
)


# ─────────────────────────── helpers ──────────────────────────────────────────

def _make_store():
    s = MagicMock()
    s.update_pending_tts_path = MagicMock()
    return s


def _make_pipeline(*, raises=None, size_bytes=1024):
    """Return a mock pipeline whose synthesize() writes `size_bytes` to the path."""
    def _synthesize(text, path, lang="zh-cn"):
        if raises:
            raise raises
        Path(path).write_bytes(b"x" * size_bytes)

    p = MagicMock()
    p.synthesize.side_effect = _synthesize
    return p


# ─────────────────────────── cleanup_tts_previews ─────────────────────────────

def test_cleanup_missing_dir():
    """cleanup_tts_previews returns 0 when directory doesn't exist."""
    with patch.object(_mod, "_TTS_DIR", Path("/nonexistent/__tts_test_xyz__")):
        assert cleanup_tts_previews() == 0


def test_cleanup_removes_old_files(tmp_path: Path):
    old_wav = tmp_path / "old.wav"
    new_wav = tmp_path / "new.wav"
    ignored_txt = tmp_path / "note.txt"
    ignored_txt.write_text("keep")

    # Write files then back-date old one
    old_wav.write_bytes(b"x" * 100)
    new_wav.write_bytes(b"x" * 100)
    old_mtime = time.time() - 90000   # >24h ago
    import os
    os.utime(str(old_wav), (old_mtime, old_mtime))

    with patch.object(_mod, "_TTS_DIR", tmp_path):
        removed = cleanup_tts_previews(max_age_sec=86400)

    assert removed == 1
    assert not old_wav.exists()
    assert new_wav.exists()
    assert ignored_txt.exists()


def test_cleanup_keeps_recent_files(tmp_path: Path):
    wav = tmp_path / "recent.wav"
    wav.write_bytes(b"x" * 100)
    with patch.object(_mod, "_TTS_DIR", tmp_path):
        removed = cleanup_tts_previews(max_age_sec=86400)
    assert removed == 0
    assert wav.exists()


# ─────────────────────────── generate_approval_tts ────────────────────────────

@pytest.fixture(autouse=True)
def _reset_cleanup_ts():
    """Reset lazy cleanup timestamp so tests are independent."""
    orig = _mod._last_cleanup_ts
    _mod._last_cleanup_ts = 0.0
    yield
    _mod._last_cleanup_ts = orig


def _semaphore():
    return asyncio.Semaphore(1)


@pytest.mark.asyncio
async def test_success_writes_url(tmp_path: Path):
    pipeline = _make_pipeline(size_bytes=1024)
    store = _make_store()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            42, "Hello world", "en",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=_semaphore(),
            fname_prefix="test-tts",
        )

    call_args = store.update_pending_tts_path.call_args
    assert call_args is not None
    pid, url = call_args.args
    assert pid == 42
    assert url.startswith("/api/voice/tts-file/test-tts-")
    assert url.endswith(".wav")


@pytest.mark.asyncio
async def test_empty_text_exits_early(tmp_path: Path):
    pipeline = _make_pipeline()
    store = _make_store()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            1, "   ", "zh",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=_semaphore(),
        )

    pipeline.synthesize.assert_not_called()
    store.update_pending_tts_path.assert_not_called()


@pytest.mark.asyncio
async def test_small_file_writes_error(tmp_path: Path):
    """When synthesize() produces a file < 512 bytes, write ERROR:unknown sentinel."""
    pipeline = _make_pipeline(size_bytes=100)
    store = _make_store()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            7, "test text", "ja",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=_semaphore(),
        )

    # P20-A: writes ERROR:unknown for small file
    store.update_pending_tts_path.assert_called_once_with(7, "ERROR:unknown")


@pytest.mark.asyncio
async def test_synthesize_exception_writes_error(tmp_path: Path):
    """If synthesize() raises with 'model crash', write ERROR:model sentinel."""
    pipeline = _make_pipeline(raises=RuntimeError("model crash"))
    store = _make_store()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            99, "crash test", "ko",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=_semaphore(),
        )

    # P20-A: writes ERROR:model for model-related exception
    store.update_pending_tts_path.assert_called_once_with(99, "ERROR:model")


@pytest.mark.asyncio
async def test_timeout_releases_semaphore_and_writes_error(tmp_path: Path, monkeypatch):
    """P15-B/P20-A: timeout must (a) write ERROR:timeout, (b) release semaphore for next caller."""
    monkeypatch.setattr(_mod, "_SYNTHESIZE_TIMEOUT_SEC", 0.01)

    import threading

    def _slow_synthesize(text, path, lang="zh-cn"):
        threading.Event().wait(2.0)   # blocks thread 2s (will be force-abandoned)

    pipeline = MagicMock()
    pipeline.synthesize.side_effect = _slow_synthesize
    store = _make_store()
    sem = _semaphore()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            5, "slow text", "en",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=sem,
        )

    # P20-A: ERROR:timeout must have been written
    store.update_pending_tts_path.assert_called_once_with(5, "ERROR:timeout")
    # Semaphore must be released (value back to 1)
    assert sem._value == 1


@pytest.mark.asyncio
async def test_lang_fallback_to_zh(tmp_path: Path):
    """Unknown language codes fall back to zh-cn."""
    pipeline = _make_pipeline(size_bytes=1024)
    store = _make_store()

    with (
        patch("src.integrations.shared.tts_preview.get_tts_pipeline", return_value=pipeline),
        patch.object(_mod, "_TTS_DIR", tmp_path),
    ):
        await generate_approval_tts(
            3, "text", "xx-unknown",
            voice_cfg={"enabled": True},
            state_store=store,
            semaphore=_semaphore(),
        )

    # Should have called synthesize with zh-cn (the fallback)
    _, call_kwargs = pipeline.synthesize.call_args
    assert pipeline.synthesize.call_args.kwargs.get("lang", "zh-cn") == "zh-cn" or \
           pipeline.synthesize.call_args.args[2] == "zh-cn"
