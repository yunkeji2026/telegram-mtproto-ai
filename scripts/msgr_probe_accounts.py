"""扫一遍所有配置账号当前 Messenger Chats 页，帮人眼确定 ``peer name``。

用法::

    python scripts/msgr_probe_accounts.py
    python scripts/msgr_probe_accounts.py --open-first  # 顺便打开每台机 top 1 的会话，读顶栏名

目的：Inbox 列表行在 Litho 里读不到 peer 显示名（只有 ``X.2Wn@hash,
SimpleTextThreadSnippet(text=...)``），所以没法直接从 Chats 列表自动
取出 ``对方名字``。但可以：

  1. 把**当前可见**的所有会话行（预览文本 + bbox Y）打出来 → 人眼核对
     哪一行是要发送的对象；
  2. 可选 ``--open-first``：点进第一行 → dump thread → 读顶栏的
     ``xxx, 对话详情`` → 打出 **真实 peer 名** → back 回 Chats。

这个脚本**只读**（不会发消息），可以安全跑。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--open-first",
        action="store_true",
        help=(
            "除了扫 Chats 列表，还点进每台机的第 1 个 non-self 会话、dump 顶栏、"
            "打出真实 peer 名"
        ),
    )
    args = ap.parse_args()

    from src.utils.config_manager import ConfigManager
    from src.integrations.line_rpa import adb_helpers as adb
    from src.integrations.messenger_rpa import thread_actions as ta
    from src.integrations.messenger_rpa import ui_scraper as uis

    cm = ConfigManager()
    if not await cm.load():
        print("config 加载失败", file=sys.stderr)
        return 2
    msgr = cm.get_messenger_rpa_config() or {}
    accts: List[Dict[str, Any]] = msgr.get("accounts") or []
    if not accts:
        print("messenger_rpa.accounts 为空")
        return 2

    print(f"=== 扫 {len(accts)} 个账号 ===\n")

    for e in accts:
        aid = str(e.get("id") or "").strip()
        serial = str(e.get("adb_serial") or "").strip()
        label = str(e.get("label") or aid).strip()
        print(f"── {aid}  ({label})  serial={serial!r} ──")
        if not serial:
            print("  skip: 无 adb_serial")
            continue

        # 1) 把 Messenger 拉到前台
        r = adb.run_adb(
            [
                "shell",
                "am start -W -n "
                "com.facebook.orca/com.facebook.orca.auth.StartScreenActivity",
            ],
            serial=serial, timeout=20.0,
        )
        if r.returncode != 0:
            print(f"  adb am start rc={r.returncode}; 跳过")
            continue
        await asyncio.sleep(2.0)

        # 2) dump Chats 页
        xml = ta.dump_view_tree(serial)
        if xml is None:
            print("  dump 失败；跳过")
            continue
        rows = uis.iter_inbox_rows(xml)
        if not rows:
            print("  无可见会话行（可能当前不是 Chats 页）")
            # 再尝试点 Chats tab
            continue

        print(f"  可见 {len(rows)} 条会话：")
        for i, row in enumerate(rows):
            tag = " (self_last)" if row.is_self_last else ""
            preview = row.preview[:60].replace("\n", " ")
            y = row.bounds.top
            print(f"    [{i}] y={y:4d}  {preview!r}{tag}")

        # 3) --open-first：开顶上第一条 non-self 会话，读 peer name
        if args.open_first:
            non_self = [r for r in rows if not r.is_self_last]
            if not non_self:
                print("  --open-first: 没有 non-self 会话")
                continue
            target = non_self[0]
            print(
                f"  --open-first: 点进 bounds={target.bounds.as_tuple()} "
                f"(preview={target.preview[:40]!r}) …",
            )
            cx, cy = target.center
            adb.run_adb(
                ["shell", f"input tap {cx} {cy}"],
                serial=serial, timeout=5.0,
            )
            await asyncio.sleep(2.0)
            vt_xml = ta.dump_view_tree(serial)
            title = uis.find_thread_title(vt_xml) if vt_xml else None
            print(f"    ↳ 顶栏 peer name = {title!r}")
            # back 回 Chats（best-effort）
            adb.run_adb(
                ["shell", "input keyevent KEYCODE_BACK"],
                serial=serial, timeout=3.0,
            )
            await asyncio.sleep(0.6)

        print()

    print("提示：互发测试时，把对应的 peer name 用 --peer-a/--peer-b 传给 "
          "msgr_mutual_chat_test.py（避开 config.yaml 里静态写死）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
