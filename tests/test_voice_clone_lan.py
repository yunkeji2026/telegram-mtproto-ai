"""局域网语音克隆（LAN 优先 → 云端兜底）单测。

覆盖：
  - voice_clone_client 纯函数：请求体构造 / 响应解析 / 健康探测缓存
  - TTSPipeline._should_try_lan 路由判定
  - TTSPipeline._try_lan_clone：在线成功 / 不可用回落 / 失败回落 / 不兜底硬失败
  - resolve_voice_cfg 注入 voice_clone_lan
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from src.ai import voice_clone_client as vcc
from src.ai.tts_pipeline import TTSPipeline, TTSResult


# ── 纯函数 ───────────────────────────────────────────────────────────────────
def test_build_clone_payload_shape():
    raw = vcc.build_clone_payload(
        text="你好", reference_audio_b64="QUJD",
        reference_text="原文", language="ja")
    data = json.loads(raw.decode())
    assert data["text"] == "你好"
    assert data["language"] == "ja"
    assert data["reference_audio_b64"] == "QUJD"
    assert data["reference_text"] == "原文"
    assert data["return_base64"] is True


def test_build_clone_payload_omits_empty_reference_text():
    data = json.loads(vcc.build_clone_payload(
        text="t", reference_audio_b64="QUJD").decode())
    assert "reference_text" not in data


def test_parse_clone_response_ok_false_raises():
    body = json.dumps({"ok": False, "error": "no speech in ref"}).encode()
    with pytest.raises(RuntimeError, match="no speech"):
        vcc.parse_clone_response(body)


def test_parse_clone_response_json_audio_base64():
    audio = b"\x00\x01\x02hello"
    body = json.dumps({"audio_base64": base64.b64encode(audio).decode()}).encode()
    assert vcc.parse_clone_response(body) == audio


def test_parse_clone_response_json_audio_key():
    audio = b"abcd"
    body = json.dumps({"audio": base64.b64encode(audio).decode()}).encode()
    assert vcc.parse_clone_response(body) == audio


def test_parse_clone_response_raw_bytes():
    raw = b"\xff\xf3rawmp3bytes"
    assert vcc.parse_clone_response(raw) == raw


def test_parse_clone_response_empty_raises():
    with pytest.raises(RuntimeError):
        vcc.parse_clone_response(b"")


def test_parse_clone_response_json_no_audio_raises():
    with pytest.raises(RuntimeError):
        vcc.parse_clone_response(json.dumps({"oops": 1}).encode())


def test_health_ok_uses_cache(monkeypatch):
    vcc.reset_health_cache()
    calls = {"n": 0}

    def fake_probe(self):
        calls["n"] += 1
        return True

    monkeypatch.setattr(vcc.VoiceCloneClient, "_probe_health", fake_probe)
    client = vcc.VoiceCloneClient({"base_url": "http://lan:8000", "health_cache_sec": 60})
    assert client.health_ok() is True
    assert client.health_ok() is True  # 命中缓存
    assert calls["n"] == 1
    assert client.health_ok(use_cache=False) is True  # 强制重探
    assert calls["n"] == 2


def test_health_ok_cache_expires(monkeypatch):
    vcc.reset_health_cache()
    calls = {"n": 0}
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "_probe_health",
        lambda self: (calls.__setitem__("n", calls["n"] + 1) or True))
    client = vcc.VoiceCloneClient({"base_url": "http://lan:8000", "health_cache_sec": 0.05})
    client.health_ok()
    time.sleep(0.12)
    client.health_ok()
    assert calls["n"] == 2  # 缓存过期 → 重探


# ── TTSPipeline 路由 ─────────────────────────────────────────────────────────
def _ref_audio(tmp_path: Path) -> Path:
    p = tmp_path / "ref.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return p


def _pipeline(tmp_path: Path, *, lan_enabled=True, fallback=True, with_ref=True):
    ref = _ref_audio(tmp_path)
    return TTSPipeline({
        "enabled": True,
        "backend": "edge_tts",
        "format": "mp3",
        "out_dir": str(tmp_path / "out"),
        "voice_profile": {
            "enabled": True,
            "owner_consent": True,
            "reference_audio_path": str(ref) if with_ref else "",
        },
        "voice_clone_lan": {
            "enabled": lan_enabled,
            "base_url": "http://lan:8000",
            "cloud_fallback": fallback,
        },
    })


def test_should_try_lan_true(tmp_path):
    assert _pipeline(tmp_path)._should_try_lan() is True


def test_should_try_lan_false_when_lan_disabled(tmp_path):
    assert _pipeline(tmp_path, lan_enabled=False)._should_try_lan() is False


def test_should_try_lan_false_without_reference(tmp_path):
    assert _pipeline(tmp_path, with_ref=False)._should_try_lan() is False


async def test_synthesize_lan_success(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def fake_clone(self, text, ref, out, *, reference_text=""):
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    rv = await p.synthesize("你好世界")
    assert rv.ok is True
    assert rv.provider == "voice_clone_lan"
    assert rv.format == "wav"
    assert Path(rv.audio_path).is_file()
    assert rv.audio_path.endswith(".wav")
    assert rv.extra.get("lan_base_url") == "http://lan:8000"


async def test_lan_unreachable_returns_none_for_fallback(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    p = _pipeline(tmp_path, fallback=True)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is None  # → 回落云端


async def test_lan_unreachable_hard_fail_without_fallback(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    p = _pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is rv
    assert res.error == "voice_clone_lan_unreachable"


async def test_lan_synth_failure_falls_back(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def boom(self, text, ref, out, *, reference_text=""):
        raise RuntimeError("server 500")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", boom)
    p = _pipeline(tmp_path, fallback=True)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is None  # 失败但允许兜底


async def test_lan_synth_failure_no_fallback_errors(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "synthesize_clone",
        lambda self, text, ref, out, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    p = _pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is rv
    assert res.error.startswith("voice_clone_lan_failed")


# ── resolve_voice_cfg 注入 ───────────────────────────────────────────────────
def test_resolve_voice_cfg_injects_lan():
    from src.ai.persona_voice import resolve_voice_cfg
    full = {
        "telegram": {"voice_reply": {"backend": "edge_tts"}},
        "voice_clone_lan": {"enabled": True, "base_url": "http://lan:8000"},
    }
    cfg = resolve_voice_cfg(None, full)
    assert cfg.get("voice_clone_lan", {}).get("enabled") is True
    assert cfg["voice_clone_lan"]["base_url"] == "http://lan:8000"
