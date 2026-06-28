"""P3：语音分层路由 + 成本估算 + provider_stats "tts" 记账 单测。"""
from __future__ import annotations

import asyncio

from fastapi import Request

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


# ── resolve_tier_for_contact 接缝 ────────────────────────────────────────────
def test_resolve_tier_for_contact_no_resolver_returns_none():
    from src.ai.voice_routing import resolve_tier_for_contact
    from src.utils.companion_context import reset_relationship_providers

    reset_relationship_providers()                       # monetization 未就绪
    assert resolve_tier_for_contact("u1") is None
    assert resolve_tier_for_contact("") is None
    assert resolve_tier_for_contact(None) is None


def test_resolve_tier_for_contact_with_resolver():
    from src.ai.voice_routing import resolve_tier_for_contact
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    set_relationship_providers(
        entitlement_resolver=lambda ck: {"tier": "vip"} if ck == "vip_user" else {"tier": "free"})
    try:
        assert resolve_tier_for_contact("vip_user") == "vip"
        assert resolve_tier_for_contact("rando") == "free"
    finally:
        reset_relationship_providers()


def test_resolve_tier_for_contact_resolver_raises_safe():
    from src.ai.voice_routing import resolve_tier_for_contact
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    def _boom(ck):
        raise RuntimeError("monetization down")

    set_relationship_providers(entitlement_resolver=_boom)
    try:
        assert resolve_tier_for_contact("u1") is None  # 异常吞掉 → 不路由
    finally:
        reset_relationship_providers()


# ── voice_autosend 端到端按档路由 ────────────────────────────────────────────
def test_stage_voice_file_routes_by_tier(tmp_path, monkeypatch):
    """VIP 端用户 → 路由到 elevenlabs；config voice_routing 生效，且后端被改写。"""
    import os
    import tempfile
    from src.inbox import voice_autosend as va
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    fd, audio = tempfile.mkstemp(suffix=".ogg")
    os.write(fd, b"OGGfakebytes")
    os.close(fd)

    captured = {}

    class _CapTTS:
        def __init__(self, cfg):
            captured["backend"] = cfg.get("backend")

        async def synthesize(self, text, timeout_sec=45.0, emotion=None):
            with open(audio, "wb") as f:   # 每次重建（stage 读后会删）
                f.write(b"OGGfakebytes")

            class _R:
                ok = True
                audio_path = audio
            return _R()

    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _CapTTS)
    monkeypatch.setattr("src.client.voice_sender.convert_to_ogg_opus",
                        lambda p, delete_src=True: p)
    monkeypatch.setattr("src.integrations.protocol_bridge.save_outbound_media",
                        lambda platform, account_id, name, data:
                        ("/local/out.ogg", "/static/x/out.ogg", "voice"))
    set_relationship_providers(
        entitlement_resolver=lambda ck: {"tier": "vip"} if ck == "vipper" else {"tier": "free"})

    cfg = {
        "telegram": {"voice_reply": {"backend": "edge_tts"}},
        "elevenlabs": {"api_key": "xi"},
        "voice_routing": {"enabled": True, "default_backend": "edge_tts",
                          "tiers": {"vip": "elevenlabs"}},
    }
    try:
        async def run():
            out = await va.stage_voice_file(
                cfg, "telegram", "acct1", "persona1", "你好", contact_key="vipper")
            assert out is not None
            assert captured["backend"] == "elevenlabs"     # VIP → 旗舰

            out2 = await va.stage_voice_file(
                cfg, "telegram", "acct1", "persona1", "你好", contact_key="freeloader")
            assert out2 is not None
            assert captured["backend"] == "edge_tts"        # 免费 → 降级

        asyncio.run(run())
    finally:
        reset_relationship_providers()


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


# ── resolve_voice_cfg_for_contact 共享接缝（sender / autosend 共用）──────────
def _routing_config():
    return {
        "voice_routing": {
            "enabled": True,
            "default_backend": "edge_tts",
            "tiers": {"vip": "elevenlabs", "free": "edge_tts"},
        },
        "telegram": {"voice_reply": {"backend": "elevenlabs",
                                     "voice_profile": {"enabled": True, "voice": "X"}}},
    }


def test_resolve_voice_cfg_for_contact_no_key_no_routing():
    from src.ai.persona_voice import resolve_voice_cfg_for_contact
    # 无 contact_key → tier=None → 不路由（保留配置后端）
    cfg = resolve_voice_cfg_for_contact(None, _routing_config(), contact_key=None)
    assert cfg.get("backend") == "elevenlabs"
    assert "_routed_backend" not in cfg


def test_resolve_voice_cfg_for_contact_routes_vip_and_free():
    from src.ai.persona_voice import resolve_voice_cfg_for_contact
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    set_relationship_providers(
        entitlement_resolver=lambda ck: {"tier": "vip"} if ck == "vip_user" else {"tier": "free"})
    try:
        vip = resolve_voice_cfg_for_contact(None, _routing_config(), contact_key="vip_user")
        assert vip.get("_routed_backend") == "elevenlabs"
        free = resolve_voice_cfg_for_contact(None, _routing_config(), contact_key="joe")
        assert free.get("_routed_backend") == "edge_tts"
        # 降级档关克隆门
        assert (free.get("voice_profile") or {}).get("enabled") is False
    finally:
        reset_relationship_providers()


def test_resolve_voice_cfg_for_contact_resolver_down_no_routing():
    from src.ai.persona_voice import resolve_voice_cfg_for_contact
    from src.utils.companion_context import reset_relationship_providers

    reset_relationship_providers()   # monetization 未就绪 → tier=None
    cfg = resolve_voice_cfg_for_contact(None, _routing_config(), contact_key="anyone")
    assert cfg.get("backend") == "elevenlabs"
    assert "_routed_backend" not in cfg


# ── 原生 TG sender：chat.id → contact_key → 分层路由 ───────────────────────
def test_sender_voice_reply_passes_chat_id_as_contact_key(monkeypatch, tmp_path):
    """_maybe_send_voice_reply 把私聊 chat.id 作为 contact_key 喂给共享接缝。"""
    import logging
    import types
    from unittest.mock import AsyncMock

    from src.client.sender import TelegramSenderMixin

    captured = {}

    def _fake_resolve(pid, raw, contact_key=None):
        captured["contact_key"] = contact_key
        return {"enabled": True, "backend": "disabled"}

    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_voice_cfg_for_contact", _fake_resolve)

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    res = types.SimpleNamespace(ok=True, audio_path=str(audio), duration_sec=1.0, error=None)

    from src.ai import tts_pipeline
    import src.client.voice_sender as vs
    monkeypatch.setattr(tts_pipeline.TTSPipeline, "synthesize", AsyncMock(return_value=res))
    monkeypatch.setattr(vs, "send_telegram_voice", AsyncMock(return_value=True))

    class _Cfg:
        config = {"telegram": {"voice_reply": {
            "enabled": True, "trigger": "always", "max_text_chars": 500,
            "max_seconds": 60,
        }}}

        def get(self, k, d=None):
            return d if d is not None else {}

    class _S(TelegramSenderMixin):
        def __init__(self):
            self.config = _Cfg()
            self.client = object()
            self.logger = logging.getLogger("sender_route_test")
            self.account_id = "a"
            self._last_send_wallclock = 0.0
            self.account_persona_ids = []

    s = _S()
    monkeypatch.setattr(s, "_presend_blocked", lambda: False)
    monkeypatch.setattr(s, "_presend_pace", AsyncMock())
    monkeypatch.setattr(s, "_postsend_record_count", lambda: None)
    s._emit_inbox = lambda **kw: None

    msg = types.SimpleNamespace(chat=types.SimpleNamespace(id=424242), id=1, from_user=None)

    async def run():
        out = await s._maybe_send_voice_reply(msg, "hi", is_peer_voice=False)
        assert out is True
        assert captured["contact_key"] == "424242"

    asyncio.run(run())


# ── /api/workspace/metrics 暴露 tts namespace（看板卡片数据源）──────────────
def test_workspace_metrics_exposes_tts_namespace():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.drafts_routes import register_metrics_route
    from src.ai.provider_stats import get_provider_stats

    stats = get_provider_stats("tts", "tts")
    stats.reset()
    stats.record("elevenlabs", ok=True, latency_ms=800, cost_usd=0.15)
    stats.record("edge_tts", ok=True, latency_ms=120)
    stats.record_cache_hit()

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    client = TestClient(app, raise_server_exceptions=True)

    d = client.get("/api/workspace/metrics").json()
    tts = d["providers"]["tts"]
    assert tts["total_cost_usd"] == 0.15
    assert tts["cache_hits"] == 1
    assert 0 < tts["cache_hit_rate"] <= 1
    provs = {r["provider"]: r for r in tts["rows"]}
    assert provs["elevenlabs"]["cost_usd"] == 0.15
    assert provs["edge_tts"]["cost_usd"] == 0.0
    stats.reset()


# ── P4-C：情绪分布 label 计数器 ───────────────────────────────────────────────
def test_provider_stats_record_label_distribution():
    from src.ai.provider_stats import get_provider_stats

    stats = get_provider_stats("tts", "tts")
    stats.reset()
    stats.record_label("warm")
    stats.record_label("warm")
    stats.record_label("playful")
    stats.record_label("")        # 空值忽略
    snap = stats.dump()
    assert snap["labels"] == {"warm": 2, "playful": 1}   # 按计数降序
    # Prometheus 也带出 label
    prom = stats.dump_prom()
    assert 'tts_label_total{label="warm"} 2' in prom
    stats.reset()


def test_pipeline_records_emotion_label(tmp_path, monkeypatch):
    """开启情感层后，非中性合成应记入 provider_stats.labels；neutral 不记。"""
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai.provider_stats import get_provider_stats

    reset_tts_cache()
    stats = get_provider_stats("tts", "tts")
    stats.reset()

    async def fake_edge(self, text, out, voice, spec=None):
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
        })
        await p.synthesize("恭喜你！", emotion="excited")
        await p.synthesize("普通一句", emotion="neutral")   # neutral 不计入分布
        snap = get_provider_stats("tts", "tts").dump()
        assert snap["labels"] == {"excited": 1}

    asyncio.run(run())
    reset_tts_cache()
    stats.reset()
