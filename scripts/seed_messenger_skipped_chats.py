"""一次性脚本：把已知的「不该自动回复」的 chat 写入 messenger_rpa_skipped_chats。

触发时机：每次改 skipped 清单时运行一次（幂等）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "config" / "messenger_rpa_state.db"

SKIP_LIST = [
    # (chat_key, chat_name, reason)
    ("messenger_rpa:Meta AI",      "Meta AI",      "meta_builtin_ai_no_reply"),
    ("messenger_rpa:Messenger",    "Messenger",    "system_account_no_reply"),
    ("messenger_rpa:Facebook",     "Facebook",     "system_account_no_reply"),
    ("messenger_rpa:Facebook Team","Facebook Team","system_account_no_reply"),
]


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"DB not found: {DB}")
    c = sqlite3.connect(str(DB))
    now = time.time()
    inserted = 0
    updated = 0
    for chat_key, chat_name, reason in SKIP_LIST:
        cur = c.execute(
            "SELECT chat_key FROM messenger_rpa_skipped_chats WHERE chat_key=?",
            (chat_key,),
        )
        exists = cur.fetchone() is not None
        c.execute(
            "INSERT INTO messenger_rpa_skipped_chats "
            "(chat_key, chat_name, reason, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_key) DO UPDATE SET "
            "  chat_name=excluded.chat_name, reason=excluded.reason",
            (chat_key, chat_name, reason, now),
        )
        if exists:
            updated += 1
        else:
            inserted += 1
    c.commit()
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT chat_key, chat_name, reason, "
        "datetime(created_at, 'unixepoch', 'localtime') as ct "
        "FROM messenger_rpa_skipped_chats ORDER BY created_at DESC"
    ).fetchall()
    c.close()
    print(f"[OK] inserted={inserted} updated={updated} total={len(rows)}")
    for r in rows:
        print(f"  - {dict(r)}")


if __name__ == "__main__":
    main()
