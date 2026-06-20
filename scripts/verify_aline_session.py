"""临时：验证已登录的 A 线会话可连接、取自身信息、给收藏夹发测试消息。

用法:
    python scripts/verify_aline_session.py --session-name camille_test \
        --api-id 37821280 --api-hash 980df2bb9007317ecb35966f6a23d0dc
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--session-name", required=True)
    p.add_argument("--api-id", type=int, required=True)
    p.add_argument("--api-hash", required=True)
    p.add_argument("--sessions-dir", default="sessions")
    p.add_argument("--no-send", action="store_true", help="只连接取信息，不发测试消息")
    args = p.parse_args()

    from pyrogram import Client

    client = Client(
        args.session_name,
        api_id=args.api_id,
        api_hash=args.api_hash,
        workdir=str(Path(args.sessions_dir)),
    )
    await client.start()
    try:
        me = await client.get_me()
        print(f"[ok] get_me: {me.first_name} (@{me.username}) id={me.id} phone={me.phone_number}")
        if not args.no_send:
            msg = await client.send_message("me", "✅ A线连通性测试 / A-line connectivity OK")
            print(f"[ok] 已发送到收藏夹 message_id={msg.id}")
        # 读最近 3 条收藏夹消息验证收发
        count = 0
        async for m in client.get_chat_history("me", limit=3):
            count += 1
            preview = (m.text or m.caption or "<non-text>")
            print(f"      recent[{count}] id={m.id}: {str(preview)[:40]}")
    finally:
        await client.stop()
    print("[done] A 线会话验证完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
