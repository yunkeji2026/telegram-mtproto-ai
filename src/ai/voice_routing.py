"""语音引擎分层路由（P3）— 按端用户会员档位选 TTS 后端。

商业意图：把「真人感情语音」做成**付费溢价点**，同时**天然控成本**——
免费/普通用户走 edge_tts / 局域网克隆（便宜或自建），VIP/SVIP 走 ElevenLabs v3
（贵但情感最强）。一处策略、全平台共用。

设计：
- **纯函数、无 IO**：路由决策只依赖 (voice_cfg, tier, routing 配置)，可单测。
- **默认关**：``voice_routing.enabled=false`` 或不传 tier → 原样返回（零行为变更）。
- **降级即省钱**：路由到非克隆后端（edge/pyttsx3/openai）时，同时关掉 voice_profile
  克隆门（含 LAN 优先），确保免费档拿到的是通用音色而非偷偷克隆——这才省到钱。
- **不改原 dict**：返回新 dict，附 ``_routed_tier`` / ``_routed_backend`` 供审计。

接现成 ``entitlement_store.get_entitlement(contact_key)["tier"]``。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 克隆类后端：路由到这些时保留 voice_profile（克隆音色）。其余视为「通用音色」降级档。
CLONE_BACKENDS = frozenset({
    "elevenlabs", "voice_clone_command", "voice_clone_lan", "coqui_http",
    "minicpm_clone",
})


def resolve_voice_routing(full_config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``voice_routing`` 配置块（缺失 → 空 dict，enabled 视为 false）。"""
    try:
        vr = (full_config or {}).get("voice_routing")
        return dict(vr) if isinstance(vr, dict) else {}
    except Exception:
        return {}


def backend_for_tier(tier: Optional[str], routing: Dict[str, Any]) -> str:
    """档位 → 后端名。未配置该档 → default_backend；都没有 → 空串（不改）。"""
    if not routing or not routing.get("enabled"):
        return ""
    tiers = routing.get("tiers") if isinstance(routing.get("tiers"), dict) else {}
    t = str(tier or "free").strip().lower()
    backend = str(tiers.get(t) or "").strip().lower()
    if not backend:
        backend = str(routing.get("default_backend") or "").strip().lower()
    return backend


def route_voice_backend(
    voice_cfg: Dict[str, Any], *, tier: Optional[str], routing: Dict[str, Any],
) -> Dict[str, Any]:
    """按档位重写 voice_cfg 的后端。纯函数；返回新 dict。

    - 路由关闭 / 无适用后端 → 原样返回（浅拷贝）。
    - 命中后端：覆盖顶层 ``backend`` 与（若存在）``voice_profile.backend``。
    - 降级到非克隆后端：把 ``voice_profile.enabled`` 置 False（关克隆门 + LAN 优先），
      免费档拿通用音色而非偷偷克隆 → 真正省成本。
    """
    out = dict(voice_cfg or {})
    backend = backend_for_tier(tier, routing)
    if not backend:
        return out

    out["backend"] = backend
    vp = out.get("voice_profile")
    if isinstance(vp, dict) and vp:
        vp = dict(vp)
        vp["backend"] = backend
        if backend not in CLONE_BACKENDS:
            # 降级档：关克隆门（_should_try_lan 与 _effective_backend 都看 enabled）
            vp["enabled"] = False
        out["voice_profile"] = vp
    # 非克隆后端同时切断 LAN 优先（即便人设没 voice_profile 也兜住）
    if backend not in CLONE_BACKENDS and isinstance(out.get("voice_clone_lan"), dict):
        lan = dict(out["voice_clone_lan"])
        lan["enabled"] = False
        out["voice_clone_lan"] = lan

    out["_routed_tier"] = str(tier or "free")
    out["_routed_backend"] = backend
    return out


# ── 成本估算（纯函数，供 provider_stats 记账）────────────────────────────────
# 默认费率（USD / 1000 字符）：仅对**按字符计费**的 provider 有意义。
# ElevenLabs 按字符计费，价随档位浮动（on-demand 较贵、企业量更低）；这里给一个
# 保守占位，运营应按实际合同在 config.voice_routing.cost_per_1k_chars 覆盖。
DEFAULT_COST_PER_1K_CHARS: Dict[str, float] = {
    "elevenlabs": 0.30,
}


def estimate_tts_cost(
    provider: str, char_count: int,
    rates: Optional[Dict[str, float]] = None,
) -> float:
    """估算一次合成花费（USD）。未知费率 provider → 0。"""
    table = dict(DEFAULT_COST_PER_1K_CHARS)
    if isinstance(rates, dict):
        for k, v in rates.items():
            try:
                table[str(k).strip().lower()] = float(v)
            except (TypeError, ValueError):
                continue
    rate = table.get(str(provider or "").strip().lower(), 0.0)
    if rate <= 0 or char_count <= 0:
        return 0.0
    return round(rate * (float(char_count) / 1000.0), 6)


def resolve_tier_for_contact(contact_key: Optional[str]) -> Optional[str]:
    """端用户 ``contact_key`` → 会员档（``'free'/'vip'/'svip'`` …）。

    可复用接缝：各平台合成前调用，把档位喂给 ``resolve_voice_cfg(..., tier=...)``。
    约定 ``contact_key == 端用户 user_id``（见 companion_context）。
    monetization 未就绪 / 空 key / 异常 → ``None`` → 调用方不路由（零行为变更）。
    """
    if not contact_key:
        return None
    try:
        from src.utils.companion_context import resolve_entitlement
        ent = resolve_entitlement(contact_key)
    except Exception:
        return None
    if not isinstance(ent, dict):
        return None
    return str(ent.get("tier") or "free")


__all__ = [
    "CLONE_BACKENDS", "DEFAULT_COST_PER_1K_CHARS",
    "resolve_voice_routing", "backend_for_tier", "route_voice_backend",
    "estimate_tts_cost", "resolve_tier_for_contact",
]
