#!/usr/bin/env python3
"""
物理机 + LINE + Root + 本仓库 AI 的一次性自检（不涉及读取聊天内容）。

用法（在项目根目录）:
  python tools/line_phone_smoke_test.py
  python tools/line_phone_smoke_test.py --serial XXXXX
  python tools/line_phone_smoke_test.py --skip-ai

若 adb devices 为空：请打开手机「开发者选项 → USB 调试」，用数据线连接，
在手机上点「允许 USB 调试」；仅充电线无法传 adb。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LINE_PKG = "jp.naver.line.android"


def _adb(args: list[str], *, serial: str | None) -> subprocess.CompletedProcess[str]:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _has_line(serial: str) -> bool:
    pr = _adb(["shell", f"pm path {LINE_PKG}"], serial=serial)
    return pr.returncode == 0 and LINE_PKG in (pr.stdout or "")


def _pick_serial(preferred: str | None) -> str | None:
    r = _adb(["devices", "-l"], serial=None)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    # Skip header
    candidates: list[tuple[str, str]] = []
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) < 2:
            continue
        sid, state = parts[0], parts[1]
        if state != "device":
            continue
        rest = " ".join(parts[2:])
        candidates.append((sid, rest))

    if preferred:
        for sid, _ in candidates:
            if sid == preferred:
                return sid
        return None

    # Prefer a phone that already has LINE (multiple wireless IPs / emulators)
    for sid, _ in candidates:
        if sid.startswith("127.0.0.1") or sid.startswith("emulator-"):
            continue
        if _has_line(sid):
            return sid

    # Prefer USB / wireless phone over BlueStacks
    for sid, rest in candidates:
        if sid.startswith("127.0.0.1") or sid.startswith("emulator-"):
            continue
        return sid
    # Fallback: first online device
    if candidates:
        return candidates[0][0]
    return None


def _shell(serial: str | None, script: str) -> subprocess.CompletedProcess[str]:
    return _adb(["shell", script], serial=serial)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="LINE phone + root + AI smoke test")
    ap.add_argument("--serial", help="adb 设备序列号（多设备时建议指定）")
    ap.add_argument("--skip-ai", action="store_true", help="跳过 AI 调用，仅测 adb/root/LINE")
    args = ap.parse_args()

    serial = _pick_serial(args.serial)
    if not serial:
        print("[FAIL] adb 未发现已授权且在线的设备（adb devices 为空或均为 offline/unauthorized）。")
        print("       请：1) 数据线连接  2) 手机开启 USB 调试  3) 点「允许此计算机调试」")
        return 2

    print(f"[OK] 使用设备: {serial}")

    p = _adb(["get-state"], serial=serial)
    print(f"     state: {(p.stdout or '').strip()}")

    for prop in ("ro.product.model", "ro.build.version.release"):
        p = _shell(serial, f"getprop {prop}")
        print(f"     {prop}: {(p.stdout or '').strip()}")

    # Root (Magisk 常见；部分设备 adb shell 无 su 路径但 su -c 仍可用)
    pr = _shell(serial, "su -c id")
    root_ok = pr.returncode == 0 and "uid=0" in (pr.stdout or "")
    print(
        f"     root (su -c id): {'OK uid=0' if root_ok else 'FAIL or no su'}"
    )
    if pr.stdout:
        print(f"       stdout: {pr.stdout.strip()[:200]}")
    if pr.stderr:
        print(f"       stderr: {pr.stderr.strip()[:200]}")

    pv = _shell(serial, f"dumpsys package {LINE_PKG} | grep versionName")
    vm = (pv.stdout or "").strip()
    if "versionName=" in vm:
        print(f"     LINE: {vm}")
    else:
        print(f"     LINE: 未安装或 dumpsys 失败: {vm or pv.stderr}")

    print()
    print("说明：本脚本不读取你的聊天记录，仅检查包版本与 root。")
    print("     个人号上的「智能对话」需 RPA 读屏 + 本仓库 AI；官方号用 line_webhook + Messaging API。")
    print()

    if args.skip_ai:
        print("[SKIP] --skip-ai：未调用大模型。")
        return 0

    async def run_ai() -> None:
        from src.utils.config_manager import ConfigManager
        from src.ai.ai_client import AIClient
        from src.utils.logger import setup_logger

        logger = setup_logger(log_file=None, console_output=True)
        cm = ConfigManager()
        if not await cm.load():
            print("[FAIL] config.yaml 加载失败（请检查 config/config.yaml）。")
            return
        ai = AIClient(cm)
        if not await ai.initialize():
            print("[FAIL] AIClient 初始化失败（检查 ai 段 API Key / provider）。")
            return
        reply = await ai.generate_reply(
            "请用中文一句话回复：收到连接测试，表示模型在线。",
            context={"reply_lang": "zh", "request_id": "line-phone-smoke"},
        )
        print("[OK] AI 回复（连通性测试）:")
        print(reply or "(空)")

    try:
        asyncio.run(run_ai())
    except Exception as e:
        print(f"[FAIL] AI 测试异常: {e}")
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
