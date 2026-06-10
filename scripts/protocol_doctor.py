#!/usr/bin/env python3
"""协议栈联调自检 CLI（M6③）。

一键检查 Telegram(pyrogram) / WhatsApp(Baileys) 协议多开「能不能跑起来」：
配置开关、依赖、Node 服务可达性、编排器、收件箱入站 sink。

用法：
    python scripts/protocol_doctor.py                       # 只读自检（含 WA 服务可达性探测）
    python scripts/protocol_doctor.py --json                # 输出 JSON
    python scripts/protocol_doctor.py --smoke \\
        --server-url http://127.0.0.1:8000 --token <admin>  # 额外跑入站桥烟囱测试

烟囱测试：向运行中的主进程 POST 一条合成消息到 /api/internal/protocol/ingest，
再读 /api/unified-inbox/thread 验证它确实进了统一收件箱（无需真账号即可验收发链路）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，无法编码 ✅/❌ 等标记 → 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_config() -> dict:
    cfg_path = ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as ex:  # noqa: BLE001
        print(f"[warn] 读取 {cfg_path} 失败: {ex}")
        return {}


async def _smoke_ingest(server_url: str, token: str) -> bool:
    """向运行中的服务 push 一条合成消息并验证它进了收件箱。"""
    import httpx

    server_url = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    acct = "doctor_smoke"
    chat_key = f"smoke_{int(time.time())}"
    text = f"protocol_doctor smoke {chat_key}"
    payload = {
        "platform": "whatsapp", "account_id": acct, "chat_key": chat_key,
        "name": "Doctor Smoke", "text": text, "direction": "in",
        "ts": time.time(), "msg_id": chat_key,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{server_url}/api/internal/protocol/ingest",
                              json=payload, headers=headers)
        print(f"[smoke] ingest -> {r.status_code} {r.text[:160]}")
        if r.status_code != 200 or not r.json().get("ok"):
            return False
        # 读 thread 验证（protocol 账号固定读 store）
        r2 = await client.get(
            f"{server_url}/api/unified-inbox/thread",
            params={"platform": "whatsapp", "account_id": acct,
                    "chat_key": chat_key, "limit": 10},
            headers=headers,
        )
        ok = r2.status_code == 200 and any(
            text in (m.get("text") or "") for m in (r2.json().get("messages") or []))
        print(f"[smoke] thread -> {r2.status_code} matched={ok}")
        return ok


async def main() -> int:
    ap = argparse.ArgumentParser(description="协议栈联调自检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--smoke", action="store_true", help="额外跑入站桥烟囱测试")
    ap.add_argument("--server-url", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default=os.environ.get("ADMIN_TOKEN", ""))
    args = ap.parse_args()

    from src.integrations.protocol_diagnostics import format_report, readiness

    config = _load_config()
    report = await readiness(config)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))

    rc = 0 if report.get("overall_ready") else 1

    if args.smoke:
        print("\n=== 入站桥烟囱测试 ===")
        try:
            ok = await _smoke_ingest(args.server_url, args.token)
        except Exception as ex:  # noqa: BLE001
            print(f"[smoke] 失败: {ex}")
            ok = False
        print(f"[smoke] 结果: {'✅ 通过' if ok else '❌ 失败'}")
        rc = rc or (0 if ok else 2)

    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
