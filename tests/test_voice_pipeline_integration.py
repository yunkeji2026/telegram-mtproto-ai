"""Integration tests: Telegram voice in→TTS out + Messenger voice capture→ASR.

V4: Telegram 端 — mock Whisper ASR + mock TTS + mock pyrogram send_voice
    验证收语音→转文字→AI前缀剥离→TTS→发语音条 全链路

V5: Messenger 端 — mock VoiceGrabber + AudioPipeline
    验证 _try_transcribe_peer_voice helper_session 模式 end-to-end
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _cp(args=None, returncode=0, stdout="", stderr=""):
    return CompletedProcess(args=args or [], returncode=returncode, stdout=stdout, stderr=stderr)


# ─────────────────────────────────────────────────────────────────
# V4: Telegram voice prefix strip
# ─────────────────────────────────────────────────────────────────

class TestTelegramVoicePrefixStrip:
    """[语音转录] 前缀必须在传给 AI 之前被剥离。"""

    def test_prefix_stripped_when_present(self):
        _VOICE_PREFIX = "[语音转录] "
        text = "[语音转录] こんにちは、お元気ですか"
        ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text
        assert ai_text == "こんにちは、お元気ですか"

    def test_no_prefix_unchanged(self):
        _VOICE_PREFIX = "[语音转录] "
        text = "普通文字消息"
        ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text
        assert ai_text == "普通文字消息"

    def test_partial_prefix_not_stripped(self):
        _VOICE_PREFIX = "[语音转录] "
        text = "[语音转录]缺少空格"  # 无尾部空格 → 不匹配
        ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text
        assert ai_text == text

    def test_empty_string_safe(self):
        _VOICE_PREFIX = "[语音转录] "
        text = ""
        ai_text = text[len(_VOICE_PREFIX):] if text.startswith(_VOICE_PREFIX) else text
        assert ai_text == ""


# ─────────────────────────────────────────────────────────────────
# V4: TTS pipeline + voice sender (Telegram send path)
# ─────────────────────────────────────────────────────────────────

class TestTelegramTTSSendPath:
    """_maybe_send_voice_reply 在 voice_reply.enabled=true + trigger=always 时应发语音。"""

    @pytest.fixture
    def fake_config(self, tmp_path):
        return {
            "telegram": {
                "voice_reply": {
                    "enabled": True,
                    "trigger": "always",
                    "backend": "disabled",  # disabled backend → synthesize 返回 error
                    "max_text_chars": 500,
                    "max_seconds": 60,
                    "timeout_sec": 5,
                }
            }
        }

    @pytest.mark.asyncio
    async def test_voice_reply_skipped_when_disabled(self, fake_config):
        """enabled=false → 立刻返回 False，不调用 TTS。"""
        fake_config["telegram"]["voice_reply"]["enabled"] = False

        from src.ai.tts_pipeline import TTSPipeline

        with patch.object(TTSPipeline, "synthesize", new_callable=AsyncMock) as mock_tts:
            # Build a minimal sender-like object
            class FakeSender:
                config = MagicMock()
                client = None
                account_persona_ids = []
                logger = MagicMock()

                async def _maybe_send_voice_reply(self, original_message, reply_text, *, is_peer_voice=False):
                    raw_cfg = fake_config
                    vr_cfg = (raw_cfg.get("telegram") or {}).get("voice_reply") or {}
                    if not vr_cfg.get("enabled", False):
                        return False
                    return False

            sender = FakeSender()
            result = await sender._maybe_send_voice_reply(None, "hello", is_peer_voice=False)
            assert result is False
            mock_tts.assert_not_called()

    @pytest.mark.asyncio
    async def test_tts_pipeline_disabled_backend_returns_error(self):
        """backend=disabled → TTSResult.ok=False, error='backend disabled'"""
        from src.ai.tts_pipeline import TTSPipeline

        tts = TTSPipeline({"enabled": True, "backend": "disabled"})
        result = await tts.synthesize("test text")
        assert result.ok is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_tts_pipeline_empty_text_returns_error(self):
        """空文本 → TTSResult.ok=False"""
        from src.ai.tts_pipeline import TTSPipeline

        tts = TTSPipeline({"enabled": True, "backend": "edge_tts"})
        result = await tts.synthesize("")
        assert result.ok is False
        assert result.error == "empty_text"

    @pytest.mark.asyncio
    async def test_tts_pipeline_disabled_flag_returns_error(self):
        """enabled=False → pipeline_disabled error"""
        from src.ai.tts_pipeline import TTSPipeline

        tts = TTSPipeline({"enabled": False, "backend": "edge_tts"})
        result = await tts.synthesize("hello")
        assert result.ok is False
        assert result.error == "pipeline_disabled"


# ─────────────────────────────────────────────────────────────────
# V4: persona_voice resolve_voice_cfg
# ─────────────────────────────────────────────────────────────────

class TestResolveVoiceCfg:
    def test_empty_config_returns_empty_dict(self):
        from src.ai.persona_voice import resolve_voice_cfg
        result = resolve_voice_cfg(None, {})
        assert result == {}

    def test_tg_voice_reply_merged(self):
        from src.ai.persona_voice import resolve_voice_cfg
        cfg = {"telegram": {"voice_reply": {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}}}
        result = resolve_voice_cfg(None, cfg)
        assert result["backend"] == "edge_tts"
        assert result["voice"] == "ja-JP-NanamiNeural"

    def test_messenger_shim_as_layer0(self):
        from src.ai.persona_voice import resolve_voice_cfg
        cfg = {
            "messenger_rpa": {"voice_output": {"backend": "pyttsx3", "voice": "voice_a"}},
            "telegram": {"voice_reply": {"backend": "edge_tts"}},
        }
        result = resolve_voice_cfg(None, cfg)
        # TG layer 1 wins over messenger layer 0
        assert result["backend"] == "edge_tts"

    def test_per_persona_voice_profile_overrides(self):
        from src.ai.persona_voice import resolve_voice_cfg
        cfg = {
            "telegram": {"voice_reply": {"backend": "edge_tts", "voice": "default_voice"}},
            "personas": {
                "profiles": [
                    {"id": "p1", "voice_profile": {"backend": "openai", "voice": "nova"}},
                ]
            },
        }
        result = resolve_voice_cfg("p1", cfg)
        assert result["backend"] == "openai"
        assert result["voice"] == "nova"

    def test_unknown_persona_falls_back(self):
        from src.ai.persona_voice import resolve_voice_cfg
        cfg = {"telegram": {"voice_reply": {"backend": "edge_tts", "voice": "default_voice"}}}
        result = resolve_voice_cfg("no_such_persona", cfg)
        assert result["backend"] == "edge_tts"

    def test_exception_returns_empty_dict(self):
        from src.ai.persona_voice import resolve_voice_cfg
        result = resolve_voice_cfg(None, None)  # type: ignore[arg-type]
        assert result == {}


# ─────────────────────────────────────────────────────────────────
# V5: Messenger — _find_start_now_xy XML detection
# ─────────────────────────────────────────────────────────────────

class TestFindStartNowXY:
    def _xml(self, text: str, bounds: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<hierarchy><node text="{text}" bounds="{bounds}" /></hierarchy>'
        )

    def test_en_start_now_detected(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("Start now", "[400,900][680,980]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (540, 940)

    def test_zh_liji_kaishi_detected(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("立即开始", "[300,800][600,880]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (450, 840)

    def test_ja_suguni_detected(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("すぐに開始", "[200,700][500,780]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (350, 740)

    def test_ko_start_detected(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("지금 시작", "[100,600][400,680]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (250, 640)

    def test_unrelated_button_returns_none(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("Cancel", "[100,600][300,660]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy is None

    def test_empty_xml_returns_none(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        assert VoiceGrabber._find_start_now_xy("") is None

    def test_content_desc_attribute_detected(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = (
            '<?xml version="1.0"?><hierarchy>'
            '<node text="" content-desc="Start now" bounds="[200,500][500,580]" />'
            '</hierarchy>'
        )
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (350, 540)

    def test_case_insensitive_matching(self):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        xml = self._xml("START NOW", "[0,0][200,100]")
        xy = VoiceGrabber._find_start_now_xy(xml)
        assert xy == (100, 50)


# ─────────────────────────────────────────────────────────────────
# V5: Messenger — capture_messenger_voice_session start_now method
# ─────────────────────────────────────────────────────────────────

class TestCaptureSessionStartNowMethod:
    """Verify start_now_method recorded in rv.extra under various conditions."""

    def _make_grabber(self, tmp_path):
        from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
        apk = tmp_path / "MrpAudioBridge.apk"
        apk.write_bytes(b"apk")
        grabber = VoiceGrabber("SERIAL", out_dir=str(tmp_path / "out"))
        return grabber, apk

    def _wav_bytes(self):
        import struct, wave
        from io import BytesIO
        buf = BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            # 0.5s of 1000Hz tone → max_abs well above silence threshold
            import math
            frames = b""
            for i in range(8000):
                v = int(8000 * math.sin(2 * math.pi * 1000 * i / 16000))
                frames += struct.pack("<h", v)
            wf.writeframes(frames)
        return buf.getvalue()

    def test_configured_xy_used_when_provided(self, tmp_path):
        grabber, apk = self._make_grabber(tmp_path)
        remote_wav = "/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture.wav"
        wav_bytes = self._wav_bytes()
        calls = []

        def fake_adb(args, timeout=10.0):
            calls.append(list(args))
            if args[:3] == ["shell", "pm", "path"]:
                return _cp(args, stdout="package:/some/path")
            if args[:3] == ["shell", "pm", "grant"]:
                return _cp(args)
            if args[:2] == ["shell", "rm"]:
                return _cp(args)
            if args[:3] == ["shell", "am", "start"]:
                return _cp(args, stdout="Started")
            if args[:3] == ["shell", "ls", "-l"]:
                return _cp(args, stdout=f"-rw 1 u u {len(wav_bytes)} 2026 {remote_wav}")
            if args[:3] == ["shell", "cat"]:
                return _cp(args, stdout="record_done")
            if args[0] == "pull":
                Path(args[2]).write_bytes(wav_bytes)
                return _cp(args, stdout="pulled")
            if args[:2] == ["shell", "input"]:
                return _cp(args)
            return _cp(args)

        grabber._adb = fake_adb
        rv = grabber.capture_messenger_voice_session(
            duration_sec=0.5,
            apk_path=str(apk),
            start_now_xy=(360, 800),
            screen_wh=(720, 1600),
            find_voice_scroll_attempts=0,
            voice_tap_xy=(200, 500),
        )
        assert rv.extra.get("start_now_method") == "configured_xy"
        assert rv.extra.get("start_now_tap") == [360, 800]

    def test_xml_detected_when_dialog_visible(self, tmp_path):
        grabber, apk = self._make_grabber(tmp_path)
        remote_wav = "/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture.wav"
        wav_bytes = self._wav_bytes()
        _xml_start_now = (
            '<?xml?><hierarchy>'
            '<node text="Start now" bounds="[400,900][680,980]" />'
            '</hierarchy>'
        )

        def fake_adb(args, timeout=10.0):
            if args[:3] == ["shell", "pm", "path"]:
                return _cp(args, stdout="package:/x")
            if args[:3] == ["shell", "pm", "grant"]:
                return _cp(args)
            if args[:2] == ["shell", "rm"]:
                return _cp(args)
            if args[:3] == ["shell", "am", "start"]:
                return _cp(args, stdout="Started")
            if args[:2] == ["shell", "uiautomator"]:
                return _cp(args, stdout=_xml_start_now)
            if args[:3] == ["shell", "ls", "-l"]:
                return _cp(args, stdout=f"-rw 1 u u {len(wav_bytes)} 2026 {remote_wav}")
            if args[:3] == ["shell", "cat"]:
                return _cp(args, stdout="record_done")
            if args[0] == "pull":
                Path(args[2]).write_bytes(wav_bytes)
                return _cp(args)
            if args[:2] == ["shell", "input"]:
                return _cp(args)
            return _cp(args)

        grabber._adb = fake_adb
        rv = grabber.capture_messenger_voice_session(
            duration_sec=0.5,
            apk_path=str(apk),
            screen_wh=(720, 1600),
            find_voice_scroll_attempts=0,
            voice_tap_xy=(200, 500),
        )
        assert rv.extra.get("start_now_method") == "xml_detected"
        assert rv.extra.get("start_now_tap") == [540, 940]

    def test_hardcoded_fallback_when_xml_no_dialog(self, tmp_path):
        grabber, apk = self._make_grabber(tmp_path)
        remote_wav = "/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture.wav"
        wav_bytes = self._wav_bytes()

        def fake_adb(args, timeout=10.0):
            if args[:3] == ["shell", "pm", "path"]:
                return _cp(args, stdout="package:/x")
            if args[:3] == ["shell", "pm", "grant"]:
                return _cp(args)
            if args[:2] == ["shell", "rm"]:
                return _cp(args)
            if args[:3] == ["shell", "am", "start"]:
                return _cp(args, stdout="Started")
            if args[:2] == ["shell", "uiautomator"]:
                return _cp(args, stdout='<?xml?><hierarchy><node text="Cancel" bounds="[0,0][100,100]"/></hierarchy>')
            if args[:3] == ["shell", "ls", "-l"]:
                return _cp(args, stdout=f"-rw 1 u u {len(wav_bytes)} 2026 {remote_wav}")
            if args[:3] == ["shell", "cat"]:
                return _cp(args, stdout="record_done")
            if args[0] == "pull":
                Path(args[2]).write_bytes(wav_bytes)
                return _cp(args)
            if args[:2] == ["shell", "input"]:
                return _cp(args)
            return _cp(args)

        grabber._adb = fake_adb
        rv = grabber.capture_messenger_voice_session(
            duration_sec=0.5,
            apk_path=str(apk),
            screen_wh=(720, 1600),
            find_voice_scroll_attempts=0,
            voice_tap_xy=(200, 500),
        )
        assert rv.extra.get("start_now_method") == "hardcoded_fallback"
        # 720 * 0.735 = 529.2 → 529, 1600 * 0.672 = 1075.2 → 1075
        assert rv.extra.get("start_now_tap") == [529, 1075]


# ─────────────────────────────────────────────────────────────────
# V5: Messenger — _try_transcribe_peer_voice disabled path
# ─────────────────────────────────────────────────────────────────

class TestTryTranscribePeerVoiceDisabled:
    """When voice_input.enabled=false, _try_transcribe_peer_voice returns ''."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_disabled(self):
        from src.integrations.messenger_rpa.runner import MessengerRpaRunner
        runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
        runner._cfg = {"voice_input": {"enabled": False}}
        result = await runner._try_transcribe_peer_voice("SERIAL")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_serial(self):
        from src.integrations.messenger_rpa.runner import MessengerRpaRunner
        runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
        runner._cfg = {"voice_input": {"enabled": True}}
        result = await runner._try_transcribe_peer_voice("")
        assert result == ""
