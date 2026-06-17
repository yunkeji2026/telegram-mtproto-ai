"""M9+ KB 缺口飞轮统一优先级排序测试：kb_gap 纯函数。"""

from src.utils.kb_gap import gap_backlog_summary, gap_priority_score, rank_kb_gaps


def test_miss_outranks_weak_at_same_count():
    miss = {"source": "miss", "query": "退款多久", "count": 5}
    weak = {"source": "weak", "query": "运费", "count": 5}
    assert gap_priority_score(miss) > gap_priority_score(weak)


def test_frequency_increases_score_but_log_compressed():
    low = gap_priority_score({"source": "miss", "count": 2})
    high = gap_priority_score({"source": "miss", "count": 100})
    assert high > low
    # 对数压缩：100 次不应是 2 次的 50 倍
    assert high < low * 5


def test_rank_sorts_desc_and_tiers():
    suggestions = [
        {"source": "overloaded", "query": "泛条目", "count": 3},
        {"source": "miss", "query": "高频未命中", "count": 20},
        {"source": "weak", "query": "弱命中", "count": 4},
    ]
    ranked = rank_kb_gaps(suggestions)
    assert ranked[0]["query"] == "高频未命中"
    assert ranked[0]["priority_tier"] == "high"
    # 分数单调不增
    scores = [r["priority_score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all("priority_score" in r and "priority_tier" in r for r in ranked)


def test_count_field_aliases():
    # 支持 cnt / count / frequency 任一
    assert gap_priority_score({"source": "miss", "cnt": 10}) == \
        gap_priority_score({"source": "miss", "count": 10})


def test_rank_handles_empty_and_garbage():
    assert rank_kb_gaps([]) == []
    ranked = rank_kb_gaps([None, "x", {"source": "miss", "count": 1}])  # 容错
    assert len(ranked) == 1


def test_top_k_truncates():
    items = [{"source": "miss", "query": f"q{i}", "count": i} for i in range(50)]
    assert len(rank_kb_gaps(items, top_k=5)) == 5


def test_backlog_summary():
    ranked = rank_kb_gaps([
        {"source": "miss", "query": "A", "count": 30},
        {"source": "overloaded", "query": "B", "count": 1},
    ])
    s = gap_backlog_summary(ranked)
    assert s["total"] == 2
    assert s["top_query"] == "A"
    assert s["tiers"]["high"] >= 1
