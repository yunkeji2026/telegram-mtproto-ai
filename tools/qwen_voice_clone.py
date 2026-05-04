"""Create/list Qwen cloned voices using Alibaba Cloud Model Studio.

Requires DASHSCOPE_API_KEY in the environment. This script uses the REST API
directly so the project does not need the DashScope SDK just to enroll a voice.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict

import requests


DEFAULT_MODEL = "qwen3-tts-vc-2026-01-22"
ENROLLMENT_MODEL = "qwen-voice-enrollment"


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
        return "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
    return "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/customization"


def _mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    if path.suffix.lower() == ".wav":
        return "audio/wav"
    if path.suffix.lower() == ".m4a":
        return "audio/mp4"
    return "audio/mpeg"


def _post(payload: Dict[str, Any], *, api_key: str, region: str, timeout: float) -> Dict[str, Any]:
    resp = requests.post(
        _endpoint(region),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"qwen_voice_api_failed:{resp.status_code}:{resp.text[:500]}")
    return resp.json()


def create_voice(args: argparse.Namespace) -> Dict[str, Any]:
    api_key = args.api_key or _load_local_secret("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    audio = Path(args.audio).resolve()
    if not audio.is_file():
        raise FileNotFoundError(str(audio))
    data_uri = f"data:{_mime(audio)};base64,{base64.b64encode(audio.read_bytes()).decode()}"
    payload = {
        "model": ENROLLMENT_MODEL,
        "input": {
            "action": "create",
            "target_model": args.target_model,
            "preferred_name": args.preferred_name,
            "audio": {"data": data_uri},
        },
    }
    result = _post(payload, api_key=api_key, region=args.region, timeout=args.timeout)
    voice = str(((result.get("output") or {}).get("voice")) or "")
    if not voice:
        raise RuntimeError(f"missing voice in response:{json.dumps(result, ensure_ascii=False)[:500]}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "provider": "qwen",
                    "voice": voice,
                    "target_model": args.target_model,
                    "preferred_name": args.preferred_name,
                    "reference_audio_path": str(audio),
                    "region": args.region,
                    "request_id": result.get("request_id", ""),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


def list_voices(args: argparse.Namespace) -> Dict[str, Any]:
    api_key = args.api_key or _load_local_secret("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    payload = {
        "model": ENROLLMENT_MODEL,
        "input": {
            "action": "list",
            "page_size": int(args.page_size),
            "page_index": int(args.page_index),
        },
    }
    return _post(payload, api_key=api_key, region=args.region, timeout=args.timeout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.getenv("DASHSCOPE_REGION", "intl"))
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--audio", default="voice_samples/my_voice.wav")
    c.add_argument("--target-model", default=DEFAULT_MODEL)
    c.add_argument("--preferred-name", default="my_voice")
    c.add_argument("--out", default="voice_samples/qwen_my_voice.json")

    l = sub.add_parser("list")
    l.add_argument("--page-size", type=int, default=10)
    l.add_argument("--page-index", type=int, default=0)

    args = parser.parse_args()
    result = create_voice(args) if args.cmd == "create" else list_voices(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
