"""诊断：查看 messenger chat 在 bot.db 的 user_context 是否已有持久化 _conversation_history。"""
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "config" / "bot.db"


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"missing: {DB}")
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT user_id, updated_at, length(data) as dlen, data FROM user_context "
        "WHERE user_id LIKE 'messenger_rpa:%' "
        "ORDER BY updated_at DESC LIMIT 10"
    ).fetchall()
    c.close()
    print(f"messenger_rpa user_contexts: {len(rows)}")
    for r in rows:
        d = {}
        try:
            d = json.loads(r["data"])
        except Exception:
            pass
        ch = d.get("_conversation_history") or []
        print(
            f"  {r['user_id']} dlen={r['dlen']} hist_turns={len(ch)//2} "
            f"last_msg={(d.get('last_message') or '')[:60]!r} "
            f"last_reply={(d.get('last_reply') or '')[:60]!r}"
        )
        if ch:
            for m in ch[-6:]:
                role = m.get("role", "?")
                content = (m.get("content") or "")[:80]
                print(f"     [{role}] {content!r}")


if __name__ == "__main__":
    main()
