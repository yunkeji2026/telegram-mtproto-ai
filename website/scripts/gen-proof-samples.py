#!/usr/bin/env python3
"""Generate real multilingual voice samples from the local AvatarHub for the website proof gallery.
Calls /api/tts_only (Fish-Speech), saves WAV, then caller converts to mp3 via ffmpeg."""
import base64
import json
import os
import sys
import urllib.request

HUB = "http://127.0.0.1:9000"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["TEMP"], "samples")
os.makedirs(OUT, exist_ok=True)

# (out_name, profile, language, text) — labels on site stay neutral (中文/English/日本語)
JOBS = [
    ("voice-zh", "刘德华", "zh-cn",
     "您好，我是无界科技的 AI 数字人。全天候在线，能用多国语言和您实时沟通，帮您自动接待每一位客户。"),
    ("voice-en", "杰森斯坦森", "en",
     "Hi, I'm your AI digital human from Boundless. I work around the clock, chat in your customer's language, and help close every deal."),
    ("voice-ja", "皮特", "ja",
     "こんにちは。無界テクノロジーのAIデジタルヒューマンです。二十四時間、多言語であなたのお客様に対応します。"),
]


def gen(profile, language, text):
    body = json.dumps({"profile": profile, "language": language, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"{HUB}/api/tts_only", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


for name, profile, lang, text in JOBS:
    try:
        res = gen(profile, lang, text)
        if not res.get("ok") or not res.get("audio_base64"):
            print(f"{name}: FAIL {json.dumps(res, ensure_ascii=False)[:200]}")
            continue
        wav = base64.b64decode(res["audio_base64"])
        p = os.path.join(OUT, name + ".wav")
        with open(p, "wb") as f:
            f.write(wav)
        print(f"{name}: OK {len(wav)} bytes, elapsed_ms={res.get('elapsed_ms')} -> {p}")
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: ERROR {type(exc).__name__}: {exc}")
