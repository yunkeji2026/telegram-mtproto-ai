"""E3 人设自身长期记忆门禁（去标识聚合，纯核心 + store）。"""
import pytest

from src.companion.persona_self_memory import (
    PersonaSelfMemoryStore,
    extract_self_topic,
    format_self_memory,
)


def test_extract_topic_basic():
    t = extract_self_topic("最近好多人问我大阪攻略怎么做")
    assert t and 2 <= len(t) <= 6
    assert extract_self_topic("ok") is None
    assert extract_self_topic("") is None
    assert extract_self_topic("123 456") is None  # 纯数字无中文


def test_format_self_memory():
    assert format_self_memory([]) == ""
    out = format_self_memory(["大阪攻略", "抹茶甜点"])
    assert "大阪攻略" in out and "见闻" in out
    assert "绝不透露" in out  # 隐私红线文案在


def test_store_record_and_top(tmp_path):
    s = PersonaSelfMemoryStore(str(tmp_path / "sm.db"))
    # 同一话题被多人问 3 次
    for _ in range(3):
        s.record_topic("lin_xiaoyu", "大阪攻略")
    s.record_topic("lin_xiaoyu", "只问一次的")
    s.record_topic("marcus_wei", "别人的话题")  # 另一个人设隔离
    top = s.top_topics("lin_xiaoyu", min_count=3, k=4)
    assert "大阪攻略" in top
    assert "只问一次的" not in top          # 未达 min_count
    assert "别人的话题" not in top          # 人设隔离


def test_store_blank_safe(tmp_path):
    s = PersonaSelfMemoryStore(str(tmp_path / "sm.db"))
    s.record_topic("", "x")
    s.record_topic("p", "")
    assert s.top_topics("") == []
    assert s.top_topics("p") == []
