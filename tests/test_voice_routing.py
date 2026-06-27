"""P3：语音分层路由 + 成本估算 + provider_stats "tts" 记账 单测。"""
from __future__ import annotations

import asyncio

from src.ai.voice_routing import (
    backend_for_tier,
    estimate_tts_cost,
    resolve_voice_routing,
    route_voice_backend,
)


# ── backend_for_tier ─────────────────────────────────────────────────────────
def test_backend_for_tier_disabled_returns_empty():
    assert backend_for_tier("vip", {"enabled": False, "tiers": {"vip": "elevenlabs"}}) == ""
    assert backend_for_tier("vip", {}) == ""


def test_backend_for_tier_mapped_and_default():
    routing = {"enabled": True, "default_backend": "edge_tts",
               "tiers": {"vip": "elevenlabs", "svip": "elevenlabs"}}
    assert backend_for_tier("vip", routing) == "elevenlabs"
    assert backend_for_tier("svip", routing) == "elevenlabs"
    assert backend_for_tier("free", routing) == "edge_tts"   # 落 default
    assert backend_for_tier(None, routing) == "edge_tts"


def test_resolve_voice_routing_reads_block():
    cfg = {"voice_routing": {"enabled": True, "tiers": {"vip": "elevenlabs"}}}
    assert resolve_voice_routing(cfg)["enabled"] is True
    assert resolve_voice_routing({})  == {}


# ── route_voice_backend ──────────────────────────────────────────────────────
def test_route_vip_to_elevenlabs_keeps_clone():
    routing = {"enabled": True, "tiers": {"vip": "elevenlabs"}}
    cfg = {"backend": "edge_tts",
           "voice_profile": {"enabled": True, "voice": "EL123", "backend": "edge_tts"}}
    out = route_voice_backend(cfg, tier="vip", routing=routing)
    assert out["backend"] == "elevenlabs"
    assert out["voice_profile"]["backend"] == "elevenlabs"
    assert out["voice_profile"]["enabled"] is True          # 克隆档保留
    assert out["_routed_tier"] == "vip"
    assert out["_routed_backend"] == "elevenlabs"
    # 原 dict 不被改
    assert cfg["backend"] == "edge_tts"


def test_route_free_downgrade_disables_clone_and_lan():
    routing = {"enabled": True, "default_backend": "edge_tts",
               "tiers": {"vip": "elevenlabs"}}
    cfg = {
        "backend": "elevenlabs",
        "voice_profile": {"enabled": True, "voice": "EL123", "backend": "elevenlabs"},
        "voice_clone_lan": {"enabled": True, "base_url": "http://x"},
    }
    out = route_voice_backend(cfg, tier="free", routing=routing)
    assert out["backend"] == "edge_tts"
    assert out["voice_profile"]["enabled"] is False         # 降级 → 关克隆门
    assert out["voice_clone_lan"]["enabled"] is False        # 同时切断 LAN 优先
    # 原 dict 不被改（深层）
    assert cfg["voice_profile"]["enabled"] is True
    assert cfg["voice_clone_lan"]["enabled"] is True


def test_route_disabled_passthrough():
    cfg = {"backend": "elevenlabs", "voice_profile": {"enabled": True}}
    out = route_voice_backend(cfg, tier="free", routing={"enabled": False})
    assert out["backend"] == "elevenlabs"
    assert "_routed_backend" not in out


# ── estimate_tts_cost ────────────────────────────────────────────────────────
def test_estimate_cost_elevenlabs_default_rate():
    # 默认 0.30/1k → 500 字符 = 0.15
    assert estimate_tts_cost("elevenlabs", 500) == 0.15


def test_estimate_cost_unknown_provider_zero():
    assert estimate_tts_cost("edge_tts", 1000) == 0.0
    assert estimate_tts_cost("elevenlabs", 0) == 0.0


def test_estimate_cost_rate_override():
    assert estimate_tts_cost("elevenlabs", 1000, {"elevenlabs": 0.10}) == 0.10
    assert estimate_tts_cost("openai", 1000, {"openai": 0.05}) == 0.05


# ── resolve_voice_cfg 接缝 ───────────────────────────────────────────────────
def test_resolve_voice_cfg_applies_routing_with_tier():
    from src.ai.persona_voice import resolve_voice_cfg

    full = {
        "telegram": {"voice_reply": {"backend": "edge_tts"}},
        "elevenlabs": {"api_key": "xi"},
        "voice_routing": {"enabled": True, "tiers": {"vip": "elevenlabs"},
                          "default_backend": "edge_tts",
                          "cost_per_1k_chars": {"elevenlabs": 0.2}},
    }
    vip_cfg = resolve_voice_cfg(None, full, tier="vip")
    assert vip_cfg["backend"] == "elevenlabs"
    assert vip_cfg["cost_per_1k_chars"]["elevenlabs"] == 0.2
    free_cfg = resolve_voice_cfg(None, full, tier="free")
    assert free_cfg["backend"] == "edge_tts"
    # 不传 tier → 不路由（行为不变）
    none_cfg = resolve_voice_cfg(None, full)
    assert none_cfg["backend"] == "edge_tts"
    assert "_routed_backend" not in none_cfg


# ── provider_stats "tts" 记账 ────────────────────────────────────────────────
def test_pipeline_records_tts_stats(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai.provider_stats import get_provider_stats

    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()

    async def fake_edge(self, text, out, voice, spec=None):
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
        })
        await p.synthesize("第一句")          # 合成 → 记 1 次 ok
        await p.synthesize("第一句")          # 命中缓存 → 记 cache hit
        snap = get_provider_stats("tts", "tts").dump()
        assert snap["cache_hits"] == 1
        assert snap["total_attempts"] == 1
        edge_row = next(r for r in snap["rows"] if r["provider"] == "edge_tts")
        assert edge_row["ok"] == 1
        assert edge_row["cost_usd"] == 0.0    # edge 无字符费

    asyncio.run(run())
    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()


def test_pipeline_records_elevenlabs_cost(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai.provider_stats import get_provider_stats
    from src.ai import elevenlabs_client as EC

    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()

    def fake_synth(self, text, voice_id, out, *, emotion=None, output_format="mp3_44100_128"):
        out.write_bytes(b"\xff\xfb" + b"\x00" * 600)

    monkeypatch.setattr(EC.ElevenLabsClient, "synthesize", fake_synth)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "elevenlabs",
            "format": "mp3", "out_dir": str(tmp_path),
            "elevenlabs": {"api_key": "xi"},
            "cost_per_1k_chars": {"elevenlabs": 1.0},   # 1 USD / 1k 便于断言
            "voice_profile": {"enabled": True, "owner_consent": True,
                              "backend": "elevenlabs", "voice": "EL123"},
        })
        await p.synthesize("x" * 1000)        # 1000 字符 * 1.0/1k = 1.0 USD
        snap = get_provider_stats("tts", "tts").dump()
        assert round(snap["total_cost_usd"], 4) == 1.0
        el_row = next(r for r in snap["rows"] if r["provider"] == "elevenlabs")
        assert el_row["cost_usd"] == 1.0

    asyncio.run(run())
    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()
