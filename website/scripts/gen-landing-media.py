#!/usr/bin/env python3
"""Generate landing-page proof media from the local AvatarHub:
1) interpreting pair: same cloned voice, zh source + en interpretation
2) EN digital-human talking head video."""
import base64
import json
import os
import sys
import urllib.request

HUB = "http://127.0.0.1:9000"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["TEMP"], "landing")
os.makedirs(OUT, exist_ok=True)

AUDIO_JOBS = [
    # 同传证据：同一音色，先中文原声，再英文"同传"输出（LingoX 卖点=保留你自己的声音）
    ("interp-src-zh", "刘德华", "zh-cn",
     "各位好，先给大家介绍一下我们这季度的重点：海外市场的增长超出预期，我们决定加大在东南亚的投入。"),
    ("interp-out-en", "刘德华", "en",
     "Hello everyone. A quick highlight for this quarter: overseas growth beat expectations, and we've decided to double down on Southeast Asia."),
]

VIDEO_JOB = ("digital-human-en", "杰森斯坦森", "en",
             "Hi, I'm an AI digital human by Boundless. Cloned face, cloned voice, perfect lip sync — all generated in real time on your own hardware.")


def tts(profile, language, text):
    body = json.dumps({"profile": profile, "language": language, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"{HUB}/api/tts_only", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def speak_video(profile, language, text):
    body = json.dumps({"text": text, "profile": profile, "language": language,
                       "generate_lipsync": True, "incognito": True}).encode("utf-8")
    req = urllib.request.Request(f"{HUB}/avatar/speak", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


for name, profile, lang, text in AUDIO_JOBS:
    try:
        res = tts(profile, lang, text)
        if res.get("ok") and res.get("audio_base64"):
            p = os.path.join(OUT, name + ".wav")
            with open(p, "wb") as f:
                f.write(base64.b64decode(res["audio_base64"]))
            print(f"{name}: OK elapsed={res.get('elapsed_ms')}ms -> {p}")
        else:
            print(f"{name}: FAIL {json.dumps(res, ensure_ascii=False)[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: ERROR {type(exc).__name__}: {exc}")

name, profile, lang, text = VIDEO_JOB
try:
    res = speak_video(profile, lang, text)
    vid = res.get("lipsync_video_b64", "")
    if vid:
        p = os.path.join(OUT, name + ".mp4")
        with open(p, "wb") as f:
            f.write(base64.b64decode(vid))
        print(f"{name}: VIDEO OK elapsed={res.get('elapsed_ms')}ms -> {p} ({os.path.getsize(p)}B)")
    else:
        print(f"{name}: NO VIDEO warning={res.get('warning', '')!r}")
except Exception as exc:  # noqa: BLE001
    print(f"{name}: ERROR {type(exc).__name__}: {exc}")
