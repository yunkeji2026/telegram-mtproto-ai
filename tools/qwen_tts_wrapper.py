"""Synthesize speech with a Qwen cloned voice and save it locally."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict

import requests


DEFAULT_MODEL = "qwen3-tts-vc-2026-01-22"


def _load_local_secret(name: str) -> str:
    if os.getenv(name):
        return os.getenv(name, "")
    root = Path(__file__).resolve().parents[1]
    candidates = [
        Path(".env.local"),
        Path("config/secrets.local.json"),
        root / ".env.local",
        root / "config" / "secrets.local.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            value = data.get(name) or data.get(name.lower())
            if value:
                return str(value).strip()
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def _endpoint(region: str) -> str:
    if region.strip().lower() in ("cn", "china", "beijing", "mainland"):
        return "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    return "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


def _load_voice(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _fix_wav_header(path: "Path") -> None:
    """Fix WAV DataSize if set to a sentinel value (streaming WAV artifact)."""
    import struct
    try:
        raw = bytearray(path.read_bytes())
        if len(raw) < 44 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
            return
        if raw[36:40] != b"data":
            return
        actual_data_size = len(raw) - 44
        struct.pack_into("<I", raw, 4, len(raw) - 8)
        struct.pack_into("<I", raw, 40, actual_data_size)
        path.write_bytes(bytes(raw))
    except Exception:
        pass


def _audio_url(data: Dict[str, Any]) -> str:
    output = data.get("output") or {}
    audio = output.get("audio") or {}
    return str(audio.get("url") or "")


def synthesize(args: argparse.Namespace) -> Dict[str, Any]:
    profile = _load_voice(args.voice_profile)
    api_key = (
        args.api_key
        or str(profile.get("dashscope_api_key") or "")
        or _load_local_secret("DASHSCOPE_API_KEY")
    )
    if not api_key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    voice = args.voice or str(profile.get("voice") or "")
    if not voice:
        raise RuntimeError("missing qwen voice id; run qwen_voice_clone.py create first")
    model = args.model or str(profile.get("target_model") or DEFAULT_MODEL)
    payload: Dict[str, Any] = {
        "model": model,
        "input": {
            "text": args.text,
            "voice": voice,
            "language_type": args.language_type,
        },
    }
    if args.instructions:
        payload["input"]["instructions"] = args.instructions
    resp = requests.post(
        _endpoint(args.region),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=args.timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"qwen_tts_failed:{resp.status_code}:{resp.text[:500]}")
    result = resp.json()
    url = _audio_url(result)
    if not url:
        raise RuntimeError(f"qwen_tts_missing_audio_url:{json.dumps(result, ensure_ascii=False)[:500]}")
    audio = requests.get(url, timeout=args.timeout)
    if audio.status_code != 200:
        raise RuntimeError(f"qwen_tts_download_failed:{audio.status_code}:{audio.text[:300]}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio.content)
    _fix_wav_header(out)
    return {
        "ok": True,
        "out": str(out),
        "bytes": len(audio.content),
        "voice": voice,
        "model": model,
        "request_id": result.get("request_id", ""),
        "audio_url": url,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--voice", default="")
    parser.add_argument("--voice-profile", default="voice_samples/qwen_my_voice.json")
    parser.add_argument("--model", default="")
    parser.add_argument("--language-type", default="Japanese")
    parser.add_argument("--instructions", default="")
    parser.add_argument("--region", default=os.getenv("DASHSCOPE_REGION", "intl"))
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    result = synthesize(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
