# Messenger RPA Audio Capture Helper

Small Android helper app for non-root internal playback capture.

It uses Android `MediaProjection` + `AudioPlaybackCaptureConfiguration` to
record app playback while Messenger voice notes are playing.  The resulting WAV
is written under:

`/sdcard/Android/data/com.codex.mrpaudiobridge/files/Music/mrpa_capture.wav`

Build on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File tools/audio_capture_helper/build.ps1
```

Install:

```powershell
adb -s <serial> install -r tools/audio_capture_helper/build/MrpAudioBridge.apk
```

Start capture:

```powershell
adb -s <serial> shell am start -n com.codex.mrpaudiobridge/.MainActivity --ei duration_ms 5000
```

The first run shows Android's screen/audio capture consent dialog.  Approve it
once, then play the Messenger voice note.  Pull the WAV after the duration.
