"""Resolve voice_output config for a given persona_id.

Three-tier fallback priority (highest → lowest):
  1. ``personas.<id>.voice_profile``   — per-persona voice clone / TTS settings
  2. ``telegram.voice_reply``           — TG-specific defaults (backend/voice/format)
  3. ``messenger_rpa.voice_output``     — legacy compat shim (kept ≥ 6 months)

Usage::

    from src.ai.persona_voice import resolve_voice_cfg
    cfg = resolve_voice_cfg(persona_id, config_manager.config)
    tts = TTSPipeline(cfg)
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_voice_cfg(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a ``voice_output``-style dict ready for ``TTSPipeline``.

    Merges layers bottom-up so higher-priority keys always win.
    Never raises; returns ``{}`` on any error so callers stay safe.
    """
    try:
        # ── Layer 0 (lowest): messenger_rpa.voice_output compat shim ──
        mrpa_vo: Dict[str, Any] = dict(
            (full_config.get("messenger_rpa") or {}).get("voice_output") or {}
        )

        # ── Layer 1: telegram.voice_reply TG-specific overrides ──
        tg_vr: Dict[str, Any] = dict(
            (full_config.get("telegram") or {}).get("voice_reply") or {}
        )
        merged = {**mrpa_vo}
        for k, v in tg_vr.items():
            if v is not None:
                merged[k] = v

        # ── Layer 2 (highest): per-persona voice_profile ──
        if persona_id:
            personas_cfg = full_config.get("personas") or {}
            profiles = personas_cfg.get("profiles") or []
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                if p.get("id") != persona_id:
                    continue
                vp = p.get("voice_profile")
                if not isinstance(vp, dict):
                    break
                # Merge voice_profile sub-dict
                base_vp = dict(merged.get("voice_profile") or {})
                base_vp.update(vp)
                merged["voice_profile"] = base_vp
                # Persona may also override top-level TTS fields
                for k in ("backend", "voice", "model", "format"):
                    if vp.get(k):
                        merged[k] = vp[k]
                break

        return merged
    except Exception:
        return {}


def get_voice_profile_for_persona(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return only the ``voice_profile`` sub-dict for clone settings."""
    cfg = resolve_voice_cfg(persona_id, full_config)
    vp = cfg.get("voice_profile")
    return dict(vp) if isinstance(vp, dict) else {}
