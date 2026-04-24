#!/usr/bin/env python3
"""
个人 LINE RPA 单次执行：读当前聊天界面 → Skill/AI → 回发。

  python tools/line_rpa_run_once.py
  python tools/line_rpa_run_once.py --dry-run
  python tools/line_rpa_run_once.py --force-reply
  python tools/line_rpa_run_once.py --force   # 忽略 line_rpa.enabled=false

需在 config/config.yaml 中配置 ai（与主程序相同）。可选 line_rpa 段；未配置时使用内置默认。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _main_async() -> int:
    parser = argparse.ArgumentParser(description="LINE personal RPA run once")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析 UI 与生成回复，不点击发送",
    )
    parser.add_argument(
        "--force-reply",
        action="store_true",
        help="即使与上次对方话术相同也再答一轮",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 line_rpa.enabled=false",
    )
    parser.add_argument(
        "--peer-text",
        type=str,
        default="",
        help="跳过从界面解析对方话术，直接作为用户输入（uiautomator 不可用时的联调）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="config.yaml 路径（默认 config/config.yaml）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s",
    )

    from src.utils.config_manager import ConfigManager
    from src.ai.ai_client import AIClient
    from src.skills.skill_manager import SkillManager
    from src.integrations.line_rpa.runner import LineRpaRunner

    cm = ConfigManager(args.config if args.config else None)
    if not await cm.load():
        print("ERROR: config load failed", file=sys.stderr)
        return 2

    lr = cm.get_line_rpa_config()
    if not lr.get("enabled") and not args.force:
        print(
            "line_rpa.enabled is false. Add line_rpa.enabled: true to config.yaml "
            "or pass --force.",
            file=sys.stderr,
        )
        return 3

    ai = AIClient(cm)
    if not await ai.initialize():
        print("ERROR: AIClient init failed", file=sys.stderr)
        return 4

    sm = SkillManager(cm, ai)
    if not await sm.initialize():
        print("ERROR: SkillManager init failed", file=sys.stderr)
        return 5

    defaults = {
        "enabled": True,
        "line_package": "jp.naver.line.android",
        "splash_activity": "jp.naver.line.android/.activity.SplashActivity",
        "dump_remote_path": "/sdcard/line_rpa_dump.xml",
        "peer_left_ratio": 0.42,
        "chat_key": "line_rpa:default",
        "default_reply_lang": "zh",
        "use_adb_keyboard": True,
        "adb_keyboard_ime": "com.android.adbkeyboard/.AdbIME",
        "adb_keyboard_prefer_b64": True,
        "adb_keyboard_package": "com.android.adbkeyboard",
        "redump_before_send": True,
        "read_fallback": "none",
        "screenshot_ocr": {
            "enabled": False,
            "crop_bottom_ratio": 0.42,
            "peer_left_strip_ratio": 0.58,
            "tesseract_lang": "chi_sim+eng",
            "skip_if_unchanged": True,
        },
        "vision_read_fallback": {"enabled": False},
        "use_backend_persona": True,
    }
    merged = {**defaults, **lr}

    runner = LineRpaRunner(
        config_manager=cm,
        skill_manager=sm,
        line_rpa_cfg=merged,
    )

    ov = (args.peer_text or "").strip() or None
    result = await runner.run_once(
        dry_run=args.dry_run,
        force_reply=args.force_reply,
        peer_text_override=ov,
    )
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 6


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
