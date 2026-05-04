from pathlib import Path
from subprocess import CompletedProcess
import wave

import pytest
from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber


def _cp(args, returncode=0, stdout="", stderr=""):
    return CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_capture_with_helper_app_installs_starts_and_pulls(tmp_path, monkeypatch):
    apk = tmp_path / "MrpAudioBridge.apk"
    apk.write_bytes(b"apk")
    out_dir = tmp_path / "out"
    grabber = VoiceGrabber("SERIAL", out_dir=str(out_dir))
    calls = []
    remote = "/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture.wav"

    def fake_adb(args, timeout=10.0):
        calls.append(args)
        if args[:3] == ["shell", "pm", "path"]:
            return _cp(args, returncode=1)
        if args[:2] == ["install", "-r"]:
            return _cp(args, stdout="Success")
        if args[:3] == ["shell", "ls", "-l"]:
            return _cp(args, stdout=f"-rw-rw---- 1 u u 32044 2026-05-01 {remote}\n")
        if args[0] == "pull":
            Path(args[2]).write_bytes(b"RIFF" + (b"\0" * 32040))
            return _cp(args, stdout="pulled")
        return _cp(args)

    monkeypatch.setattr(grabber, "_adb", fake_adb)
    rv = grabber.capture_with_helper_app(
        duration_sec=1,
        apk_path=str(apk),
        wait_for_user_consent_sec=1,
    )

    assert rv.ok is True
    assert rv.method == "helper_app"
    assert Path(rv.local_path).exists()
    assert ["install", "-r", str(apk)] in calls
    assert [
        "shell", "am", "start",
        "-n", "com.codex.mrpaudiobridge/.MainActivity",
        "--ei", "duration_ms", "1000",
    ] in calls


def test_capture_with_helper_app_empty_audio_reports_helper_error(tmp_path, monkeypatch):
    apk = tmp_path / "MrpAudioBridge.apk"
    apk.write_bytes(b"apk")
    grabber = VoiceGrabber("SERIAL", out_dir=str(tmp_path / "out"))

    def fake_adb(args, timeout=10.0):
        if args[:3] == ["shell", "pm", "path"]:
            return _cp(args, stdout="package:/data/app/base.apk\n")
        if args[:3] == ["shell", "ls", "-l"]:
            return _cp(args, stdout="-rw-rw---- 1 u u 44 2026-05-01 mrpa_capture.wav\n")
        if args[0] == "pull":
            Path(args[2]).write_bytes(b"\0" * 44)
            return _cp(args, stdout="pulled")
        if args[:3] == ["shell", "cat", "/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture_error.txt"]:
            return _cp(args, stdout="AudioRecord init failed")
        return _cp(args)

    monkeypatch.setattr(grabber, "_adb", fake_adb)
    rv = grabber.capture_with_helper_app(
        duration_sec=1,
        apk_path=str(apk),
        wait_for_user_consent_sec=1,
    )

    assert rv.ok is False
    assert rv.local_path
    assert "helper_empty_audio:AudioRecord init failed" in rv.error


def test_wav_signal_stats_detects_silence_and_signal(tmp_path):
    silent = tmp_path / "silent.wav"
    signal = tmp_path / "signal.wav"
    for path, sample in ((silent, 0), (signal, 1200)):
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            frame = int(sample).to_bytes(2, "little", signed=True)
            wf.writeframes(frame * 1600)

    assert VoiceGrabber._wav_signal_stats(silent)["max_abs"] == 0
    assert VoiceGrabber._wav_signal_stats(signal)["max_abs"] == 1200
    assert VoiceGrabber._wav_signal_stats(signal)["rms"] > 1000


def test_detect_peer_voice_tap_from_image(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (720, 1600), "white")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((104, 304, 568, 444), radius=28, fill=(242, 243, 245))
    draw.polygon([(145, 350), (145, 400), (188, 375)], fill=(0, 0, 0))
    for x in range(220, 450, 24):
        draw.rounded_rectangle((x, 338, x + 8, 412), radius=4, fill=(0, 0, 0))
    path = tmp_path / "thread.png"
    img.save(path)

    x, y, reason = VoiceGrabber._detect_peer_voice_tap_from_image(
        path, screen_wh=(720, 1600)
    )

    assert 145 <= x <= 190
    assert 350 <= y <= 400
    assert reason.startswith("pixel_component")


def test_find_peer_voice_tap_scrolls_until_visible(tmp_path, monkeypatch):
    grabber = VoiceGrabber("SERIAL", out_dir=str(tmp_path))
    calls = {"screen": 0, "adb": []}

    def fake_cap(path):
        calls["screen"] += 1
        path.write_bytes(b"png")
        return True

    def fake_detect(path, *, screen_wh):
        if calls["screen"] == 1:
            return 158, 624, "fallback_no_component"
        return 161, 907, "pixel_component:142,886,180,928,n=307"

    def fake_adb(args, timeout=10.0):
        calls["adb"].append(args)
        return _cp(args)

    monkeypatch.setattr(grabber, "_screencap_to_file", fake_cap)
    monkeypatch.setattr(VoiceGrabber, "_detect_peer_voice_tap_from_image", staticmethod(fake_detect))
    monkeypatch.setattr(grabber, "_adb", fake_adb)

    found, info = grabber._find_peer_voice_tap(
        screen_wh=(720, 1600),
        scroll_attempts=2,
    )

    assert found == (161, 907)
    assert len(info["attempts"]) == 2
    assert any(args[:3] == ["shell", "input", "swipe"] for args in calls["adb"])
