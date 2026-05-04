"""W3-D2.1：批量 refresh 所有 journey 的 intimacy_score。

W3-D1.1 修了 intimacy_engine 接入 inbound 链路，但只对**新** msg_in 生效。
这个脚本对**已有**的 journey 各跑一次 IntimacyEngine.refresh_journey_intimacy，
让历史数据也反映正确的 intimacy_score（之前永远 0）。

用法：
  python scripts/migrate_intimacy_v1.py [--db config/contacts.db] [--dry-run]

dry-run 模式：只算分不写库，看哪些 journey 会涨/降。

幂等：refresh 每次都基于当前 events 算，重复跑结果一致。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 让 import src.* 工作（脚本通常从 repo 根跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore  # noqa: E402
from src.skills.intimacy_engine import IntimacyEngine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="config/contacts.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="只算分不写库（看 before/after 差）")
    ap.add_argument("--min-score-change", type=float, default=0.0,
                    help="只显示分数变化 >= 此值的 journey（debug 用）")
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[fail] db 不存在: {db_path}", file=sys.stderr)
        return 1

    store = ContactStore(db_path=db_path)
    engine = IntimacyEngine(store)

    # 拉所有 journey
    with store._lock:
        rows = store._conn.execute(
            "SELECT journey_id, funnel_stage, intimacy_score FROM journeys"
        ).fetchall()

    n_total = len(rows)
    n_changed = 0
    n_increased = 0
    n_decreased = 0
    by_stage = {}
    print(f"[migrate] 扫到 {n_total} 个 journey, 模式={'DRY-RUN' if args.dry_run else 'WRITE'}")
    print()

    for r in rows:
        jid = r["journey_id"]
        stage = r["funnel_stage"] or "?"
        old_score = float(r["intimacy_score"] or 0)
        # 计算（不写）
        bd = engine.compute_intimacy(jid, now=int(time.time()))
        new_score = bd.score
        delta = round(new_score - old_score, 2)
        if abs(delta) >= args.min_score_change:
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
            print(
                f"  {jid[:8]} stage={stage:18s} {old_score:5.1f} → {new_score:5.1f} {arrow} "
                f"(turn_in={bd.turn_count_in} turn_out={bd.turn_count_out} active_d={bd.active_days_7d})"
            )
        if delta != 0:
            n_changed += 1
            if delta > 0:
                n_increased += 1
            else:
                n_decreased += 1
        by_stage.setdefault(stage, {"total": 0, "changed": 0, "avg_delta": 0.0})
        by_stage[stage]["total"] += 1
        if delta != 0:
            by_stage[stage]["changed"] += 1
        by_stage[stage]["avg_delta"] += delta
        # 真写（★ W3-D3.1：_touch=False 避免破坏 silent_days）
        if not args.dry_run:
            try:
                store.update_journey(
                    jid, _touch=False,
                    intimacy_score=new_score,
                    intimacy_updated_at=int(time.time()),
                )
            except Exception as e:
                print(f"  [WARN] write fail jid={jid[:8]}: {e}", file=sys.stderr)

    # 汇总
    print()
    print(f"[summary] 总 {n_total} | 改变 {n_changed} | ↑{n_increased} ↓{n_decreased}")
    print("[by stage]")
    for stage, agg in sorted(by_stage.items()):
        avg = agg["avg_delta"] / max(1, agg["total"])
        print(
            f"  {stage:20s} total={agg['total']:3d} changed={agg['changed']:3d} "
            f"avg_delta={avg:+.2f}"
        )
    if args.dry_run:
        print()
        print("[hint] 这是 dry-run。再次跑去掉 --dry-run 真正写库。")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
