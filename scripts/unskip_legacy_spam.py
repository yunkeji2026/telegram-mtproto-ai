"""一键清理 messenger_rpa_skipped_chats 中的「历史 LOW-confidence 误判」。

背景：
    优化 A 把 spam 判定改为 HIGH/LOW 分级 — HIGH 永久 skip，LOW 单次跳过。
    但**优化前**就被永久标记的 chats（reason='msg_level_spam:keywords'）
    没有 level 信息，无法精确区分；保守做法是清掉这批让 runtime 重判。

    对真 spam（赌博推广等）：下次扫到会立刻再被 HIGH 标记 → 仍永久 skip
    对误判：日常无 LOW keyword 的消息不再被标 → 客户被回复

用法：
    python scripts/unskip_legacy_spam.py                   # dry-run 看会清掉哪些
    python scripts/unskip_legacy_spam.py --apply           # 真删
    python scripts/unskip_legacy_spam.py --apply --db config/messenger_rpa_state_bg_phone_2.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 历史 LOW/混合 reason 模式（这些都该清，让 runtime 用新分级重判）
LEGACY_REASONS = (
    "msg_level_spam:keywords",   # 优化 A 之前的旧 reason
    "msg_level_spam:low:",        # 新 LOW 格式（理应不入此表，防御性清理）
)

# 这些保留：HIGH 永久 skip + 系统账号 + 业务手动标记
PROTECTED_PREFIXES = (
    "msg_level_spam:high:",
    "system_account_no_reply",
    "meta_builtin_ai_no_reply",
    "manual:",  # 运营手动标
)


def _scan_db(db_path: Path):
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT chat_key, chat_name, reason, created_at "
            "FROM messenger_rpa_skipped_chats ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return rows


def _is_legacy(reason: str) -> bool:
    r = (reason or "").strip()
    if not r:
        return True  # 空 reason 也算可疑
    for p in PROTECTED_PREFIXES:
        if r.startswith(p):
            return False
    for p in LEGACY_REASONS:
        if r.startswith(p):
            return True
    # 其他不在 protected 也不在 legacy 的 reason → 保守保留
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db", action="append", default=None,
        help="state db 路径，可重复；省略时自动扫 config/messenger_rpa_state*.db",
    )
    ap.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    args = ap.parse_args()

    if args.db:
        db_paths = [Path(p) for p in args.db]
    else:
        db_paths = list(Path("config").glob("messenger_rpa_state*.db"))

    total_legacy = 0
    total_protected = 0
    for db in db_paths:
        rows = _scan_db(db)
        if rows is None:
            print(f"[skip] {db}: 无表或不存在")
            continue
        legacy = [r for r in rows if _is_legacy(r[2])]
        protected = [r for r in rows if not _is_legacy(r[2])]
        print(f"\n=== {db} (legacy={len(legacy)}, protected={len(protected)}) ===")
        for r in legacy:
            print(f"  [LEGACY-WILL-CLEAR] name={(r[1] or '')[:25]:25} "
                  f"reason={(r[2] or '')[:50]}")
        for r in protected[:5]:
            print(f"  [PROTECTED-KEEP]   name={(r[1] or '')[:25]:25} "
                  f"reason={(r[2] or '')[:50]}")
        if len(protected) > 5:
            print(f"  ... +{len(protected) - 5} more protected rows")

        if args.apply and legacy:
            conn = sqlite3.connect(str(db))
            try:
                for r in legacy:
                    conn.execute(
                        "DELETE FROM messenger_rpa_skipped_chats WHERE chat_key=?",
                        (r[0],),
                    )
                conn.commit()
                print(f"  ✅ DELETED {len(legacy)} rows")
            finally:
                conn.close()
        total_legacy += len(legacy)
        total_protected += len(protected)

    print(f"\nTotal: legacy={total_legacy}, protected={total_protected}")
    if not args.apply:
        print("\n(dry-run; 加 --apply 真删)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
