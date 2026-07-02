"""防复读调参顾问（src.utils.anti_repeat_advisor）纯函数单测。

锁定：
  - 样本不足 → sample_ok=False 且无建议（不给噪音）。
  - 缓存命中率极高 → 建议下调容量（不低于 floor）；偏低 → 建议上调。
  - 语义层零命中 → 建议关闭；贡献显著 → 明确价值、建议保留。
  - 阈值可经 config.inbox.auto_draft.anti_repeat.advisor.* 覆盖。
"""

from __future__ import annotations

from src.utils.anti_repeat_advisor import evaluate_anti_repeat_tuning


def _metrics(*, checks=0, sem_trig=0, sem_share=0.0, hit=0, miss=0, hit_rate=0.0):
    return {
        "checks": checks,
        "semantic_triggered": sem_trig,
        "semantic_share_pct": sem_share,
        "embed_cache": {"hit": hit, "miss": miss, "hit_rate_pct": hit_rate},
    }


def _ids(res):
    return {s["id"] for s in res["suggestions"]}


def test_insufficient_samples_stays_silent():
    res = evaluate_anti_repeat_tuning(_metrics(checks=10, hit=5, miss=5, hit_rate=50.0))
    assert res["sample_ok"] is False
    assert res["suggestions"] == []


def test_high_hit_rate_suggests_shrink():
    m = _metrics(checks=300, sem_trig=5, sem_share=10.0, hit=990, miss=10, hit_rate=99.0)
    res = evaluate_anti_repeat_tuning(m, current_cache_max=512)
    assert "embed_cache_shrink" in _ids(res)
    s = next(s for s in res["suggestions"] if s["id"] == "embed_cache_shrink")
    assert s["suggested"]["inbox.auto_draft.anti_repeat.semantic.embed_cache_max"] == 256
    assert "embed_cache_grow" not in _ids(res)


def test_shrink_respects_floor():
    m = _metrics(checks=300, hit=990, miss=10, hit_rate=99.0)
    res = evaluate_anti_repeat_tuning(m, current_cache_max=64)  # 已在 floor
    assert "embed_cache_shrink" not in _ids(res)  # 不再下调


def test_low_hit_rate_suggests_grow():
    m = _metrics(checks=50, hit=200, miss=300, hit_rate=40.0)
    res = evaluate_anti_repeat_tuning(m, current_cache_max=512)
    assert "embed_cache_grow" in _ids(res)
    s = next(s for s in res["suggestions"] if s["id"] == "embed_cache_grow")
    assert s["level"] == "warning"
    assert s["suggested"]["inbox.auto_draft.anti_repeat.semantic.embed_cache_max"] == 1024


def test_semantic_zero_hits_suggests_disable():
    m = _metrics(checks=500, sem_trig=0, sem_share=0.0, hit=100, miss=5, hit_rate=95.2)
    res = evaluate_anti_repeat_tuning(m)
    assert "semantic_no_value" in _ids(res)
    s = next(s for s in res["suggestions"] if s["id"] == "semantic_no_value")
    assert s["suggested"]["inbox.auto_draft.anti_repeat.semantic.enabled"] is False


def test_semantic_valuable_suggests_keep():
    m = _metrics(checks=500, sem_trig=60, sem_share=35.0)
    res = evaluate_anti_repeat_tuning(m)
    assert "semantic_valuable" in _ids(res)
    assert "semantic_no_value" not in _ids(res)


def test_thresholds_overridable_via_config():
    # 抬高 min_samples → 原本够的样本变为不足 → 不再给缓存建议
    cfg = {"inbox": {"auto_draft": {"anti_repeat": {"advisor": {
        "min_samples": 5000, "semantic_min_checks": 5000,
    }}}}}
    m = _metrics(checks=300, hit=990, miss=10, hit_rate=99.0)
    res = evaluate_anti_repeat_tuning(m, cfg, current_cache_max=512)
    assert res["sample_ok"] is False
    assert res["suggestions"] == []


def test_observed_and_thresholds_echoed():
    m = _metrics(checks=300, sem_trig=5, sem_share=10.0, hit=990, miss=10, hit_rate=99.0)
    res = evaluate_anti_repeat_tuning(m, current_cache_max=256)
    assert res["observed"]["current_cache_max"] == 256
    assert res["observed"]["embed_cache_total"] == 1000
    assert res["thresholds"]["cache_hit_high_pct"] == 98.0
