"""实时共情语音：纯函数核心 + 主机客户端 + Track A 克隆 instructions 单测。"""
from __future__ import annotations

import json

from src.ai.realtime_voice import (
    EV_ERROR,
    EV_INPUT_AUDIO,
    EV_INTERRUPT,
    EV_READY,
    EV_SESSION_INIT,
    EV_TRANSCRIPT_ASSISTANT,
    RealtimeVoiceConfig,
    build_call_system_prompt,
    build_session_init,
    dumps_event,
    input_audio_event,
    interrupt_event,
    parse_host_event,
    pick_language,
)


# ── RealtimeVoiceConfig ──────────────────────────────────────────────────────
def test_config_defaults_disabled():
    c = RealtimeVoiceConfig.from_config(None)
    assert c.enabled is False
    c2 = RealtimeVoiceConfig.from_config({})
    assert c2.enabled is False
    c3 = RealtimeVoiceConfig.from_config({"realtime_voice": {}})
    assert c3.enabled is False


def test_config_opener_enabled_default_and_toggle():
    """通话主动开场白：默认开；opener.enabled / opener_enabled 均可关。"""
    assert RealtimeVoiceConfig.from_config(None).opener_enabled is True
    assert RealtimeVoiceConfig.from_config({"realtime_voice": {}}).opener_enabled is True
    assert RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"opener": {"enabled": False}}}).opener_enabled is False
    assert RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"opener_enabled": False}}).opener_enabled is False


def test_config_subtitle_defaults_and_toggle():
    """双语字幕：默认开、目标 zh；subtitle.enabled/lang 可调；不支持语言回落 zh。"""
    c = RealtimeVoiceConfig.from_config(None)
    assert c.subtitle_enabled is True and c.subtitle_lang == "zh"
    c2 = RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"subtitle": {"enabled": False, "lang": "en"}}})
    assert c2.subtitle_enabled is False and c2.subtitle_lang == "en"
    c3 = RealtimeVoiceConfig.from_config({"realtime_voice": {"subtitle": {"lang": "fr"}}})
    assert c3.subtitle_lang == "zh"


def test_config_parses_block_and_clamps_language():
    c = RealtimeVoiceConfig.from_config({"realtime_voice": {
        "enabled": True,
        "base_url": "https://gpu-host:7860/",
        "default_language": "fr",   # 不支持 → 回落 zh
        "sample_rate": 24000,
        "api_key": "sk-x",
    }})
    assert c.enabled is True
    assert c.base_url == "https://gpu-host:7860"   # 去尾斜杠
    assert c.default_language == "zh"
    assert c.sample_rate == 24000
    assert c.api_key == "sk-x"


def test_config_ws_url_scheme_swap():
    assert RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"base_url": "http://h:7860", "ws_path": "/v1/realtime"}}
    ).ws_url() == "ws://h:7860/v1/realtime"
    assert RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"base_url": "https://h:7860", "ws_path": "/rt"}}
    ).ws_url() == "wss://h:7860/rt"


# ── pick_language ────────────────────────────────────────────────────────────
def test_pick_language():
    assert pick_language("你好世界") == "zh"
    assert pick_language("hello there") == "en"
    assert pick_language("", default="en") == "en"
    assert pick_language("   ", default="zh") == "zh"
    assert pick_language("123 !!!", default="en") == "en"   # 无字母无CJK → default
    assert pick_language("你好 hello") == "zh"               # 含CJK优先 zh


# ── build_call_system_prompt ─────────────────────────────────────────────────
def test_system_prompt_uses_base_prompt_and_memory():
    p = build_call_system_prompt(
        base_prompt="你是林小雨，温柔体贴的护士。",
        memory_bullets=["对方上周失眠", "喜欢猫"],
        language="zh",
    )
    assert "林小雨" in p
    assert "对方上周失眠" in p and "喜欢猫" in p
    assert "实时语音通话" in p           # 默认共情守则
    assert "中文" in p


def test_system_prompt_persona_fallback_when_no_base():
    p = build_call_system_prompt(
        persona={"name": "Aria", "role": "知心姐姐",
                 "personality": {"traits": ["温暖", "幽默"], "style": "轻声细语"}},
        language="en",
    )
    assert "Aria" in p
    assert "温暖" in p
    assert "English" in p


def test_system_prompt_empty_inputs_safe():
    p = build_call_system_prompt(language="zh")
    assert isinstance(p, str) and p.strip()      # 仍给出守则，不空


def test_system_prompt_carries_emotion_tone():
    """首句情绪锚：emotion_tone 给了就并入系统提示；空则不加（向后兼容）。"""
    p = build_call_system_prompt(base_prompt="你是林小雨。", language="zh",
                                 emotion_tone="你天然的语气基调偏【俏皮、活泼】。")
    assert "俏皮、活泼" in p
    p2 = build_call_system_prompt(base_prompt="你是林小雨。", language="zh")
    assert "俏皮" not in p2


# ── build_session_init ───────────────────────────────────────────────────────
def test_session_init_voice_ref_takes_precedence():
    init = build_session_init(
        system_prompt="hi", language="zh",
        voice_ref_b64="QUJD", voice="builtin_a", sample_rate=16000)
    assert init["type"] == EV_SESSION_INIT
    assert init["voice_ref_b64"] == "QUJD"
    assert "voice" not in init          # 有参考音 → 不带内置音色名
    assert init["language"] == "zh"
    assert init["sample_rate"] == 16000


def test_session_init_builtin_voice_when_no_ref_and_lang_clamp():
    init = build_session_init(
        system_prompt="hi", language="de", voice="builtin_a", sample_rate=0)
    assert init["voice"] == "builtin_a"
    assert "voice_ref_b64" not in init
    assert init["language"] == "zh"     # 不支持语种 → zh
    assert init["sample_rate"] == 16000  # 0 → 默认


# ── 事件序列化/解析 ───────────────────────────────────────────────────────────
def test_input_audio_and_interrupt_events():
    ev = input_audio_event("QUJD", seq=3)
    assert ev["type"] == EV_INPUT_AUDIO and ev["audio_b64"] == "QUJD" and ev["seq"] == 3
    assert interrupt_event()["type"] == EV_INTERRUPT


def test_dumps_event_roundtrip_unicode():
    s = dumps_event({"type": EV_TRANSCRIPT_ASSISTANT, "text": "你好"})
    assert "你好" in s                  # ensure_ascii=False
    assert json.loads(s)["text"] == "你好"


def test_parse_host_event_variants():
    assert parse_host_event({"type": EV_READY})["type"] == EV_READY
    assert parse_host_event(json.dumps({"type": EV_TRANSCRIPT_ASSISTANT, "text": "hi"})
                            )["text"] == "hi"
    assert parse_host_event(json.dumps({"type": EV_READY}).encode())["type"] == EV_READY
    assert parse_host_event("not json")["type"] == EV_ERROR
    assert parse_host_event(b"\xff\xfe")["type"] == EV_ERROR
    assert parse_host_event("")["type"] == EV_ERROR
    assert parse_host_event(123)["type"] == EV_ERROR
    # 未知事件类型 → error（不放行未知契约）
    assert parse_host_event({"type": "bogus"})["type"] == EV_ERROR


# ── 主机客户端：健康探测缓存 + websockets 可用性 ─────────────────────────────
def test_health_ok_uses_cache(monkeypatch):
    from src.ai import realtime_voice_client as rvc

    rvc.reset_health_cache()
    calls = {"n": 0}
    monkeypatch.setattr(
        rvc.RealtimeVoiceClient, "_probe_health",
        lambda self: (calls.__setitem__("n", calls["n"] + 1) or True))
    client = rvc.RealtimeVoiceClient(
        RealtimeVoiceConfig.from_config(
            {"realtime_voice": {"base_url": "http://rt:7860", "health_cache_sec": 60}}))
    assert client.health_ok() is True
    assert client.health_ok() is True   # 命中缓存
    assert calls["n"] == 1
    assert client.health_ok(use_cache=False) is True
    assert calls["n"] == 2


def test_websockets_available_returns_bool():
    from src.ai.realtime_voice_client import websockets_available
    assert isinstance(websockets_available(), bool)


# ── Track A：克隆 instructions 结构化字段（不会被读出）──────────────────────────
def test_build_clone_payload_includes_instructions_when_set():
    from src.ai import voice_clone_client as vcc
    body = json.loads(vcc.build_clone_payload(
        text="你好", reference_audio_b64="QUJD",
        instructions="用温暖的语气说").decode())
    assert body["instructions"] == "用温暖的语气说"


def test_build_clone_payload_omits_empty_instructions():
    from src.ai import voice_clone_client as vcc
    body = json.loads(vcc.build_clone_payload(
        text="你好", reference_audio_b64="QUJD").decode())
    assert "instructions" not in body
