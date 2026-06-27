"""跨平台记忆 key 迁移工具单测（数据治理）。

锁定契约：
  - EpisodicMemoryStore.list_key_stats / merge_key 原语（去重并 key）
  - plan_canonical_migration：dry-run 只读，正确识别裸 DM key、跳过 canonical/群 key
  - apply_canonical_migration：落地并 key、按 content_hash 去重、幂等可重跑
"""

from __future__ import annotations

from src.utils.episodic_key_migration import (
    apply_canonical_migration,
    plan_canonical_migration,
)
from src.utils.episodic_memory_store import EpisodicMemoryStore


def _store(tmp_path):
    return EpisodicMemoryStore(tmp_path / "mig.db")


def test_list_key_stats_and_merge_key(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "客户叫 Jun")
    s.add_fact("123", "喜欢夜聊")
    s.add_fact("telegram:123", "喜欢夜聊")   # 与目标重复内容
    stats = dict(s.list_key_stats())
    assert stats["123"] == 2
    assert stats["telegram:123"] == 1

    moved = s.merge_key("123", "telegram:123")
    assert moved == 1  # 仅「客户叫 Jun」迁移；「喜欢夜聊」重复被忽略
    stats2 = dict(s.list_key_stats())
    assert "123" not in stats2          # 旧 key 残留已清
    assert stats2["telegram:123"] == 2  # 去重后合计 2 条


def test_merge_key_noop_same_or_empty(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "fact a")
    assert s.merge_key("123", "123") == 0
    assert s.merge_key("", "telegram:1") == 0
    assert dict(s.list_key_stats())["123"] == 1


def test_plan_is_readonly_and_filters(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "fact a")          # 裸 DM key → 候选
    s.add_fact("telegram:999", "fact b")  # 已 canonical → 跳过
    s.add_fact("456_789", "group fact")   # 群/复合 key → 跳过(only_simple)

    plan = plan_canonical_migration(s, "telegram")
    olds = {p["old_key"] for p in plan}
    assert olds == {"123"}
    assert plan[0]["new_key"] == "telegram:123"
    assert plan[0]["action"] == "rename"  # target 不存在
    # dry-run 只读：keys 未变
    assert dict(s.list_key_stats()).get("123") == 1


def test_plan_merge_when_target_exists(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "fact a")
    s.add_fact("telegram:123", "fact b")
    plan = plan_canonical_migration(s, "telegram")
    item = [p for p in plan if p["old_key"] == "123"][0]
    assert item["action"] == "merge"
    assert item["target_exists"] is True


def test_apply_migration_and_idempotent(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "客户叫 Jun")
    s.add_fact("123", "喜欢夜聊")
    s.add_fact("789", "另一个客户")

    rep = apply_canonical_migration(s, "telegram")
    assert rep["merged_keys"] == 2
    assert rep["moved_rows"] == 3
    stats = dict(s.list_key_stats())
    assert stats.get("telegram:123") == 2
    assert stats.get("telegram:789") == 1
    assert "123" not in stats and "789" not in stats

    # 幂等：再跑一次无候选、零迁移
    rep2 = apply_canonical_migration(s, "telegram")
    assert rep2["candidates"] == 0
    assert rep2["moved_rows"] == 0


def test_key_health_counts_bare_and_canonical(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "裸 key 客户")
    s.add_fact("456", "另一裸 key")
    s.add_fact("telegram:999", "已 canonical")
    h = s.key_health(sample=5)
    assert h["total_keys"] == 3
    assert h["bare_keys"] == 2
    assert h["canonical_keys"] == 1
    assert h["bare_facts"] == 2
    assert h["bare_ratio"] == round(2 / 3, 4)
    bare_keys = {b["key"] for b in h["bare_samples"]}
    assert bare_keys == {"123", "456"}


def test_key_health_after_migration_is_clean(tmp_path):
    s = _store(tmp_path)
    s.add_fact("123", "客户叫 Jun")
    s.add_fact("telegram:999", "已 canonical")
    apply_canonical_migration(s, "telegram")
    h = s.key_health()
    assert h["bare_keys"] == 0
    assert h["bare_ratio"] == 0.0
    assert h["bare_samples"] == []


def test_key_health_empty_store(tmp_path):
    s = _store(tmp_path)
    h = s.key_health()
    assert h["total_keys"] == 0
    assert h["bare_ratio"] == 0.0


def test_all_keys_mode_still_skips_group_keys(tmp_path):
    s = _store(tmp_path)
    s.add_fact("alice", "用户名 key")     # 非数字但简单
    s.add_fact("456_789", "group fact")   # 含下划线 → 始终跳过
    plan = plan_canonical_migration(s, "telegram", only_simple=False)
    olds = {p["old_key"] for p in plan}
    assert "alice" in olds
    assert "456_789" not in olds
