"""P1-D migration: cancel pending approval rows with empty reply_text.

Run once. Marks any row with status='pending' and empty/NULL reply_text as
'cancelled' with decision_note='P1-D: empty_reply_stale'. Rows with real
reply_text are preserved regardless of age.

Usage:
    python tools/migrate_clean_empty_pending.py [--dry-run]

Without --dry-run, the UPDATE is committed. With --dry-run, only counts are
printed (no writes).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_DIR = Path(__file__).resolve().parents[1] / "config"

# All sqlite files that might hold the messenger_rpa_approvals table
CANDIDATE_DBS = [
    "messenger_rpa_approvals.db",
    "messenger_rpa_state.db",
    "messenger_rpa_state_bg_phone_1.db",
    "messenger_rpa_state_bg_phone_2.db",
]


def run(dry_run: bool) -> int:
    now = time.time()
    total_affected = 0
    total_preserved = 0
    for db in CANDIDATE_DBS:
        path = DB_DIR / db
        if not path.is_file():
            print(f"- skip {db}: not found")
            continue
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            tables = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            if "messenger_rpa_approvals" not in tables:
                print(f"- skip {db}: no approvals table")
                conn.close()
                continue

            # Count what we will cancel (empty reply) vs preserve (real reply)
            empty_count = cur.execute(
                "SELECT COUNT(*) FROM messenger_rpa_approvals "
                "WHERE status='pending' "
                "AND (reply_text IS NULL OR TRIM(reply_text)='')"
            ).fetchone()[0]
            real_count = cur.execute(
                "SELECT COUNT(*) FROM messenger_rpa_approvals "
                "WHERE status='pending' "
                "AND reply_text IS NOT NULL AND TRIM(reply_text)<>''"
            ).fetchone()[0]
            total_preserved += real_count

            print(f"\n=== {db} ===")
            print(f"  will cancel (empty reply): {empty_count}")
            print(f"  will preserve (real reply): {real_count}")

            # Per-chat breakdown of what will be cancelled
            per_chat = cur.execute(
                "SELECT chat_name, COUNT(*) as n FROM messenger_rpa_approvals "
                "WHERE status='pending' "
                "AND (reply_text IS NULL OR TRIM(reply_text)='') "
                "GROUP BY chat_name ORDER BY n DESC"
            ).fetchall()
            for r in per_chat:
                print(f"    - {r['chat_name']!r}: {r['n']}")

            if dry_run:
                conn.close()
                continue

            if empty_count > 0:
                cur.execute(
                    "UPDATE messenger_rpa_approvals "
                    "SET status='cancelled', "
                    "    decided_at=?, "
                    "    decided_by='P1-D_migration', "
                    "    decision_note='empty_reply_stale' "
                    "WHERE status='pending' "
                    "AND (reply_text IS NULL OR TRIM(reply_text)='')",
                    (now,),
                )
                affected = cur.rowcount
                total_affected += affected
                conn.commit()
                print(f"  cancelled rows: {affected}")
            conn.close()
        except Exception as ex:
            print(f"  ERR {type(ex).__name__}: {ex}")
    print("\n---")
    print(f"TOTAL cancelled: {total_affected}")
    print(f"TOTAL preserved (real-reply pending): {total_preserved}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="count only; no UPDATE")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
