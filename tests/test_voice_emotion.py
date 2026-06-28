"""voice_emotion 纯函数 + TTSPipeline 情感/缓存接线单测。"""
from __future__ import annotations

import asyncio

from src.ai.voice_emotion import (
    EmotionSpec,
    NEUTRAL,
    coerce_emotion,
    derive_emotion,
    edge_prosody,
    to_elevenlabs_text,
    to_openai_instructions,
)


# ── EmotionSpec 规整 ─────────────────────────────────────────────────────────
def test_emotion_spec_normalizes_bad_input():
    s = EmotionSpec(emotion="不存在的情绪", intensity=5.0, pace="huh")
    assert s.emotion == "neutral"
    assert s.intensity == 1.0          # 超界裁剪到 [0,1]
    assert s.pace == "normal"
    assert s.is_neutral() is True
    assert s.cache_key() == ""         # neutral → 空键（== 无情绪）


def test_emotion_spec_valid_values_kept():
    s = EmotionSpec(emotion="WARM", intensity=0.3, pace="slow")
    assert s.emotion == "warm"
    assert abs(s.intensity - 0.3) < 1e-6
    assert s.pace == "slow"
    assert s.cache_key() == "warm:0.3:slow"


# ── derive_emotion ───────────────────────────────────────────────────────────
def test_derive_emotion_low_csat_overrides_to_empathetic():
    s = derive_emotion(csat=1.0, intent="praise", text="太好了！")
    assert s.emotion == "empathetic"
    assert s.pace == "slow"


def test_derive_emotion_intent_complaint():
    assert derive_emotion(intent="customer_complaint").emotion == "empathetic"
    assert derive_emotion(intent="退款申请").emotion == "apologetic"


def test_derive_emotion_text_cues():
    assert derive_emotion(text="哈哈哈你真逗").emotion == "playful"
    assert derive_emotion(text="谢谢你帮我").emotion == "warm"
    assert derive_emotion(text="对不起让你久等了").emotion == "apologetic"
    assert derive_emotion(text="太好了恭喜你！！").emotion == "excited"


def test_derive_emotion_relationship_stage_and_default():
    assert derive_emotion(rel_stage="intimate").emotion == "playful"
    assert derive_emotion(rel_stage="stranger").emotion == "warm"
    # 无任何信号 → default
    assert derive_emotion().emotion == "warm"
    assert derive_emotion(default="calm").emotion == "calm"


# ── 引擎映射 ─────────────────────────────────────────────────────────────────
def test_openai_instructions_neutral_is_passthrough():
    assert to_openai_instructions(NEUTRAL, base="保持简洁") == "保持简洁"
    assert to_openai_instructions(NEUTRAL) == ""


def test_openai_instructions_appends_tone_after_base():
    out = to_openai_instructions(EmotionSpec("warm", intensity=0.6), base="保持简洁")
    assert out.startswith("保持简洁")
    assert "温暖" in out


def test_openai_instructions_intensity_degree():
    strong = to_openai_instructions(EmotionSpec("excited", intensity=0.9))
    assert "强烈地" in strong


def test_elevenlabs_text_injects_tag():
    assert to_elevenlabs_text("你好", NEUTRAL) == "你好"
    assert to_elevenlabs_text("你好", EmotionSpec("warm")) == "[warmly] 你好"
    assert to_elevenlabs_text("", EmotionSpec("warm")) == ""


def test_edge_prosody_neutral_empty_else_has_rate():
    assert edge_prosody(NEUTRAL) == {}
    p = edge_prosody(EmotionSpec("excited", intensity=1.0))
    assert "rate" in p and p["rate"].endswith("%")
    assert "pitch" in p and p["pitch"].endswith("Hz")


def test_coerce_emotion_variants():
    assert coerce_emotion(None).is_neutral()
    assert coerce_emotion("warm").emotion == "warm"
    assert coerce_emotion({"emotion": "sad", "intensity": 0.9}).emotion == "sad"
    spec = EmotionSpec("calm")
    assert coerce_emotion(spec) is spec


# ── TTSPipeline 缓存命中 ─────────────────────────────────────────────────────
def test_tts_cache_hits_second_call(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    calls = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        calls["n"] += 1
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
        })
        r1 = await p.synthesize("你好世界")
        r2 = await p.synthesize("你好世界")
        assert r1.ok and r2.ok
        assert calls["n"] == 1                       # 第二次未再合成
        assert not r1.extra.get("cache_hit")
        assert r2.extra.get("cache_hit") is True

    asyncio.run(run())
    reset_tts_cache()


def test_tts_cache_disabled_recomputes(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    calls = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        calls["n"] += 1
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
            "tts_cache": {"enabled": False},
        })
        await p.synthesize("重复句")
        await p.synthesize("重复句")
        assert calls["n"] == 2                       # 关缓存 → 每次都合成

    asyncio.run(run())
    reset_tts_cache()


def test_tts_emotion_passed_to_edge(tmp_path, monkeypatch):
    """显式传 emotion → edge backend 收到非 neutral spec（rate/pitch 生效）。"""
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    seen = {"spec": None}

    async def fake_edge(self, text, out, voice, spec=None):
        seen["spec"] = spec
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
        })
        await p.synthesize("恭喜你！", emotion="excited")
        assert seen["spec"] is not None
        assert seen["spec"].emotion == "excited"
        assert not seen["spec"].is_neutral()

    asyncio.run(run())
    reset_tts_cache()


def test_tts_emotion_cache_key_differs(tmp_path, monkeypatch):
    """同文本不同情绪应各自缓存，不串味。"""
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    calls = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        calls["n"] += 1
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
        })
        await p.synthesize("一样的话", emotion="warm")
        await p.synthesize("一样的话", emotion="excited")
        assert calls["n"] == 2                       # 情绪不同 → 不同键 → 各合成一次
        await p.synthesize("一样的话", emotion="warm")
        assert calls["n"] == 2                       # warm 第二次命中缓存

    asyncio.run(run())
    reset_tts_cache()


# ── resolve_emotion_for_send 共享接缝（sender/autosend/收件箱 三入口共用）──────
def test_resolve_emotion_for_send_disabled_returns_none():
    from src.ai.persona_voice import resolve_emotion_for_send
    # emotion 块缺失 → None（调用点传 emotion=None → neutral，零行为变更）
    assert resolve_emotion_for_send({}, "你好呀") is None
    # 显式关 → None
    assert resolve_emotion_for_send(
        {"emotion": {"enabled": False}}, "你好呀") is None


def test_resolve_emotion_for_send_enabled_text_cue():
    from src.ai.persona_voice import resolve_emotion_for_send
    # 开启 + 无 provider → 仍可用文本线索派生（"哈哈" → playful）
    spec = resolve_emotion_for_send(
        {"emotion": {"enabled": True}}, "哈哈哈你太逗了")
    assert spec is not None
    assert spec.emotion == "playful"


def test_resolve_emotion_for_send_uses_rel_stage():
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    # funnel provider 返回 intimate → 无文本线索时派生 playful
    set_relationship_providers(
        funnel_lookup=lambda *, channel, account_id, external_id: "intimate")
    try:
        spec = resolve_emotion_for_send(
            {"emotion": {"enabled": True}}, "在吗",
            platform="telegram", account_id="a1", chat_key="u1")
        assert spec is not None
        assert spec.emotion == "playful"
    finally:
        reset_relationship_providers()


def test_resolve_emotion_for_send_default_when_no_signal():
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.utils.companion_context import reset_relationship_providers

    reset_relationship_providers()
    spec = resolve_emotion_for_send(
        {"emotion": {"enabled": True, "default": "calm"}}, "随便一句话")
    assert spec is not None
    assert spec.emotion == "calm"     # 无信号 → 配置默认基调


def test_resolve_emotion_for_send_provider_error_safe():
    from src.ai.persona_voice import resolve_emotion_for_send
    from src.utils.companion_context import (
        set_relationship_providers, reset_relationship_providers,
    )

    def _boom(*, channel, account_id, external_id):
        raise RuntimeError("funnel down")

    set_relationship_providers(funnel_lookup=_boom)
    try:
        # provider 抛错 → 吞掉，仍用文本线索派生（不抛给 TTS 主流程）
        spec = resolve_emotion_for_send(
            {"emotion": {"enabled": True}}, "谢谢你帮我",
            account_id="a1", chat_key="u1")
        assert spec is not None
        assert spec.emotion == "warm"   # "谢谢" 文本线索
    finally:
        reset_relationship_providers()
