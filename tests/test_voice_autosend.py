"""全自动语音回复（src/inbox/voice_autosend.py）单测。

覆盖：
  - should_send_voice 决策矩阵（enabled/长度/trigger × peer_sent_voice）。
  - resolve_voice_autosend_cfg 缺失/存在。
  - stage_voice_file 全链（mock TTS/转码/落盘）成功路径 + 合成失败回 None。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.inbox import voice_autosend as va


# ── resolve_voice_autosend_cfg ──────────────────────────────────────

def test_resolve_cfg_missing_returns_empty():
    assert va.resolve_voice_autosend_cfg({}) == {}
    assert va.resolve_voice_autosend_cfg({"inbox": {}}) == {}
    assert va.resolve_voice_autosend_cfg({"inbox": {"l2_autosend": {}}}) == {}


def test_resolve_cfg_present():
    cfg = {"inbox": {"l2_autosend": {"voice": {"enabled": True, "trigger": "always"}}}}
    out = va.resolve_voice_autosend_cfg(cfg)
    assert out["enabled"] is True
    assert out["trigger"] == "always"


# ── should_send_voice 矩阵 ──────────────────────────────────────────

def test_disabled_block_never_sends():
    assert va.should_send_voice({}, "hi") is False
    assert va.should_send_voice({"enabled": False}, "hi") is False


def test_empty_text_never_sends():
    assert va.should_send_voice({"enabled": True, "trigger": "always"}, "") is False
    assert va.should_send_voice({"enabled": True, "trigger": "always"}, "   ") is False


def test_length_guards():
    vb = {"enabled": True, "trigger": "always", "min_chars": 3, "max_chars": 10}
    assert va.should_send_voice(vb, "ab") is False          # < min
    assert va.should_send_voice(vb, "abc") is True           # == min
    assert va.should_send_voice(vb, "a" * 10) is True        # == max
    assert va.should_send_voice(vb, "a" * 11) is False       # > max


def test_trigger_never():
    vb = {"enabled": True, "trigger": "never"}
    assert va.should_send_voice(vb, "hello", peer_sent_voice=True) is False


def test_trigger_always():
    vb = {"enabled": True, "trigger": "always"}
    assert va.should_send_voice(vb, "hello", peer_sent_voice=False) is True


def test_trigger_when_peer_voice():
    vb = {"enabled": True, "trigger": "when_peer_voice"}
    assert va.should_send_voice(vb, "hello", peer_sent_voice=True) is True
    assert va.should_send_voice(vb, "hello", peer_sent_voice=False) is False


def test_invalid_trigger_falls_back_to_when_peer_voice():
    vb = {"enabled": True, "trigger": "garbage"}
    assert va.should_send_voice(vb, "hello", peer_sent_voice=True) is True
    assert va.should_send_voice(vb, "hello", peer_sent_voice=False) is False


def test_default_trigger_is_when_peer_voice():
    vb = {"enabled": True}  # 无 trigger → 默认 when_peer_voice
    assert va.should_send_voice(vb, "hello", peer_sent_voice=True) is True
    assert va.should_send_voice(vb, "hello", peer_sent_voice=False) is False


# ── stage_voice_file 全链 ───────────────────────────────────────────

class _FakeResult:
    def __init__(self, ok, audio_path):
        self.ok = ok
        self.audio_path = audio_path


class _FakeTTS:
    _audio_path = ""
    _ok = True

    def __init__(self, cfg):
        self.cfg = cfg

    async def synthesize(self, text, timeout_sec=45.0):
        return _FakeResult(self._ok, self._audio_path)


@pytest.mark.asyncio
async def test_stage_voice_file_success(monkeypatch):
    # 造一个真实临时音频文件，假 TTS 返回它，转码原样返回，落盘 mock
    fd, audio = tempfile.mkstemp(suffix=".ogg")
    os.write(fd, b"OGGfakebytes")
    os.close(fd)

    monkeypatch.setattr("src.ai.persona_voice.resolve_voice_cfg",
                        lambda pid, cfg: {"backend": "fake"})
    _FakeTTS._audio_path = audio
    _FakeTTS._ok = True
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTS)
    monkeypatch.setattr("src.client.voice_sender.convert_to_ogg_opus",
                        lambda p, delete_src=True: p)

    captured = {}

    def _fake_save(platform, account_id, name, data):
        captured["platform"] = platform
        captured["name"] = name
        captured["data"] = data
        return ("/local/out.ogg", "/static/protocol_media/x/out.ogg", "voice")

    monkeypatch.setattr("src.integrations.protocol_bridge.save_outbound_media", _fake_save)

    out = await va.stage_voice_file({}, "telegram", "acct1", "persona1", "你好")
    assert out == ("/local/out.ogg", "/static/protocol_media/x/out.ogg")
    assert captured["platform"] == "telegram"
    assert captured["data"] == b"OGGfakebytes"
    # 合成临时文件已被清理
    assert not os.path.exists(audio)


# ── 可观测性计数 ────────────────────────────────────────────────────

def test_voice_metrics_record_and_snapshot():
    before = va.metrics_snapshot()
    va.record_voice_sent(1234)
    va.record_voice_fallback("synth_failed")
    after = va.metrics_snapshot()
    assert after["sent"] == before["sent"] + 1
    assert after["fallback"] == before["fallback"] + 1
    assert after["last_reason"] == "synth_failed"
    assert after["last_duration_ms"] == 1234
    assert after["last_ts"] > 0


def test_voice_metrics_ignores_nonpositive_duration():
    va.record_voice_sent(1500)
    snap1 = va.metrics_snapshot()
    va.record_voice_sent(0)  # 时长无效 → 不覆盖 last_duration_ms
    snap2 = va.metrics_snapshot()
    assert snap2["last_duration_ms"] == snap1["last_duration_ms"] == 1500
    assert snap2["sent"] == snap1["sent"] + 1


@pytest.mark.asyncio
async def test_stage_voice_file_tts_failure_returns_none(monkeypatch):
    monkeypatch.setattr("src.ai.persona_voice.resolve_voice_cfg",
                        lambda pid, cfg: {"backend": "fake"})
    _FakeTTS._audio_path = ""
    _FakeTTS._ok = False
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTS)

    out = await va.stage_voice_file({}, "telegram", "acct1", "persona1", "你好")
    assert out is None
