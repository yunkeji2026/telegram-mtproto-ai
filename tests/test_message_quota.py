"""Phase K：月度消息配额软状态单测。

此前用量看板只对照「席位」，无「消息含量」软提示。本阶段补 `message_quota_status` +
`plan_included_messages`（billing 纯函数）并接入 /api/workspace/usage 概览。
"""
from src.utils.billing import (
    DEFAULT_PRICING,
    message_quota_status,
    plan_included_messages,
)


# ── plan_included_messages ───────────────────────────────────────────────────

def test_plan_included_default_pricing():
    assert plan_included_messages("basic") == 5000
    assert plan_included_messages("pro") == 20000
    assert plan_included_messages("flagship") == 0   # 不限
    assert plan_included_messages("community") == 0

def test_plan_included_unknown_falls_back_community():
    assert plan_included_messages("nope") == 0

def test_plan_included_custom_pricing():
    pr = {"plans": {"basic": {"included_messages": 1234}}}
    assert plan_included_messages("basic", pr) == 1234


# ── message_quota_status ─────────────────────────────────────────────────────

def test_quota_unlimited_when_included_zero():
    q = message_quota_status(99999, 0)
    assert q["level"] == "ok" and q["included"] == 0 and q["ratio"] is None

def test_quota_ok_below_80pct():
    q = message_quota_status(100, 1000)
    assert q["level"] == "ok" and q["ratio"] == 0.1

def test_quota_warn_at_80pct():
    q = message_quota_status(800, 1000)
    assert q["level"] == "warn"

def test_quota_over():
    q = message_quota_status(1200, 1000)
    assert q["level"] == "over" and "超" in q["text"]

def test_quota_boundary_exactly_included_is_ok():
    q = message_quota_status(1000, 1000)
    assert q["level"] in ("ok", "warn")  # 100% 不算 over（未超）
    assert q["level"] == "warn"          # ratio=1.0 >=0.8 → warn


# ── 接入 build_usage_summary（用 fake store + fake license）──────────────────

def test_usage_summary_includes_message_quota(monkeypatch):
    import src.web.routes.unified_inbox_usage_routes as ur

    class _Store:
        def get_usage_stats(self, since, until_ts=None):
            return {"messages_total": 4500, "messages_in": 2000, "messages_out": 2500,
                    "ai_calls": 100, "ai_sent": 50, "active_agents": 1, "trend": []}

    monkeypatch.setattr(ur, "_inbox_store", lambda req: _Store())
    monkeypatch.setattr(ur, "_license_quota",
                        lambda: {"plan": "basic", "state": "active", "customer": "X",
                                 "seats": 2, "channels": []})
    monkeypatch.setattr(ur, "_pricing", lambda req: None)

    class _Req:
        app = type("A", (), {"state": type("S", (), {})()})()

    out = ur.build_usage_summary(_Req(), span=30)
    assert out["available"] is True
    mq = out["message_quota"]
    assert mq["included"] == 5000 and mq["used"] == 4500
    assert mq["level"] == "warn"   # 4500/5000 = 0.9 ≥ 0.8
