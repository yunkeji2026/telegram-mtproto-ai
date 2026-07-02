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


# ── trigger=smart（委托 voice_fitness 上下文评分）──────────────────────

def test_smart_trigger_peer_voice_sends():
    vb = {"enabled": True, "trigger": "smart", "max_chars": 120}
    assert va.should_send_voice(vb, "好的呀", peer_sent_voice=True) is True


def test_smart_trigger_neutral_filler_falls_back_to_text():
    vb = {"enabled": True, "trigger": "smart", "max_chars": 120}
    # 中性短句 + 无对等 + 无额外信号 → 低分 → 文字
    assert va.should_send_voice(vb, "嗯嗯好的") is False


def test_smart_trigger_respects_length_guard():
    vb = {"enabled": True, "trigger": "smart", "max_chars": 10}
    # 超 max_chars → 通用长度护栏先拦（不进评分）
    assert va.should_send_voice(
        vb, "这是一句超过十个字的很长回复内容", peer_sent_voice=True) is False


def test_smart_trigger_url_is_text():
    vb = {"enabled": True, "trigger": "smart", "max_chars": 120}
    # 含网址 → voice_fitness 硬否决（即便客户发了语音）
    assert va.should_send_voice(vb, "看这个 https://x.com", peer_sent_voice=True) is False


# ── decide_voice（带 reason 的统一决策入口）──────────────────────────

def test_decide_voice_disabled_reason():
    d = va.decide_voice({}, "hi")
    assert d.send_voice is False and d.reason == "disabled"


def test_decide_voice_empty_reason():
    assert va.decide_voice({"enabled": True}, "  ").reason == "empty"


def test_decide_voice_when_peer_voice_reasons():
    vb = {"enabled": True, "trigger": "when_peer_voice"}
    assert va.decide_voice(vb, "hi", peer_sent_voice=True).reason == "peer_voice"
    assert va.decide_voice(vb, "hi", peer_sent_voice=False).reason == "no_peer_voice"


def test_decide_voice_always_and_never_reasons():
    assert va.decide_voice(
        {"enabled": True, "trigger": "always"}, "hi").reason == "trigger_always"
    assert va.decide_voice(
        {"enabled": True, "trigger": "never"}, "hi").reason == "trigger_never"


def test_decide_voice_too_long_reason():
    vb = {"enabled": True, "trigger": "always", "max_chars": 5}
    d = va.decide_voice(vb, "123456")
    assert d.send_voice is False and d.reason == "too_long"


def test_decide_voice_smart_url_reason():
    vb = {"enabled": True, "trigger": "smart", "max_chars": 120}
    d = va.decide_voice(vb, "看这个 https://x.com", peer_sent_voice=True)
    assert d.send_voice is False and d.reason == "unspeakable"


def test_should_send_voice_is_decide_voice_projection():
    vb = {"enabled": True, "trigger": "when_peer_voice"}
    assert va.should_send_voice(vb, "hi", peer_sent_voice=True) is \
        va.decide_voice(vb, "hi", peer_sent_voice=True).send_voice


# ── record_voice_decision（观测计数）─────────────────────────────────

def test_record_voice_decision_counts():
    before = va.metrics_snapshot()
    va.record_voice_decision(True, "peer_voice")
    va.record_voice_decision(False, "low_fitness")
    after = va.metrics_snapshot()
    assert after["voice_chosen"] == before["voice_chosen"] + 1
    assert after["text_chosen"] == before["text_chosen"] + 1
    assert after["decision_reasons"].get("low_fitness", 0) == \
        before["decision_reasons"].get("low_fitness", 0) + 1
    assert after["last_decision"] == "text:low_fitness"


# ── persona_allowed_for_voice（Phase2 人设级灰度白名单）─────────────────

def test_persona_allowlist_absent_allows_all():
    # 无 persona_allowlist key → 不限制（向后兼容：所有人设按各自 voice_profile 发声）
    assert va.persona_allowed_for_voice({"enabled": True}, "anyone") is True
    assert va.persona_allowed_for_voice({"enabled": True}, "") is True


def test_persona_allowlist_empty_allows_all():
    assert va.persona_allowed_for_voice({"persona_allowlist": []}, "anyone") is True
    assert va.persona_allowed_for_voice({"persona_allowlist": None}, "anyone") is True


def test_persona_allowlist_hit_allows_voice():
    vb = {"persona_allowlist": ["lin_xiaoyu"]}
    assert va.persona_allowed_for_voice(vb, "lin_xiaoyu") is True


def test_persona_allowlist_miss_falls_back_to_text():
    vb = {"persona_allowlist": ["lin_xiaoyu"]}
    assert va.persona_allowed_for_voice(vb, "chen_meiling") is False


def test_persona_allowlist_empty_pid_blocked_when_restricted():
    # 名单非空但真实人设未解析出（空 pid）→ 保守不放行（回落文本，绝不误发）
    vb = {"persona_allowlist": ["lin_xiaoyu"]}
    assert va.persona_allowed_for_voice(vb, "") is False
    assert va.persona_allowed_for_voice(vb, None) is False


def test_persona_allowlist_strips_and_dedups():
    vb = {"persona_allowlist": [" lin_xiaoyu ", "", "lin_xiaoyu"]}
    assert va.persona_allowed_for_voice(vb, " lin_xiaoyu ") is True
    assert va.persona_allowed_for_voice(vb, "lin_xiaoyu") is True
    assert va.persona_allowed_for_voice(vb, "other") is False


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

    async def synthesize(self, text, timeout_sec=45.0, emotion=None):
        return _FakeResult(self._ok, self._audio_path)


@pytest.mark.asyncio
async def test_stage_voice_file_success(monkeypatch):
    # 造一个真实临时音频文件，假 TTS 返回它，转码原样返回，落盘 mock
    fd, audio = tempfile.mkstemp(suffix=".ogg")
    os.write(fd, b"OGGfakebytes")
    os.close(fd)

    monkeypatch.setattr("src.ai.persona_voice.resolve_voice_cfg",
                        lambda pid, cfg, tier=None: {"backend": "fake"})
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
                        lambda pid, cfg, tier=None: {"backend": "fake"})
    _FakeTTS._audio_path = ""
    _FakeTTS._ok = False
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTS)

    out = await va.stage_voice_file({}, "telegram", "acct1", "persona1", "你好")
    assert out is None
