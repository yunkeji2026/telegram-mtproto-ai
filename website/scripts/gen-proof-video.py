#!/usr/bin/env python3
"""Generate a real lip-synced talking-head video from the local AvatarHub for the site."""
import base64
import json
import os
import sys
import urllib.request

HUB = "http://127.0.0.1:9000"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["TEMP"], "samples")
os.makedirs(OUT, exist_ok=True)
PROFILE = sys.argv[2] if len(sys.argv) > 2 else "刘德华"
TEXT = "大家好，我是无界科技的高清数字人。会眨眼、会摆头，声音和口型完全同步——这一切都在你自己的机器上实时生成。"

body = json.dumps({
    "text": TEXT,
    "profile": PROFILE,
    "language": "zh-cn",
    "generate_lipsync": True,
    "incognito": True,
    "emotion": "happy",
}).encode("utf-8")

req = urllib.request.Request(f"{HUB}/avatar/speak", data=body,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.loads(r.read().decode("utf-8"))
except Exception as exc:  # noqa: BLE001
    print(f"ERROR {type(exc).__name__}: {exc}")
    sys.exit(1)

vid = res.get("lipsync_video_b64", "")
warn = res.get("warning", "")
print(f"elapsed_ms={res.get('elapsed_ms')} warning={warn!r} video_len={len(vid)}")
if vid:
    p = os.path.join(OUT, "digital-human.mp4")
    with open(p, "wb") as f:
        f.write(base64.b64decode(vid))
    print(f"VIDEO OK -> {p} ({os.path.getsize(p)} bytes)")
else:
    # fall back: save audio so we at least know speak worked
    aud = res.get("audio_base64", "")
    if aud:
        p = os.path.join(OUT, "dh_audio.wav")
        with open(p, "wb") as f:
            f.write(base64.b64decode(aud))
        print(f"NO VIDEO — audio saved -> {p}. lipsync engine likely off.")
    else:
        print("NO VIDEO and NO AUDIO. Full response:")
        print(json.dumps(res, ensure_ascii=False)[:400])
