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
    *,
    tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a ``voice_output``-style dict ready for ``TTSPipeline``.

    Merges layers bottom-up so higher-priority keys always win.
    Never raises; returns ``{}`` on any error so callers stay safe.

    P3：传入端用户会员 ``tier``（如 ``get_entitlement(contact_key)["tier"]``）后，
    按 ``voice_routing`` 策略改写后端（VIP→elevenlabs，免费→edge 降级省成本）。
    ``tier=None``（默认）或 ``voice_routing.enabled=false`` → 不路由，行为不变。
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

        # ── 注入全局局域网克隆配置，让 TTSPipeline 实现「LAN 优先 → 云端兜底」──
        vcl = full_config.get("voice_clone_lan")
        if isinstance(vcl, dict) and vcl:
            merged["voice_clone_lan"] = dict(vcl)

        # ── 注入全局 ElevenLabs 配置（付费情感旗舰档，backend=elevenlabs 时消费）──
        el = full_config.get("elevenlabs")
        if isinstance(el, dict) and el:
            merged["elevenlabs"] = dict(el)

        # ── P3：注入 TTS 成本费率（供 provider_stats 记账，与是否路由无关）──
        routing_block = full_config.get("voice_routing")
        if isinstance(routing_block, dict):
            rates = routing_block.get("cost_per_1k_chars")
            if isinstance(rates, dict) and rates:
                merged["cost_per_1k_chars"] = dict(rates)

        # ── P3：按端用户档位分层路由（VIP→旗舰，免费→降级省成本）──
        if tier is not None:
            from src.ai.voice_routing import resolve_voice_routing, route_voice_backend
            routing = resolve_voice_routing(full_config)
            if routing.get("enabled"):
                merged = route_voice_backend(merged, tier=tier, routing=routing)

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
