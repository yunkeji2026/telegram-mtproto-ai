"""Create/list Qwen cloned voices using Alibaba Cloud Model Studio (CLI).

Thin wrapper over ``src.ai.voice_enroll`` (the importable core). Requires
DASHSCOPE_API_KEY in the environment / .env.local / config/secrets.local.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 允许 `python tools/qwen_voice_clone.py` 直接运行时导入 src 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.voice_enroll import (  # noqa: E402
    DEFAULT_TARGET_MODEL,
    delete_cloned_voice,
    enroll_voice,
    list_cloned_voices,
)

DEFAULT_MODEL = DEFAULT_TARGET_MODEL  # 向后兼容旧引用


def create_voice(args: argparse.Namespace) -> dict:
    res = enroll_voice(
        audio_path=args.audio,
        preferred_name=args.preferred_name,
        api_key=args.api_key,
        region=args.region,
        target_model=args.target_model,
        timeout=args.timeout,
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "provider": "qwen",
                    "voice": res["voice"],
                    "target_model": args.target_model,
                    "preferred_name": args.preferred_name,
                    "reference_audio_path": str(Path(args.audio).resolve()),
                    "region": args.region,
                    "request_id": res.get("request_id", ""),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return res.get("raw", res)


def list_voices(args: argparse.Namespace) -> dict:
    return list_cloned_voices(
        api_key=args.api_key,
        region=args.region,
        page_size=args.page_size,
        page_index=args.page_index,
        timeout=args.timeout,
    )


def delete_voice(args: argparse.Namespace) -> dict:
    return delete_cloned_voice(
        voice=args.voice,
        api_key=args.api_key,
        region=args.region,
        timeout=args.timeout,
    )


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

    d = sub.add_parser("delete")
    d.add_argument("--voice", required=True)

    args = parser.parse_args()
    if args.cmd == "create":
        result = create_voice(args)
    elif args.cmd == "delete":
        result = delete_voice(args)
    else:
        result = list_voices(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
