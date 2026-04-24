#!/usr/bin/env python3
"""
个人 LINE RPA 轮询：按间隔重复 run_once，连续失败熔断。

  python tools/line_rpa_loop.py --interval 20 --max-failures 5 --force

需 line_rpa.enabled 或 --force；Ctrl+C 结束。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _main_async() -> int:
    ap = argparse.ArgumentParser(description="LINE RPA polling loop")
    ap.add_argument("--interval", type=float, default=15.0, help="秒，每轮间隔")
    ap.add_argument(
        "--max-failures",
        type=int,
        default=8,
        help="连续失败（ok=false）达到此次数则退出",
    )
    ap.add_argument("--force", action="store_true", help="忽略 line_rpa.enabled=false")
    ap.add_argument("--dry-run", action="store_true", help="同 run_once --dry-run")
    ap.add_argument("--peer-text", default="", help="固定对方话术（调试用）")
    ap.add_argument("--config", default="", help="config.yaml 路径")
    ap.add_argument(
        "--log-jsonl",
        default="",
        help="每轮追加一行 JSON（含时间戳与 result）",
    )
    args = ap.parse_args()

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
            "line_rpa.enabled is false. Use --force or set line_rpa.enabled: true.",
            file=sys.stderr,
        )
        return 3

    ai = AIClient(cm)
    if not await ai.initialize():
        return 4

    sm = SkillManager(cm, ai)
    if not await sm.initialize():
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
    }
    merged = {**defaults, **lr}

    runner = LineRpaRunner(
        config_manager=cm,
        skill_manager=sm,
        line_rpa_cfg=merged,
    )

    stop = asyncio.Event()

    def _sig(*_: object) -> None:
        stop.set()

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    ov = (args.peer_text or "").strip() or None
    fails = 0
    n = 0
    while not stop.is_set():
        n += 1
        logging.info("line_rpa loop iteration %s", n)
        result = None
        loop_err: str | None = None
        try:
            result = await runner.run_once(
                dry_run=args.dry_run,
                force_reply=False,
                peer_text_override=ov,
            )
        except Exception as e:
            loop_err = str(e)
            logging.exception("run_once: %s", e)
            fails += 1
        else:
            ok = bool(result.get("ok"))
            step = result.get("step", "")
            logging.info("result ok=%s step=%s", ok, step)
            if ok or step in (
                "duplicate_peer_skipped",
                "no_peer_text",
                "empty_reply",
                "screen_unchanged_skipped",
            ):
                fails = 0
            else:
                fails += 1
            if fails >= args.max_failures:
                logging.error(
                    "连续失败 %s 次，退出（可调整 --max-failures）",
                    fails,
                )
                return 7
        if args.log_jsonl:
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "iteration": n,
                "result": result,
                "error": loop_err,
            }
            Path(args.log_jsonl).parent.mkdir(parents=True, exist_ok=True)
            with open(args.log_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        try:
            await asyncio.wait_for(stop.wait(), timeout=args.interval)
        except asyncio.TimeoutError:
            pass

    logging.info("line_rpa loop 已停止")
    return 0


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
