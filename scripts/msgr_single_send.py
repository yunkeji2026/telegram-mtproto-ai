#!/usr/bin/env python3
"""单机向指定 Messenger 会话发一条固定文案（不经 LLM）。

用于第二台机未授权 / 只验单路 ADB 时烟测 ``send_to_chat_name`` 全链路。

用法::

    python scripts/msgr_single_send.py --account bg_phone_2 --peer "Victor Zan" --text "smoke"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _build_skill_manager(cm):
    from src.skills.skill_manager import SkillManager
    from src.ai.ai_client import AIClient

    ai = AIClient(cm)
    init = getattr(ai, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    sk = SkillManager(cm, ai)
    init = getattr(sk, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    return sk


def _serial_for(msgr: dict, account_id: str) -> str:
    for e in msgr.get("accounts") or []:
        if isinstance(e, dict) and str(e.get("id") or "").strip() == account_id:
            return str(e.get("adb_serial") or "").strip()
    return ""


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", required=True, help="messenger_rpa.accounts 里的 id")
    ap.add_argument("--peer", required=True, help="对方在本机 Chats 列表的显示名")
    ap.add_argument("--text", required=True, help="要发送的文本")
    ap.add_argument(
        "--no-assert",
        action="store_true",
        help="关闭发送后 U1/U4 assert（与互发脚本一致）",
    )
    args = ap.parse_args()

    from src.utils.config_manager import ConfigManager
    from src.integrations.messenger_rpa.service import MessengerRpaService

    cm = ConfigManager()
    if not await cm.load():
        print("config 加载失败", file=sys.stderr)
        return 2
    msgr = cm.get_messenger_rpa_config() or {}
    ser = _serial_for(msgr, args.account)
    if not ser:
        print(f"未找到 account_id={args.account!r} 的 adb_serial", file=sys.stderr)
        return 2

    from src.integrations.messenger_rpa import thread_actions as ta
    import subprocess

    st = subprocess.run(
        ["adb", "-s", ser, "get-state"],
        capture_output=True,
        text=True,
        timeout=12,
    )
    state = (st.stdout or "").strip()
    if state != "device":
        err = (st.stderr or "").strip()
        print(
            f"ADB 未就绪 serial={ser!r} state={state!r} stderr={err!r} —— "
            f"请插线、点手机「允许 USB 调试」后重试。",
            file=sys.stderr,
        )
        return 3

    skill = await _build_skill_manager(cm)
    svc = MessengerRpaService(
        config_manager=cm,
        skill_manager=skill,
        messenger_rpa_cfg=msgr,
    )
    print(f">>> account={args.account} serial={ser!r} -> {args.peer!r} …")
    r = await svc.send_to_chat_name_for_account(
        args.account, chat_name=args.peer, reply_text=args.text,
    )
    print(f"ok={r.get('ok')} step={r.get('step')} err={r.get('error')!r}")
    if not r.get("ok"):
        print(f"full: {r}")
        return 1
    if not args.no_assert:
        try:
            vt = ta.verify_thread_title(ser, args.peer)
            sent = await ta.assert_sent(ser, args.text, wait_sec=0.5)
            print(
                f"assert title={vt.ok} actual={vt.actual!r} | sent={sent.ok} {sent.reason!r}",
            )
        except Exception as ex:
            print(f"assert 异常: {ex}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
