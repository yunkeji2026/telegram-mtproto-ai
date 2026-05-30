from pathlib import Path
from subprocess import CompletedProcess
from io import BytesIO

from PIL import Image, ImageDraw
from src.integrations.messenger_rpa.voice_sender import MessengerVoiceSender


def _cp(args, returncode=0, stdout="", stderr=""):
    return CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_voice_sender_pushes_and_opens_share_intent(tmp_path, monkeypatch):
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"mp3")
    sender = MessengerVoiceSender("SERIAL")
    calls = []

    def fake_adb(args, timeout=10.0):
        calls.append(args)
        return _cp(args, stdout="OK")

    monkeypatch.setattr(sender, "_adb", fake_adb)
    rv = sender.send_audio_file(
        str(audio),
        recipient_tap_xy=(600, 640),
        send_tap_xy=(616, 634),
    )

    assert rv.ok is True
    assert rv.remote_path.startswith("/sdcard/Download/reply-")
    assert any(args[0] == "push" and args[1] == str(audio) for args in calls)
    assert any(
        args[:4] == ["shell", "am", "start", "-a"]
        and "android.intent.action.SEND" in args
        and "-p" in args
        and "com.facebook.orca" in args
        for args in calls
    )
    assert ["shell", "input", "tap", "600", "640"] in calls
    assert ["shell", "input", "tap", "616", "634"] in calls


def test_find_share_send_button_matches_recipient_row():
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node text="Other Person" bounds="[72,510][420,568]" />
  <node text="Send" bounds="[852,506][1012,576]" />
  <node text="Victor Zan" bounds="[72,624][430,682]" />
  <node text="Send" bounds="[852,620][1012,690]" />
</hierarchy>"""

    found = MessengerVoiceSender.find_share_send_button(xml, "Victor Zan")

    assert found == (932, 655, "Victor Zan->Send")


def test_voice_sender_auto_finds_share_send_button(tmp_path, monkeypatch):
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"mp3")
    sender = MessengerVoiceSender("SERIAL")
    calls = []
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node text="Victor Zan" bounds="[72,624][430,682]" />
  <node text="Send" bounds="[852,620][1012,690]" />
</hierarchy>"""

    def fake_adb(args, timeout=10.0):
        calls.append(args)
        return _cp(args, stdout="OK")

    monkeypatch.setattr(sender, "_adb", fake_adb)
    monkeypatch.setattr(sender, "_dump_xml", lambda: xml)

    rv = sender.send_audio_file(str(audio), recipient_name="Victor Zan")

    assert rv.ok is True
    assert rv.extra["share_send_button_match"] == "Victor Zan->Send"
    assert ["shell", "input", "tap", "932", "655"] in calls


def test_find_first_share_send_button_from_png_detects_blue_button():
    img = Image.new("RGB", (720, 1600), "white")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((544, 600, 688, 670), radius=18, fill=(10, 102, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")

    found = MessengerVoiceSender.find_first_share_send_button_from_png(buf.getvalue())

    assert found is not None
    assert found[:2] == (616, 635)
    assert found[2].startswith("blue_send_button:")


def test_voice_sender_falls_back_to_search_and_screenshot_send(tmp_path, monkeypatch):
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"mp3")
    sender = MessengerVoiceSender("SERIAL")
    calls = []
    searches = []
    img = Image.new("RGB", (720, 1600), "white")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((544, 600, 688, 670), radius=18, fill=(10, 102, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")

    def fake_adb(args, timeout=10.0):
        calls.append(args)
        return _cp(args, stdout="OK")

    monkeypatch.setattr(sender, "_adb", fake_adb)
    monkeypatch.setattr(sender, "_dump_xml", lambda: "")
    monkeypatch.setattr(sender, "_screenshot_png", lambda: buf.getvalue())
    monkeypatch.setattr(
        sender,
        "_search_share_recipient",
        lambda name, screen_wh: (searches.append((name, screen_wh)) or True),
    )

    rv = sender.send_audio_file(str(audio), recipient_name="Victor Zan")

    assert rv.ok is True
    assert searches == [("Victor Zan", (720, 1600))]
    assert rv.extra["share_send_button_match"].startswith("blue_send_button:")
    assert ["shell", "input", "tap", "616", "635"] in calls


def test_voice_sender_writes_audit_screenshots(tmp_path, monkeypatch):
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"mp3")
    sender = MessengerVoiceSender("SERIAL")
    calls = []
    png = b"\x89PNG\r\n\x1a\nfake"

    def fake_adb(args, timeout=10.0):
        calls.append(args)
        return _cp(args, stdout="OK")

    monkeypatch.setattr(sender, "_adb", fake_adb)
    monkeypatch.setattr(sender, "_screenshot_png", lambda: png)

    rv = sender.send_audio_file(
        str(audio),
        send_tap_xy=(616, 634),
        audit_dir=str(tmp_path / "audit"),
    )

    assert rv.ok is True
    pre = Path(rv.extra["pre_send_screenshot_path"])
    post = Path(rv.extra["post_send_screenshot_path"])
    assert pre.exists()
    assert post.exists()
    assert pre.read_bytes() == png
    assert post.read_bytes() == png


def test_voice_sender_dry_run_does_not_call_adb(tmp_path, monkeypatch):
    audio = tmp_path / "reply.wav"
    audio.write_bytes(b"wav")
    sender = MessengerVoiceSender("SERIAL")
    calls = []
    monkeypatch.setattr(sender, "_adb", lambda args, timeout=10.0: calls.append(args))

    rv = sender.send_audio_file(str(audio), dry_run=True)

    assert rv.ok is True
    assert rv.extra["dry_run"] is True
    assert rv.extra["mime"] == "audio/wav"
    assert calls == []


def test_voice_sender_reports_missing_file():
    sender = MessengerVoiceSender("SERIAL")
    rv = sender.send_audio_file("does-not-exist.mp3")

    assert rv.ok is False
    assert rv.error.startswith("local_audio_missing:")
