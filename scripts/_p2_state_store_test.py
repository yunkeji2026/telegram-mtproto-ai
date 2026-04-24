"""P2 state_store 单元验证：variant/sla/extra 三大新方法。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "test.db"
        s = MessengerRpaStateStore(str(p), max_runs_kept=20)

        v1 = s.assign_variant("chat-alpha", weights={"A": 1.0, "B": 1.0})
        v1_again = s.assign_variant("chat-alpha", weights={"A": 1.0, "B": 1.0})
        assert v1 == v1_again, f"sticky failed: {v1}!={v1_again}"
        print(f"[1] variant sticky OK: chat-alpha -> {v1}")

        c = Counter()
        for i in range(200):
            v = s.assign_variant(f"chat-{i}", weights={"A": 1.0, "B": 1.0})
            c[v] += 1
        print(f"[2] variant distribution over 200 chats: {dict(c)}")

        vs = s.variant_stats()
        print(f"[3] variant_stats keys: {list(vs.get('variants', {}).keys())}")

        sla0 = s.pending_sla_stats(threshold_sec=300)
        print(f"[4] sla empty: {sla0}")
        assert sla0["pending_count"] == 0 and sla0["overdue_count"] == 0

        aid = s.enqueue_approval(
            chat_key="c1", chat_name="Alice", peer_text="hi",
            peer_kind="text", reply_text="hello", screenshot_path="",
            run_id="rid1", extra={},
        )
        with sqlite3.connect(str(p)) as cc:
            cc.execute(
                "UPDATE messenger_rpa_approvals SET created_at=? WHERE id=?",
                (time.time() - 1000, aid),
            )
            cc.commit()
        sla2 = s.pending_sla_stats(threshold_sec=300)
        print(f"[5] sla overdue after aging: {sla2}")
        assert sla2["overdue_count"] == 1 and sla2["pending_count"] == 1

        ok = s.patch_approval_extra(
            aid, patch={"image_caption": "a girl smiling"},
        )
        assert ok
        got = s.get_approval(aid)
        ex = json.loads(got["extra_json"] or "{}")
        assert ex.get("image_caption") == "a girl smiling", ex
        print(f"[6] patch_approval_extra OK: caption='{ex['image_caption']}'")

        # Batch decide: 创建 3 条 pending + 批量 reject
        ids = []
        for i in range(3):
            ids.append(
                s.enqueue_approval(
                    chat_key=f"bulk{i}", chat_name=f"B{i}",
                    peer_text=f"m{i}", peer_kind="text",
                    reply_text=f"r{i}", screenshot_path="", run_id=f"rid{i}",
                    extra={},
                )
            )
        for bid in ids:
            ok = s.decide_approval(
                bid, approve=False, decided_by="test", decision_note="bulk",
            )
            assert ok
        pending_after = s.list_approvals(status="pending")
        # only the aged one remained pending（前面被批量 reject）
        pending_ids = [a["id"] for a in pending_after]
        assert aid in pending_ids and all(b not in pending_ids for b in ids)
        print(f"[7] batch reject OK: {len(ids)} rejected, pending={pending_ids}")

    print("\n=== ALL P2 STATE-STORE TESTS PASSED ===")


if __name__ == "__main__":
    main()
