#!/usr/bin/env python3
"""
一次性：用 Pyrogram 生成 sessions/{session_name}.session
验证码：在项目根目录创建 code.txt，内容仅为数字（5 分钟内）。
二步验证：环境变量 TG_2FA_PASSWORD 或根目录 2fa_password.txt（一行）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    FloodWait,
    Unauthorized,
)


async def wait_code_file(path: str, timeout: int) -> str:
    for _ in range(timeout):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    code = f.read().strip()
                if code.isdigit() and len(code) >= 5:
                    os.remove(path)
                    return code
            except OSError:
                pass
        await asyncio.sleep(1)
    raise SystemExit(f"超时 {timeout}s：未在项目根目录找到有效 code.txt（仅数字）")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Create Pyrogram session file")
    ap.add_argument("--api-id", type=int, required=True)
    ap.add_argument("--api-hash", required=True)
    ap.add_argument("--phone", required=True)
    ap.add_argument("--session-name", required=True)
    ap.add_argument("--wait-seconds", type=int, default=600)
    args = ap.parse_args()

    os.makedirs("sessions", exist_ok=True)
    session_path = os.path.join("sessions", f"{args.session_name}.session")
    if os.path.exists(session_path):
        bak = session_path + ".bak"
        os.replace(session_path, bak)
        print(f"[info] 已备份旧会话: {bak}")

    client = Client(
        name=args.session_name,
        api_id=args.api_id,
        api_hash=args.api_hash,
        phone_number=args.phone,
        workdir="sessions",
    )

    # Pyrogram: connect() 返回 True 表示会话已授权（storage 中有 user_id）
    is_authorized = await client.connect()
    if is_authorized:
        me = await client.get_me()
        print(f"[ok] 会话已有效: {me.first_name} (@{me.username}) id={me.id}")
        await client.disconnect()
        return

    print(f"[info] 正在向 {args.phone} 发送 Telegram 验证码…")
    sent = await client.send_code(args.phone)
    print(
        f"[info] 请在 {args.wait_seconds} 秒内，在项目根目录创建文件 code.txt，"
        f"内容仅为验证码数字（不要空格或换行）。\n"
        f"      根目录: {ROOT}"
    )

    code = await wait_code_file("code.txt", args.wait_seconds)
    print("[info] 已读取验证码，正在登录…")

    try:
        await client.sign_in(
            phone_number=args.phone,
            phone_code_hash=sent.phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        print("[info] 需要二步验证密码…")
        pw = os.environ.get("TG_2FA_PASSWORD", "").strip()
        if not pw and os.path.exists("2fa_password.txt"):
            with open("2fa_password.txt", "r", encoding="utf-8") as f:
                pw = f.read().strip()
        if not pw:
            await client.disconnect()
            raise SystemExit("缺少二步验证：请设置 TG_2FA_PASSWORD 或创建 2fa_password.txt")
        await client.check_password(pw)
        try:
            os.remove("2fa_password.txt")
        except OSError:
            pass
    except PhoneCodeInvalid as e:
        await client.disconnect()
        raise SystemExit(f"验证码无效: {e}") from e
    except PhoneCodeExpired as e:
        await client.disconnect()
        raise SystemExit(f"验证码过期: {e}") from e
    except FloodWait as e:
        await client.disconnect()
        raise SystemExit(f"FloodWait: 请等待 {e.value} 秒后再试") from e
    except Unauthorized as e:
        await client.disconnect()
        raise SystemExit(f"未授权: {e}") from e

    me = await client.get_me()
    print(f"[ok] 登录成功: {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()
    print(f"[ok] 会话文件: sessions/{args.session_name}.session")


if __name__ == "__main__":
    asyncio.run(main())
