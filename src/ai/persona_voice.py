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


# Fields that pin a *specific* cloned/reference voice. When a persona switches
# to a different backend (e.g. a public neural voice via edge_tts), these must
# NOT leak from the inherited global clone profile — otherwise every persona
# would reuse the operator's own ``my_voice`` reference audio / consent flags.
_CLONE_BLEED_KEYS = (
    "reference_audio_path", "reference_text", "command_args", "command_template",
    "command_timeout_sec", "voice", "speaker_id", "voice_profile_json_path",
    "base_url", "model", "clone_path", "source", "language",
)


def _merge_voice_profile(merged: Dict[str, Any], vp: Dict[str, Any]) -> None:
    """Apply a persona ``voice_profile`` on top of an already merged voice cfg.

    If the persona explicitly selects a *different* backend than the inherited
    one, clone-specific fields are dropped first so a public neural voice does
    not accidentally reuse the global clone's reference audio / consent flags.
    """
    if not isinstance(vp, dict):
        return
    # Ignore empty UI placeholders such as {backend:"", voice:""}.
    if not any(vp.get(k) for k in (
        "enabled", "backend", "voice", "speaker_id", "reference_audio_path",
    )):
        return
    base_vp = dict(merged.get("voice_profile") or {})
    new_backend = str(vp.get("backend") or "").strip().lower()
    old_backend = str(base_vp.get("backend") or "").strip().lower()
    if new_backend and old_backend and new_backend != old_backend:
        for k in _CLONE_BLEED_KEYS:
            base_vp.pop(k, None)
        # Public/cloud neural backends carry no clone consent/enable semantics.
        if new_backend in ("edge_tts", "openai", "elevenlabs"):
            base_vp.pop("enabled", None)
            base_vp.pop("owner_consent", None)
    base_vp.update(vp)
    merged["voice_profile"] = base_vp
    # Persona may also override top-level TTS fields.
    for k in ("backend", "voice", "model", "format"):
        if vp.get(k):
            merged[k] = vp[k]


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

        # ── Layer 2: config.yaml per-persona voice_profile ──
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
                _merge_voice_profile(merged, vp)
                break

        # ── Layer 3 (highest): runtime PersonaManager profiles ──
        # Web voice enrollment persists into profiles_runtime.yaml via PersonaManager,
        # not config.yaml. Read it here so "登记成功" immediately affects TTS sends.
        if persona_id:
            try:
                from src.utils.persona_manager import PersonaManager
                p_rt = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
                if isinstance(p_rt, dict):
                    _merge_voice_profile(merged, p_rt.get("voice_profile") or {})
            except Exception:
                pass

        # ── 注入全局局域网克隆配置，让 TTSPipeline 实现「LAN 优先 → 云端兜底」──
        vcl = full_config.get("voice_clone_lan")
        if isinstance(vcl, dict) and vcl:
            merged["voice_clone_lan"] = dict(vcl)

        # ── 注入 MiniCPM-o 情感克隆主机配置（backend=minicpm_clone 时消费；异步语音消息）──
        mcc = full_config.get("minicpm_clone")
        if isinstance(mcc, dict) and mcc:
            merged["minicpm_clone"] = dict(mcc)

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


def resolve_voice_cfg_for_contact(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
    *,
    contact_key: Optional[str] = None,
) -> Dict[str, Any]:
    """便捷接缝：端用户 ``contact_key`` → 会员档 → 分层路由后的 ``voice_cfg``。

    各平台合成前**一处调用**，避免在 sender / voice_autosend / messenger 等处
    各自重复「``resolve_tier_for_contact`` → ``resolve_voice_cfg(tier=...)``」。

    ``contact_key=None`` / monetization 未就绪 / 异常 → ``tier=None`` → 不路由
    （零行为变更，与直接调 ``resolve_voice_cfg`` 等价）。
    """
    tier: Optional[str] = None
    if contact_key:
        try:
            from src.ai.voice_routing import resolve_tier_for_contact
            tier = resolve_tier_for_contact(contact_key)
        except Exception:
            tier = None
    return resolve_voice_cfg(persona_id, full_config, tier=tier)


def resolve_emotion_for_send(
    voice_cfg: Dict[str, Any],
    text: str,
    *,
    platform: str = "telegram",
    account_id: Optional[str] = None,
    chat_key: Optional[str] = None,
    intent: Optional[str] = None,
    csat: Optional[float] = None,
    persona: Optional[Dict[str, Any]] = None,
) -> Any:
    """P4：合成前的共享情感接缝（sender / voice_autosend / unified_inbox 共用）。

    仅当 ``voice_cfg.emotion.enabled=true`` 才解析会话信号并派生 ``EmotionSpec``；
    否则返回 ``None`` → 调用点传 ``emotion=None`` → 走 neutral（**零行为变更**）。

    信号优先级（见 ``voice_emotion.derive_emotion``）：CSAT 极差 → intent → 文本线索
    → 关系阶段微调。``rel_stage`` 经 ``companion_context.resolve_funnel_stage`` 取，
    provider 未就绪 / 异常 → None（仍可用 text 线索派生，绝不抛给 TTS 主流程）。
    """
    try:
        emo_cfg = voice_cfg.get("emotion") if isinstance(voice_cfg, dict) else None
        if not isinstance(emo_cfg, dict) or not emo_cfg.get("enabled"):
            return None
        default = str(emo_cfg.get("default") or "warm").strip().lower()

        rel_stage: Optional[str] = None
        if chat_key:
            try:
                from src.utils.companion_context import resolve_funnel_stage
                rel_stage = resolve_funnel_stage(
                    account_id, chat_key, channel=platform or "telegram")
            except Exception:
                rel_stage = None

        from src.ai.voice_emotion import derive_emotion
        return derive_emotion(
            intent=intent, rel_stage=rel_stage, csat=csat,
            text=text, default=default, persona=persona)
    except Exception:
        return None


def resolve_effective_voice_context(
    full_config: Dict[str, Any],
    *,
    persona_id: Optional[str] = None,
    chat_key: Optional[str] = None,
    account_persona_id: Optional[str] = None,
    contact_key: Optional[str] = None,
    platform: str = "telegram",
    account_id: Optional[str] = None,
    text: str = "",
    intent: Optional[str] = None,
    csat: Optional[float] = None,
) -> Dict[str, Any]:
    """Resolve the actual persona, voice config, and emotion used for one send.

    This is the shared decision point for manual inbox voice, Telegram auto voice,
    and System Z autosend voice. ``persona_id`` is an explicit UI/operator choice;
    otherwise we fall back to chat binding, then account persona, then defaults.
    """
    cfg = full_config or {}
    resolved_persona: Dict[str, Any] = {}
    resolved_id = str(persona_id or "").strip()
    source = "explicit" if resolved_id else "fallback"
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if resolved_id:
            p = pm.get_persona_by_id(resolved_id)
            if isinstance(p, dict):
                resolved_persona = p
        else:
            p, tier = pm.get_persona_with_tier(
                str(chat_key or ""), str(account_persona_id or ""))
            if isinstance(p, dict):
                resolved_persona = p
                resolved_id = str(p.get("id") or "").strip()
                source = str(tier or source)
    except Exception:
        resolved_persona = {}

    voice_cfg = resolve_voice_cfg_for_contact(
        resolved_id or None, cfg, contact_key=contact_key)
    # Inline/snapshot bindings can carry a voice_profile without an id. Merge it
    # directly so legacy chat bindings still get their own voice.
    if isinstance(resolved_persona, dict):
        _merge_voice_profile(voice_cfg, resolved_persona.get("voice_profile") or {})

    # The pinned emotion baseline lives on the *resolved* voice_profile (which an
    # inline binding inherits from its profile by id). Surface it to the emotion
    # layer so the auto-reply path honors the same baseline as explicit selection.
    emo_persona = dict(resolved_persona) if isinstance(resolved_persona, dict) else {}
    vp_eff = voice_cfg.get("voice_profile")
    if isinstance(vp_eff, dict) and vp_eff.get("emotion") and not (
        (emo_persona.get("voice_profile") or {}).get("emotion")
    ):
        _evp = dict(emo_persona.get("voice_profile") or {})
        _evp["emotion"] = vp_eff["emotion"]
        emo_persona["voice_profile"] = _evp

    emotion = resolve_emotion_for_send(
        voice_cfg, text, platform=platform, account_id=account_id,
        chat_key=chat_key or contact_key, intent=intent, csat=csat,
        persona=emo_persona or None,
    )
    return {
        "persona_id": resolved_id,
        "persona": resolved_persona,
        "persona_source": source,
        "voice_cfg": voice_cfg,
        "emotion": emotion,
    }


def resolve_account_persona_id(
    full_config: Dict[str, Any],
    platform: str,
    account_id: str,
    *,
    registry: Any = None,
) -> str:
    """Resolve the effective account-level persona id for a platform account.

    Single source of truth for "which persona does this account speak as",
    shared by autosend-voice gating and auto-draft enrichment so both agree.

    Priority (highest → lowest):
      1. registry ``meta.persona_id``   — explicit singular binding
      2. registry ``meta.persona_ids[0]`` — plural list written by
         ``TelegramAccountRegistry.sync_to_account_registry`` (config sync)
      3. ``config[platform].persona_ids[0]`` — static config default

    Fixes the plural/singular mismatch root cause: sync writes ``persona_ids``
    (list) but callers historically read ``persona_id`` (scalar) → empty
    ``_real_pid`` → voice grey-list allowlist mis-blocks → "发语音却只收到文字".
    Best-effort: any lookup failure degrades to the next tier, never raises.
    """
    cfg = full_config or {}
    try:
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        row = registry.get(platform, account_id) or {}
        meta = row.get("meta") or {}
        pid = str(meta.get("persona_id") or "").strip()
        if pid:
            return pid
        _pids = meta.get("persona_ids") or []
        if _pids:
            pid = str((_pids[0] if _pids else "") or "").strip()
            if pid:
                return pid
    except Exception:
        pass
    try:
        _dpids = (cfg.get(platform, {}) or {}).get("persona_ids") or []
        if _dpids:
            return str((_dpids[0] if _dpids else "") or "").strip()
    except Exception:
        pass
    return ""


def get_voice_profile_for_persona(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return only the ``voice_profile`` sub-dict for clone settings."""
    cfg = resolve_voice_cfg(persona_id, full_config)
    vp = cfg.get("voice_profile")
    return dict(vp) if isinstance(vp, dict) else {}
