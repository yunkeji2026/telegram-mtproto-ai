"""深度人设趋势/AB 快照门禁（默认关骨架）。"""
from src.companion.deep_persona_trend import (
    DeepPersonaTrendStore,
    flatten_stats_for_trend,
)


def test_flatten_stats_incl_embedder():
    dump = {"consolidations": 5, "callbacks_emitted": 2, "life_shares": 1,
            "embedder": {"calls": 40, "avg_latency_ms": 88.0}}
    flat = flatten_stats_for_trend(dump)
    assert flat["consolidations"] == 5
    assert flat["callbacks_emitted"] == 2
    assert flat["embed_calls"] == 40
    assert flat["embed_avg_latency_ms"] == 88.0


def test_upsert_and_read(tmp_path):
    s = DeepPersonaTrendStore(str(tmp_path / "tr.db"))
    s.upsert_today({"consolidations": 3, "callbacks_emitted": 1}, day="2026-07-01")
    s.upsert_today({"consolidations": 5, "callbacks_emitted": 2}, day="2026-07-01")  # 覆盖
    s.upsert_today({"consolidations": 9}, day="2026-07-02")
    rows = s.read_recent(7)
    assert len(rows) == 2
    # 升序（旧→新）
    assert rows[0]["day"] == "2026-07-01" and rows[0]["consolidations"] == 5
    assert rows[1]["day"] == "2026-07-02" and rows[1]["consolidations"] == 9


def test_read_empty(tmp_path):
    s = DeepPersonaTrendStore(str(tmp_path / "tr.db"))
    assert s.read_recent(7) == []
