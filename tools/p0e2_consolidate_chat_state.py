"""P0-E2 数据迁移：合并 OCR 抖动分裂的 chat_state 行。

监控期发现同一真实用户被 OCR 抖动分裂成多个 chat_key（如
"レッドスイス佳奈"/"レッドスター佳奈"/"レッドジョージ" 实际是同一人）。
此脚本用 SequenceMatcher.ratio 聚合相似 chat_name，选最长名为 canonical，
合并其他行的关键字段（last_sent_at / last_reply / last_peer_text 等），
删除孤儿行。

用法：
    # 1. dry-run（预览）
    python tools/p0e2_consolidate_chat_state.py config/messenger_rpa_state_vwnj_test.db

    # 2. 真正合并（写入数据库）
    python tools/p0e2_consolidate_chat_state.py config/messenger_rpa_state_vwnj_test.db --commit

    # 3. 调阈值（默认 0.85，可在 0.7-0.9 间调）
    python tools/p0e2_consolidate_chat_state.py <db> --threshold 0.75 --commit

    # 4. 多 DB 批量（shell 循环）
    for db in config/messenger_rpa_state*.db; do
      python tools/p0e2_consolidate_chat_state.py "$db" --commit
    done

注意：
- 默认 DRY-RUN，不修改数据库
- 建议先 dry-run 看 mapping report，人工确认 cluster 合理后再 --commit
- 重要：先停止 main.py 进程再 commit，避免并发写入冲突
- 备份：脚本会自动在 commit 前生成 .backup-{ts} 副本
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _name_from_key(chat_key: str) -> str:
    """取 'prefix:name' 中的 name 部分。"""
    _, _, name = chat_key.partition(":")
    return name


def cluster_chat_rows(
    rows: List[Dict[str, Any]], threshold: float,
) -> List[List[Dict[str, Any]]]:
    """Greedy 聚合：扫描每行尝试加入已存在的 cluster，否则新建。

    SequenceMatcher.ratio() 复杂度 O(N*M)，对几百行规模够用。
    """
    clusters: List[List[Dict[str, Any]]] = []
    for r in rows:
        name_lower = _name_from_key(r["chat_key"]).casefold().strip()
        if not name_lower:
            clusters.append([r])
            continue
        placed = False
        for cluster in clusters:
            # 与 cluster 内任一成员相似度 >= threshold 即归入
            for member in cluster:
                m_name = _name_from_key(member["chat_key"]).casefold().strip()
                if not m_name:
                    continue
                ratio = SequenceMatcher(None, name_lower, m_name).ratio()
                if ratio >= threshold:
                    cluster.append(r)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append([r])
    return clusters


def pick_canonical(cluster: List[Dict[str, Any]]) -> Dict[str, Any]:
    """选最长 chat_name + 最近 updated 作为 canonical。

    OCR 抖动通常是漏字符（"Victor Zan" → "Victor"），保留更长的更可能是真实名。
    """
    return max(
        cluster,
        key=lambda r: (
            len(_name_from_key(r["chat_key"])),
            float(r.get("updated_at") or 0),
        ),
    )


def merge_fields(
    canonical: Dict[str, Any], members: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """合并 cluster 字段：max(last_sent_at) + 取最近非空值。"""
    last_sent_at = max(
        (float(m.get("last_sent_at") or 0) for m in members),
        default=0.0,
    )
    sorted_by_recency = sorted(
        members,
        key=lambda m: -(float(m.get("last_sent_at") or 0)),
    )

    def pick_latest_non_empty(field: str) -> str:
        for m in sorted_by_recency:
            v = m.get(field) or ""
            if v and str(v).strip():
                return v
        return canonical.get(field, "") or ""

    return {
        "chat_key": canonical["chat_key"],
        "chat_name": canonical.get("chat_name") or _name_from_key(
            canonical["chat_key"]
        ),
        "last_peer_text": pick_latest_non_empty("last_peer_text"),
        "last_peer_fp": pick_latest_non_empty("last_peer_fp"),
        "last_peer_kind": pick_latest_non_empty("last_peer_kind"),
        "last_reply": pick_latest_non_empty("last_reply"),
        "last_screen_sha256": pick_latest_non_empty("last_screen_sha256"),
        "last_sent_at": last_sent_at,
    }


def print_clusters_report(
    db_path: Path,
    rows: List[Dict[str, Any]],
    clusters: List[List[Dict[str, Any]]],
) -> Tuple[int, int]:
    """打印 cluster 报告，返回 (待合并 cluster 数, 待删除孤儿数)。"""
    print(f"\n=== {db_path.name} ===")
    print(f"Total rows: {len(rows)}, Clusters: {len(clusters)}")

    n_clusters_to_merge = 0
    n_orphans_to_delete = 0
    for cluster in clusters:
        if len(cluster) <= 1:
            continue
        canonical = pick_canonical(cluster)
        n_clusters_to_merge += 1
        n_orphans_to_delete += len(cluster) - 1
        print(f"\n  [Cluster] canonical={canonical['chat_key']!r}:")
        for m in cluster:
            is_canonical = m["chat_key"] == canonical["chat_key"]
            marker = "    KEEP" if is_canonical else "    DROP"
            sent_at = float(m.get("last_sent_at") or 0)
            sent_at_str = (
                time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(sent_at)
                )
                if sent_at > 0 else "(never)"
            )
            print(
                f"    {marker} {m['chat_key']!r:<60s} "
                f"last_sent={sent_at_str}"
            )
    return n_clusters_to_merge, n_orphans_to_delete


def commit_consolidation(
    conn: sqlite3.Connection,
    clusters: List[List[Dict[str, Any]]],
) -> int:
    """在事务中执行合并 + 删除孤儿。返回删除的行数。"""
    n_deleted = 0
    cursor = conn.cursor()
    now_ts = time.time()
    for cluster in clusters:
        if len(cluster) <= 1:
            continue
        canonical = pick_canonical(cluster)
        merged = merge_fields(canonical, cluster)
        cursor.execute(
            "UPDATE messenger_rpa_chat_state SET "
            "  chat_name=?, last_peer_text=?, last_peer_fp=?, "
            "  last_peer_kind=?, last_reply=?, last_screen_sha256=?, "
            "  last_sent_at=?, updated_at=? "
            "WHERE chat_key=?",
            (
                merged["chat_name"],
                merged["last_peer_text"],
                merged["last_peer_fp"],
                merged["last_peer_kind"],
                merged["last_reply"],
                merged["last_screen_sha256"],
                merged["last_sent_at"],
                now_ts,
                merged["chat_key"],
            ),
        )
        for m in cluster:
            if m["chat_key"] != canonical["chat_key"]:
                cursor.execute(
                    "DELETE FROM messenger_rpa_chat_state WHERE chat_key=?",
                    (m["chat_key"],),
                )
                n_deleted += 1
    conn.commit()
    return n_deleted


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="P0-E2 chat_state OCR-split consolidation",
    )
    parser.add_argument("db_path", type=Path, help="SQLite DB 路径")
    parser.add_argument(
        "--threshold", type=float, default=0.85,
        help="SequenceMatcher 相似度阈值 (默认 0.85)",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="真正写入（默认 dry-run）",
    )
    args = parser.parse_args(argv)

    db_path: Path = args.db_path
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM messenger_rpa_chat_state ORDER BY updated_at DESC",
        ).fetchall()
    ]

    if not rows:
        print(f"{db_path.name}: empty table, nothing to consolidate")
        return 0

    clusters = cluster_chat_rows(rows, args.threshold)
    n_clusters, n_orphans = print_clusters_report(db_path, rows, clusters)

    if n_clusters == 0:
        print("\n[OK] No OCR-split clusters detected; nothing to consolidate.")
        return 0

    print(
        f"\nSummary: {n_clusters} clusters with splits, "
        f"{n_orphans} orphan rows to delete, "
        f"{len(rows) - n_orphans} rows after consolidation"
    )

    if not args.commit:
        print("\n[DRY-RUN] No changes written. Re-run with --commit to apply.")
        return 0

    # commit 前自动备份
    backup_path = db_path.with_suffix(
        f".backup-{time.strftime('%Y%m%d_%H%M%S')}.db",
    )
    shutil.copy2(db_path, backup_path)
    print(f"\n[BACKUP] {backup_path}")

    n_deleted = commit_consolidation(conn, clusters)
    print(f"[OK] Committed. Deleted {n_deleted} orphan rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
