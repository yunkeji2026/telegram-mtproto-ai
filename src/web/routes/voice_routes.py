"""Unified voice / TTS API routes.

Endpoints
---------
POST /api/voice/tts-test
    Generate a TTS preview for a given persona + text.
    Body: {text, persona_id?, format?}
    Returns: {ok, url, duration_sec, provider, voice, error?}

GET  /api/voice/tts-test/{filename}
    Serve the generated preview audio file (short-lived).
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger(__name__)

_TTS_PREVIEW_DIR = Path("tmp_tts_preview")
_TTS_PREVIEW_TTL_SEC = 600  # files older than 10 min are cleaned up


_TTS_PREVIEW_PREFIXES = ("ttspreview-", "line-tts-")
# P9-D: per-prefix TTL — approval queue files survive longer than quick UI tests
_TTS_PREVIEW_TTL_BY_PREFIX: Dict[str, float] = {
    "ttspreview-": _TTS_PREVIEW_TTL_SEC,   # 10 min: UI test previews
    "line-tts-":   7200.0,                  # 2 h: LINE approval queue
}


def _cleanup_old_previews() -> None:
    """Remove preview files older than per-prefix TTL (best-effort)."""
    try:
        now = time.time()
        for f in _TTS_PREVIEW_DIR.iterdir():
            if not f.is_file():
                continue
            ttl = next(
                (v for k, v in _TTS_PREVIEW_TTL_BY_PREFIX.items() if f.name.startswith(k)),
                None,
            )
            if ttl is None:
                continue
            try:
                st = f.stat()
                # P11-B: 任何 < 512B 的孤儿文件（截断/静默失败）直接清除，不受 TTL 约束
                if st.st_size < 512 or st.st_mtime < now - ttl:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def register_voice_routes(app, api_auth, config_manager=None):
    """Register /api/voice/* endpoints on *app*."""

    @app.post("/api/voice/tts-test")
    async def api_voice_tts_test(request: Request, _=Depends(api_auth)):
        """Generate a TTS audio preview.

        Request body (JSON):
            text        — text to synthesise (required)
            persona_id  — persona ID for voice config lookup (optional)
            format      — output format override: mp3|ogg (optional)
        """
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")

        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        if len(text) > 400:
            raise HTTPException(400, "text too long (max 400 chars)")

        persona_id: Optional[str] = body.get("persona_id") or None
        fmt_override: Optional[str] = body.get("format") or None
        # Optional: caller passes current UI settings to override saved config
        cfg_override: Optional[Dict[str, Any]] = body.get("voice_cfg_override") or None

        # Resolve voice config
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}

        try:
            from src.ai.persona_voice import resolve_voice_cfg
            voice_cfg = resolve_voice_cfg(persona_id, raw_cfg)
        except Exception as ex:
            logger.warning("[voice/tts-test] resolve_voice_cfg failed: %s", ex)
            voice_cfg = {}

        # Apply caller override (allows previewing unsaved UI settings)
        if isinstance(cfg_override, dict):
            voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
            logger.debug("[voice/tts-test] override keys: %s", list(cfg_override))

        voice_cfg["enabled"] = True
        if fmt_override:
            voice_cfg["format"] = fmt_override.strip().lower()

        # Redirect output to preview dir
        _TTS_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex[:10]
        suffix = voice_cfg.get("format", "mp3")
        preview_path = _TTS_PREVIEW_DIR / f"ttspreview-{uid}.{suffix}"
        voice_cfg["out_dir"] = str(_TTS_PREVIEW_DIR)

        try:
            from src.ai.tts_pipeline import TTSPipeline
            tts = TTSPipeline(voice_cfg)
            # Override out path so we control filename
            import asyncio as _aio
            result = await _aio.wait_for(
                tts.synthesize(text, timeout_sec=45.0),
                timeout=50.0,
            )
        except Exception as ex:
            logger.error("[voice/tts-test] TTS error: %s", ex)
            return {"ok": False, "error": str(ex)[:200]}

        if not result.ok:
            return {"ok": False, "error": result.error}

        # Rename to our deterministic preview path
        try:
            Path(result.audio_path).rename(preview_path)
        except Exception:
            preview_path = Path(result.audio_path)

        # Async cleanup of old files (non-blocking)
        try:
            _cleanup_old_previews()
        except Exception:
            pass

        file_url = f"/api/voice/tts-test/{preview_path.name}"
        return {
            "ok": True,
            "url": file_url,
            "filename": preview_path.name,
            "duration_sec": result.duration_sec,
            "provider": result.provider,
            "voice": result.voice,
            "format": result.format,
            "bytes": preview_path.stat().st_size if preview_path.is_file() else 0,
        }

    def _serve_tts_file(filename: str):
        """Shared helper: validate + serve any TTS preview file."""
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        if not any(filename.startswith(p) for p in _TTS_PREVIEW_PREFIXES):
            raise HTTPException(404, "not found")
        fpath = _TTS_PREVIEW_DIR / filename
        if not fpath.is_file():
            raise HTTPException(404, "preview expired or not found")
        ext = fpath.suffix.lower().lstrip(".")
        mime_map = {"mp3": "audio/mpeg", "ogg": "audio/ogg",
                   "wav": "audio/wav", "opus": "audio/ogg"}
        return FileResponse(str(fpath), media_type=mime_map.get(ext, "application/octet-stream"))

    @app.get("/api/voice/tts-test/{filename}")
    async def api_voice_tts_file(filename: str, request: Request, _=Depends(api_auth)):
        """Serve a previously generated TTS preview file."""
        return _serve_tts_file(filename)

    @app.get("/api/voice/tts-file/{filename}")
    async def api_voice_tts_file_alt(filename: str, request: Request, _=Depends(api_auth)):
        """P8-D: Unified TTS file serving endpoint (line-tts-* + ttspreview-*)."""
        return _serve_tts_file(filename)

    @app.post("/api/admin/tts-cleanup")
    async def api_admin_tts_cleanup(request: Request, max_age_sec: int = 86400, _=Depends(api_auth)):
        """P16-C: Admin endpoint to trigger manual cleanup of old TTS preview files.

        Query params:
            max_age_sec — files older than this are deleted (default 24h)
        Returns:
            {ok: true, removed: N, max_age_sec: N}
        """
        from src.integrations.shared.tts_preview import cleanup_tts_previews
        removed = cleanup_tts_previews(max_age_sec=float(max_age_sec))
        return {"ok": True, "removed": removed, "max_age_sec": max_age_sec}

    @app.get("/api/admin/tts-stats")
    async def api_admin_tts_stats(request: Request, _=Depends(api_auth)):
        """P17-C: Return statistics about tmp_tts_preview directory.

        Returns:
            {ok: true, files: N, total_bytes: N, oldest_sec: N|null, newest_sec: N|null, by_prefix: {...}}
        """
        from src.integrations.shared.tts_preview import _TTS_DIR as tts_dir
        import time
        stats = {"files": 0, "total_bytes": 0, "by_prefix": {}, "oldest_sec": None, "newest_sec": None}
        now = time.time()
        prefixes = ("tts-", "line-tts-", "wa-tts-")
        try:
            if tts_dir.exists():
                for f in tts_dir.iterdir():
                    if not f.is_file():
                        continue
                    st = f.stat()
                    age = now - st.st_mtime
                    stats["files"] += 1
                    stats["total_bytes"] += st.st_size
                    if stats["oldest_sec"] is None or age > stats["oldest_sec"]:
                        stats["oldest_sec"] = age
                    if stats["newest_sec"] is None or age < stats["newest_sec"]:
                        stats["newest_sec"] = age
                    pref = next((p for p in prefixes if f.name.startswith(p)), "other")
                    stats["by_prefix"][pref] = stats["by_prefix"].get(pref, 0) + 1
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, **stats}

    @app.get("/admin/tts-dashboard")
    async def admin_tts_dashboard(request: Request):
        """P20-B: TTS stats dashboard page."""
        try:
            api_auth(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(
                    '<!doctype html><meta http-equiv="refresh" content="0; url=/login">'
                    '<a href="/login">请先登录</a>',
                    status_code=200,
                )
            raise
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="src/web/templates")
        return templates.TemplateResponse("admin_tts_dashboard.html", {"request": request})
