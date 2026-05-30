"""WhatsApp 语音消息检测 + voice_grabber 单元测试。"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.whatsapp_rpa.ui_hierarchy import (
    WaVoiceMessage,
    detect_last_incoming_voice,
    detect_voice_messages,
    find_attach_button,
)


# ── 测试用 XML 片段 ──────────────────────────────────────────────────────────

_VOICE_XML = textwrap.dedent("""\
<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="com.whatsapp"
        bounds="[0,0][720,1600]">
    <!-- 对方语音消息 (incoming, 左侧) -->
    <node class="android.widget.RelativeLayout" bounds="[14,1200][450,1380]">
      <node resource-id="com.whatsapp:id/control_btn"
            content-desc="播放语音消息"
            class="android.widget.ImageButton"
            bounds="[28,1260][96,1368]" />
      <node resource-id="com.whatsapp:id/audio_seekbar"
            content-desc="语音消息进度条"
            class="android.widget.SeekBar"
            bounds="[100,1280][400,1340]" />
      <node resource-id="com.whatsapp:id/description"
            text="0:05"
            class="android.widget.TextView"
            bounds="[100,1340][160,1370]" />
    </node>
    <!-- 自己发的语音消息 (outgoing, 右侧) -->
    <node class="android.widget.RelativeLayout" bounds="[300,1400][706,1560]">
      <node resource-id="com.whatsapp:id/control_btn"
            content-desc="Play voice message"
            class="android.widget.ImageButton"
            bounds="[520,1430][588,1538]" />
      <node resource-id="com.whatsapp:id/audio_seekbar"
            content-desc="voice message seekbar"
            class="android.widget.SeekBar"
            bounds="[340,1450][510,1510]" />
      <node resource-id="com.whatsapp:id/description"
            text="0:12"
            class="android.widget.TextView"
            bounds="[340,1510][400,1540]" />
    </node>
    <!-- 附件按钮 -->
    <node resource-id="com.whatsapp:id/input_attach_button"
          content-desc="附件"
          class="android.widget.ImageButton"
          bounds="[620,1540][690,1590]" />
  </node>
</hierarchy>
""").encode("utf-8")

_TEXT_ONLY_XML = textwrap.dedent("""\
<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="com.whatsapp"
        bounds="[0,0][720,1600]">
    <node resource-id="com.whatsapp:id/message_text"
          text="Hello world"
          class="android.widget.TextView"
          bounds="[14,1200][400,1260]" />
  </node>
</hierarchy>
""").encode("utf-8")

_EMPTY_XML = b'<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0"></hierarchy>'


# ── detect_voice_messages ────────────────────────────────────────────────────

class TestDetectVoiceMessages:
    def test_detects_two_voice_messages(self):
        voices = detect_voice_messages(_VOICE_XML, screen_width=720)
        assert len(voices) == 2

    def test_incoming_outgoing_classification(self):
        voices = detect_voice_messages(_VOICE_XML, screen_width=720)
        incoming = [v for v in voices if v.is_incoming]
        outgoing = [v for v in voices if not v.is_incoming]
        assert len(incoming) == 1
        assert len(outgoing) == 1

    def test_duration_parsing(self):
        voices = detect_voice_messages(_VOICE_XML, screen_width=720)
        # Sorted by bottom_y ascending
        assert voices[0].duration_text == "0:05"
        assert voices[0].duration_sec == 5.0
        assert voices[1].duration_text == "0:12"
        assert voices[1].duration_sec == 12.0

    def test_play_button_coordinates(self):
        voices = detect_voice_messages(_VOICE_XML, screen_width=720)
        v0 = voices[0]  # incoming
        assert v0.play_cx == (28 + 96) // 2
        assert v0.play_cy == (1260 + 1368) // 2

    def test_sorted_by_bottom_y(self):
        voices = detect_voice_messages(_VOICE_XML, screen_width=720)
        assert voices[0].bottom_y <= voices[1].bottom_y

    def test_no_voice_in_text_only(self):
        voices = detect_voice_messages(_TEXT_ONLY_XML, screen_width=720)
        assert voices == []

    def test_empty_xml(self):
        voices = detect_voice_messages(_EMPTY_XML, screen_width=720)
        assert voices == []

    def test_invalid_xml(self):
        voices = detect_voice_messages(b"<not valid xml", screen_width=720)
        assert voices == []

    def test_wider_screen(self):
        """screen_width=1080 时 incoming/outgoing 判断仍正确。"""
        voices = detect_voice_messages(_VOICE_XML, screen_width=1080)
        incoming = [v for v in voices if v.is_incoming]
        outgoing = [v for v in voices if not v.is_incoming]
        assert len(incoming) == 1
        assert len(outgoing) == 1


# ── detect_last_incoming_voice ───────────────────────────────────────────────

class TestDetectLastIncomingVoice:
    def test_returns_incoming(self):
        v = detect_last_incoming_voice(_VOICE_XML, screen_width=720)
        assert v is not None
        assert v.is_incoming is True
        assert v.duration_sec == 5.0

    def test_none_when_no_voice(self):
        v = detect_last_incoming_voice(_TEXT_ONLY_XML, screen_width=720)
        assert v is None

    def test_none_when_only_outgoing(self):
        """只有自己发的语音时返回 None。"""
        xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
          <node class="android.widget.FrameLayout" package="com.whatsapp"
                bounds="[0,0][720,1600]">
            <node class="android.widget.RelativeLayout" bounds="[300,1400][706,1560]">
              <node resource-id="com.whatsapp:id/control_btn"
                    content-desc="Play voice message"
                    bounds="[520,1430][588,1538]" />
              <node resource-id="com.whatsapp:id/description"
                    text="0:03" bounds="[340,1510][400,1540]" />
            </node>
          </node>
        </hierarchy>
        """).encode("utf-8")
        v = detect_last_incoming_voice(xml, screen_width=720)
        assert v is None


# ── find_attach_button ───────────────────────────────────────────────────────

class TestFindAttachButton:
    def test_finds_attach_button(self):
        pos = find_attach_button(_VOICE_XML)
        assert pos is not None
        cx, cy = pos
        assert cx == (620 + 690) // 2
        assert cy == (1540 + 1590) // 2

    def test_none_when_missing(self):
        pos = find_attach_button(_TEXT_ONLY_XML)
        assert pos is None


# ── fallback: audio_seekbar 检测 ─────────────────────────────────────────────

class TestFallbackAudioSeekbar:
    def test_seekbar_fallback_when_no_control_btn_cd(self):
        """control_btn 无 content-desc 时 fallback 到 audio_seekbar。"""
        xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
          <node class="android.widget.FrameLayout" package="com.whatsapp"
                bounds="[0,0][720,1600]">
            <node class="android.widget.RelativeLayout" bounds="[14,1200][450,1380]">
              <node resource-id="com.whatsapp:id/control_btn"
                    content-desc=""
                    bounds="[28,1260][96,1368]" />
              <node resource-id="com.whatsapp:id/audio_seekbar"
                    content-desc="Voice message"
                    bounds="[100,1280][400,1340]" />
              <node resource-id="com.whatsapp:id/description"
                    text="1:30" bounds="[100,1340][160,1370]" />
            </node>
          </node>
        </hierarchy>
        """).encode("utf-8")
        voices = detect_voice_messages(xml, screen_width=720)
        assert len(voices) == 1
        assert voices[0].duration_sec == 90.0
        assert voices[0].is_incoming is True


# ── voice_grabber 单元测试 ───────────────────────────────────────────────────

class TestVoiceGrabber:
    def test_get_latest_returns_error_when_no_dir(self):
        from src.integrations.whatsapp_rpa.voice_grabber import get_latest_voice_note

        with patch("src.integrations.whatsapp_rpa.voice_grabber._shell", return_value=""):
            vn = get_latest_voice_note("FAKE_SERIAL")
            assert not vn.ok
            assert "empty_or_missing" in vn.error

    def test_get_latest_skips_already_processed(self):
        from src.integrations.whatsapp_rpa.voice_grabber import get_latest_voice_note

        import time
        now_ts = str(int(time.time()))

        def mock_shell(serial, cmd):
            if "ls -1" in cmd:
                return "202622\n"
            if "ls -lt" in cmd:
                return (
                    "total 8\n"
                    f"-rw-rw---- 1 u0_a246 media_rw 5242 2026-05-26 06:59 PTT-20260526-WA0000.opus\n"
                )
            if "stat" in cmd:
                return f"{now_ts} 5242"
            return ""

        with patch("src.integrations.whatsapp_rpa.voice_grabber._shell", side_effect=mock_shell):
            vn = get_latest_voice_note(
                "FAKE_SERIAL",
                already_processed={"PTT-20260526-WA0000.opus"},
            )
            assert not vn.ok
            assert "no_recent" in vn.error

    def test_get_latest_success_pulls_file(self, tmp_path):
        from src.integrations.whatsapp_rpa.voice_grabber import get_latest_voice_note

        import time
        now_ts = str(int(time.time()))
        target_file = tmp_path / "PTT-20260526-WA0001.opus"
        target_file.write_bytes(b"\x00" * 200)  # dummy opus data

        def mock_shell(serial, cmd):
            if "ls -1" in cmd:
                return "202622\n"
            if "ls -lt" in cmd:
                return (
                    "total 8\n"
                    "-rw-rw---- 1 u0_a246 media_rw 5242 2026-05-26 07:30 PTT-20260526-WA0001.opus\n"
                )
            if "stat" in cmd:
                return f"{now_ts} 5242"
            return ""

        mock_pull = MagicMock()
        mock_pull.returncode = 0
        mock_pull.stdout = ""
        mock_pull.stderr = ""

        def mock_run_adb(args, serial=None, timeout=None):
            if args[0] == "pull":
                # Simulate pull by copying the dummy file
                import shutil
                dst = args[2]
                shutil.copy2(str(target_file), dst)
                return mock_pull
            if args[0] == "shell":
                r = MagicMock()
                r.returncode = 0
                r.stdout = mock_shell(serial, args[1])
                return r
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("src.integrations.whatsapp_rpa.voice_grabber.adb.run_adb", side_effect=mock_run_adb):
            vn = get_latest_voice_note("FAKE_SERIAL", local_dir=str(tmp_path / "out"))
            assert vn.ok
            assert vn.filename == "PTT-20260526-WA0001.opus"
            assert vn.date_str == "20260526"
            assert os.path.exists(vn.local_path)

    def test_list_recent_empty(self):
        from src.integrations.whatsapp_rpa.voice_grabber import list_recent_voice_notes

        with patch("src.integrations.whatsapp_rpa.voice_grabber._shell", return_value=""):
            notes = list_recent_voice_notes("FAKE_SERIAL")
            assert notes == []


# ── voice_sender 单元测试 ────────────────────────────────────────────────────

class TestVoiceSender:
    def test_send_missing_file(self):
        from src.integrations.whatsapp_rpa.voice_sender import WhatsAppVoiceSender
        sender = WhatsAppVoiceSender("FAKE_SERIAL")
        rv = sender.send_audio_file("/nonexistent/file.mp3")
        assert not rv.ok
        assert "local_audio_missing" in rv.error

    def test_send_dry_run(self, tmp_path):
        from src.integrations.whatsapp_rpa.voice_sender import WhatsAppVoiceSender
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\xff\xfb\x90" * 100)
        sender = WhatsAppVoiceSender("FAKE_SERIAL")
        rv = sender.send_audio_file(str(f), dry_run=True, recipient_name="Alice")
        assert rv.ok
        assert rv.extra.get("dry_run") is True


class TestVoiceSenderXmlParsing:
    def test_find_recipient_in_share(self):
        from src.integrations.whatsapp_rpa.voice_sender import _find_recipient_in_share
        xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
          <node class="android.widget.FrameLayout" bounds="[0,0][720,1600]">
            <node text="Alice" bounds="[50,200][200,260]" />
            <node text="Bob" bounds="[210,200][370,260]" />
            <node text="Charlie" bounds="[380,200][540,260]" />
          </node>
        </hierarchy>
        """)
        found = _find_recipient_in_share(xml, "Bob")
        assert found is not None
        cx, cy, info = found
        assert cx == (210 + 370) // 2
        assert "Bob" in info

    def test_find_recipient_not_found(self):
        from src.integrations.whatsapp_rpa.voice_sender import _find_recipient_in_share
        xml = '<hierarchy><node text="Alice" bounds="[0,0][100,100]"/></hierarchy>'
        assert _find_recipient_in_share(xml, "Zara") is None

    def test_find_share_send_button(self):
        from src.integrations.whatsapp_rpa.voice_sender import _find_share_send_button
        xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
          <node class="android.widget.FrameLayout" bounds="[0,0][720,1600]">
            <node resource-id="com.whatsapp:id/send"
                  content-desc="Send" bounds="[600,1400][700,1500]" />
          </node>
        </hierarchy>
        """)
        found = _find_share_send_button(xml)
        assert found is not None
        cx, cy = found
        assert cx == (600 + 700) // 2
        assert cy == (1400 + 1500) // 2

    def test_find_send_button_none(self):
        from src.integrations.whatsapp_rpa.voice_sender import _find_share_send_button
        xml = '<hierarchy><node text="Hello" bounds="[0,0][100,100]"/></hierarchy>'
        assert _find_share_send_button(xml) is None

    def test_find_recipient_fuzzy_match(self):
        from src.integrations.whatsapp_rpa.voice_sender import _find_recipient_in_share
        xml = '<hierarchy><node text="阿龙🐉" bounds="[50,200][200,260]"/></hierarchy>'
        found = _find_recipient_in_share(xml, "阿龙")
        assert found is not None

    def test_mime_detection(self):
        from src.integrations.whatsapp_rpa.voice_sender import _mime_for
        assert _mime_for(Path("test.opus")) == "audio/ogg"
        assert _mime_for(Path("test.wav")) == "audio/wav"
        assert _mime_for(Path("test.mp3")) == "audio/mpeg"


# ── 通知语音检测 ─────────────────────────────────────────────────────────────

class TestVoiceNotificationDetection:
    def test_microphone_emoji(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("🎤 语音消息")
        assert _is_voice_notification("🎤 Voice message")
        assert _is_voice_notification("🎤 Audio (0:05)")

    def test_chinese_label(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("语音消息")

    def test_english_label(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("Voice message")
        assert _is_voice_notification("Audio message")

    def test_indonesian_label(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("Pesan suara")

    def test_voice_duration_format(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("Voice (0:12)")

    def test_normal_text_not_voice(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert not _is_voice_notification("Hello, how are you?")
        assert not _is_voice_notification("Can you send me a voice?")  # "voice" alone not a pattern
        assert not _is_voice_notification("")
        assert not _is_voice_notification("photo")

    def test_case_insensitive(self):
        from src.integrations.whatsapp_rpa.runner import _is_voice_notification
        assert _is_voice_notification("VOICE MESSAGE")
        assert _is_voice_notification("voice Message")


# ── 语音占位符 + 指标 ───────────────────────────────────────────────────

class TestVoicePlaceholder:
    def test_with_duration(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        from types import SimpleNamespace
        voice_msg = SimpleNamespace(duration_text="0:12")
        result = {}
        ph = WhatsAppRpaRunner._voice_placeholder(voice_msg, result)
        assert "0:12" in ph
        assert result["voice_transcribe_fallback"] is True
        assert result["voice_placeholder"] == ph

    def test_without_duration(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        result = {}
        ph = WhatsAppRpaRunner._voice_placeholder(None, result)
        assert "语音消息" in ph
        assert "转文字失败" in ph
        assert result["voice_transcribe_fallback"] is True

    def test_empty_duration(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        from types import SimpleNamespace
        voice_msg = SimpleNamespace(duration_text="")
        result = {}
        ph = WhatsAppRpaRunner._voice_placeholder(voice_msg, result)
        assert "语音消息" in ph


class TestVoiceMetrics:
    def test_initial_metrics(self):
        from unittest.mock import MagicMock
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        runner = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        runner._voice_metrics = {
            "stt_attempts": 0, "stt_ok": 0, "stt_fail": 0,
            "stt_fallback_used": 0, "stt_placeholder": 0,
            "stt_batch_multi": 0,
            "tts_attempts": 0, "tts_ok": 0, "tts_fail": 0,
            "tts_sent": 0, "tts_send_fail": 0,
        }
        m = runner.get_voice_metrics()
        assert m["stt_attempts"] == 0
        assert m["tts_attempts"] == 0
        # returned copy, not reference
        m["stt_attempts"] = 999
        assert runner.get_voice_metrics()["stt_attempts"] == 0

    def test_metrics_increment(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        runner = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        runner._voice_metrics = {
            "stt_attempts": 0, "stt_ok": 0, "stt_fail": 0,
            "stt_fallback_used": 0, "stt_placeholder": 0,
            "stt_batch_multi": 0,
            "tts_attempts": 0, "tts_ok": 0, "tts_fail": 0,
            "tts_sent": 0, "tts_send_fail": 0,
        }
        runner._voice_metrics["stt_attempts"] += 3
        runner._voice_metrics["stt_ok"] += 2
        runner._voice_metrics["stt_fail"] += 1
        m = runner.get_voice_metrics()
        assert m["stt_attempts"] == 3
        assert m["stt_ok"] == 2
        assert m["stt_fail"] == 1


class TestMultiVoiceDetection:
    """Test that detect_voice_messages returns multiple incoming voices."""
    def test_multiple_incoming(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_voice_messages
        xml = '<hierarchy rotation="0">' \
              '<node bounds="[0,400][360,500]" resource-id="com.whatsapp:id/control_btn"' \
              ' content-desc="播放 语音消息" />' \
              '<node bounds="[0,600][360,700]" resource-id="com.whatsapp:id/control_btn"' \
              ' content-desc="播放 语音消息" />' \
              '</hierarchy>'
        xml = xml.encode("utf-8")
        voices = detect_voice_messages(xml, screen_width=720)
        incoming = [v for v in voices if v.is_incoming]
        assert len(incoming) == 2
        assert incoming[0].bottom_y < incoming[1].bottom_y
