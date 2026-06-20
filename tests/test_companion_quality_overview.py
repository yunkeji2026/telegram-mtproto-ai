"""O·P 联动质量看板：metrics 聚合 getter + /api/companion/quality-overview 端点。

覆盖：care/reactivation 的 skip 原因直方图 + like/dislike 反馈率 + dry_run 计数 +
共享黑名单规模；window 过滤；端点契约。
"""
from src.monitoring.metrics_store import get_metrics_store


def _reset():
    ms = get_metrics_store()
    for name in ("_care_skipped_recent", "_care_feedback_recent", "_care_dry_samples",
                 "_reactivation_skipped_recent", "_reactivation_feedback_recent",
                 "_reactivation_scheduled_recent", "_reactivation_dry_run_recent",
                 "_reactivation_disliked_replies"):
        getattr(ms, name).clear()
    return ms


def test_quality_overview_aggregates_both_lines():
    ms = _reset()
    # care：2 skip（no_context×2）+ 1 skip(identity_leak) + like1/dislike1
    ms.record_care_skipped("no_context")
    ms.record_care_skipped("no_context")
    ms.record_care_skipped("identity_leak")
    ms.record_care_feedback("like")
    ms.record_care_feedback("dislike")
    ms.record_care_dry_run(sample={"care_id": 1, "topic": "x", "reply_text": "hi"})
    # reactivation：1 skip + dislike1
    ms.record_reactivation_skipped("no_episodic")
    ms.record_reactivation_feedback("dislike", 0)
    ms.add_disliked_reply("某条被否决话术")

    ov = ms.companion_quality_overview(window_sec=86400)
    assert ov["care"]["skipped"] == 3
    assert ov["care"]["skip_reasons"]["no_context"] == 2
    assert ov["care"]["skip_reasons"]["identity_leak"] == 1
    assert ov["care"]["dry_run"] == 1
    assert ov["care"]["feedback"]["like"] == 1
    assert ov["care"]["feedback"]["dislike"] == 1
    assert ov["care"]["feedback"]["like_rate_pct"] == 50.0
    assert ov["reactivation"]["skipped"] == 1
    assert ov["reactivation"]["skip_reasons"]["no_episodic"] == 1
    assert ov["reactivation"]["feedback"]["dislike"] == 1
    assert ov["disliked_blacklist_size"] == 1


def test_quality_overview_like_rate_none_when_no_feedback():
    ms = _reset()
    ov = ms.companion_quality_overview(window_sec=86400)
    assert ov["care"]["feedback"]["like_rate_pct"] is None
    assert ov["reactivation"]["feedback"]["like_rate_pct"] is None
    assert ov["care"]["skipped"] == 0


def test_record_care_feedback_ignores_bad_verdict():
    ms = _reset()
    ms.record_care_feedback("meh")
    ms.record_care_feedback("")
    assert len(ms._care_feedback_recent) == 0


def test_quality_overview_endpoint(auth_client):
    ms = _reset()
    ms.record_care_skipped("no_context")
    r = auth_client.get("/api/companion/quality-overview?window_hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "care" in body and "reactivation" in body
    assert body["care"]["skipped"] >= 1
    assert body["window_sec"] == 24 * 3600


def test_quality_trend_endpoint_disabled(auth_client):
    # 未注入 store → enabled:false（不报错）
    if hasattr(auth_client.app.state, "quality_trend_store"):
        delattr(auth_client.app.state, "quality_trend_store")
    r = auth_client.get("/api/companion/quality-trend?hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["enabled"] is False


def test_quality_trend_endpoint_enabled(auth_client):
    from src.monitoring.quality_trend_store import QualityTrendStore
    store = QualityTrendStore(":memory:")
    store.record_snapshot({"window_sec": 86400, "care": {"skipped": 2}})
    auth_client.app.state.quality_trend_store = store
    try:
        r = auth_client.get("/api/companion/quality-trend?hours=24")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["enabled"] is True
        assert body["count"] == 1
        assert body["points"][0]["care_skipped"] == 2
    finally:
        delattr(auth_client.app.state, "quality_trend_store")
