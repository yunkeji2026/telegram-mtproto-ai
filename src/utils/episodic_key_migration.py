"""跨平台记忆 key 迁移（数据治理·一次性工具）。

背景
====
情景记忆按 ``user_id`` key 存储。新收件箱统一引擎（``generate_inbox_draft``）写记忆时
经 :class:`CrossPlatformIdentity` 解析为 **canonical key** ``"<platform>:<uid>"``；而早期
原生 bot 产线若未传 ``platform``，会把同一个人的记忆写在**裸 key** ``"<uid>"`` 下。
于是「同一客户、两套 key」——旧记忆对新产线不可见，拉低记忆命中率。

本模块提供**先 dry-run 后落地**的并 key 工具：把裸 DM key 并入 canonical key。

安全边界
========
- 默认 **dry-run**（``plan_*`` 纯只读，不动 DB）；``apply_*`` 才写。
- 仅迁移**简单 DM key**（纯字母数字、无 ``_`` 无 ``:``）。群/``<cid>_<uid>`` 复合 key
  语义不同，默认跳过（``only_simple=True``）。
- 合并按 ``content_hash`` 去重（见 :meth:`EpisodicMemoryStore.merge_key`），幂等可重跑。
- canonical 目标 = ``"<platform>:<uid>"``，与 ``CrossPlatformIdentity.resolve`` 的默认
  规则一致；规划阶段不触碰 identity 表（不产生副作用）。

CLI::

    python -m src.utils.episodic_key_migration --db config/bot.db --platform telegram        # dry-run
    python -m src.utils.episodic_key_migration --db config/bot.db --platform telegram --apply # 落地
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

_SIMPLE_KEY = re.compile(r"^[A-Za-z0-9]+$")


def _is_canonical(key: str) -> bool:
    """已是 canonical（含 ``platform:`` 前缀）→ 无需迁移。"""
    return ":" in (key or "")


def _is_candidate(key: str, *, only_simple: bool) -> bool:
    key = str(key or "")
    if not key or _is_canonical(key):
        return False
    if only_simple:
        return bool(_SIMPLE_KEY.match(key))
    # 宽松模式：只要不是 canonical 且不含 ``_``（避免误并群 key）
    return "_" not in key


def plan_canonical_migration(
    store: Any, platform: str, *, only_simple: bool = True,
) -> List[Dict[str, Any]]:
    """规划（dry-run，纯只读）：返回每个待迁移裸 key 的动作。

    返回项：``{old_key, new_key, fact_count, target_exists, action}``，
    ``action`` ∈ ``{"merge", "rename"}``（target 已存在=merge，否则=rename）。
    """
    platform = str(platform or "").strip()
    if not platform:
        return []
    stats = store.list_key_stats()  # [(key, count), ...]
    counts = {k: c for k, c in stats}
    all_keys = set(counts.keys())
    plan: List[Dict[str, Any]] = []
    for key, cnt in stats:
        if not _is_candidate(key, only_simple=only_simple):
            continue
        target = f"{platform}:{key}"
        if target == key:
            continue
        plan.append({
            "old_key": key,
            "new_key": target,
            "fact_count": int(cnt),
            "target_exists": target in all_keys,
            "action": "merge" if target in all_keys else "rename",
        })
    return plan


def apply_canonical_migration(
    store: Any, platform: str, *, only_simple: bool = True,
) -> Dict[str, Any]:
    """落地迁移：对每个候选 key 调 ``merge_key`` 并入 canonical。返回汇总报告。"""
    plan = plan_canonical_migration(store, platform, only_simple=only_simple)
    moved_rows = 0
    merged_keys = 0
    details: List[Dict[str, Any]] = []
    for item in plan:
        n = store.merge_key(item["old_key"], item["new_key"])
        moved_rows += n
        merged_keys += 1
        details.append({**item, "moved_rows": n})
    return {
        "platform": platform,
        "candidates": len(plan),
        "merged_keys": merged_keys,
        "moved_rows": moved_rows,
        "details": details,
    }


def _main(argv: List[str] | None = None) -> int:
    import argparse

    from src.utils.episodic_memory_store import EpisodicMemoryStore

    ap = argparse.ArgumentParser(description="跨平台记忆 key 迁移（裸 key → canonical）")
    ap.add_argument("--db", required=True, help="bot.db 路径（含 episodic_memory 表）")
    ap.add_argument("--platform", required=True, help="canonical 前缀平台，如 telegram")
    ap.add_argument("--apply", action="store_true", help="落地执行（默认仅 dry-run）")
    ap.add_argument("--all-keys", action="store_true",
                    help="放宽到所有非 canonical 且不含下划线的 key（默认仅纯字母数字）")
    args = ap.parse_args(argv)

    store = EpisodicMemoryStore(args.db)
    only_simple = not args.all_keys
    if args.apply:
        rep = apply_canonical_migration(store, args.platform, only_simple=only_simple)
        print(f"[apply] platform={rep['platform']} 并入 {rep['merged_keys']} 个 key、"
              f"迁移 {rep['moved_rows']} 条事实")
        for d in rep["details"]:
            print(f"  {d['old_key']} → {d['new_key']}  "
                  f"({d['action']}, moved={d['moved_rows']}/{d['fact_count']})")
    else:
        plan = plan_canonical_migration(store, args.platform, only_simple=only_simple)
        print(f"[dry-run] platform={args.platform} 候选 {len(plan)} 个裸 key "
              f"（加 --apply 落地）：")
        for d in plan:
            print(f"  {d['old_key']} → {d['new_key']}  "
                  f"({d['action']}, facts={d['fact_count']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
