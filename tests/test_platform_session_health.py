"""平台会话健康闭环契约（P0-2）：store 迁移语义 + push 端点 + 告警发布。

链路：messenger-web(Node) 在登录/掉线/放弃自愈时 POST
``/api/internal/protocol/session-status`` → ``PlatformSessionHealth`` 登记 →
「进入不健康/恢复」经 EventBus 发 ``platform_session_alert``（告警渠道订阅别名
``platform_session``）→ ops 看板卡片读 ``/api/workspace/metrics.platform_sessions``。

之前 Node 掉线只写自己的日志，Python/运营两眼一抹黑（「会话死了还在装在线」），
本文件把「上报→登记→告警→观测」四步的契约钉死。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_account_routes import register_account_routes


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """每例独立的健康单例 + EventBus（防跨测试污染）。"""
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _store():
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    return get_platform_session_health()


def _client():
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


def _events():
    from src.integrations.shared.event_bus import get_event_bus
    return [e for e in get_event_bus().recent_events(50)
            if e["type"] == "platform_session_alert"]


# ── store 纯语义 ─────────────────────────────────────────────────────────────

def test_store_transition_semantics():
    s = _store()
    t1 = s.record("messenger", "100", "authorized")
    assert t1["changed"] and not t1["went_unhealthy"] and not t1["recovered"]

    t2 = s.record("messenger", "100", "needs_login", detail="cookies expired")
    assert t2["went_unhealthy"] and not t2["recovered"]
    assert s.is_unhealthy("messenger", "100") is True

    # 重复同态：不再触发 went_unhealthy（防告警风暴）
    t3 = s.record("messenger", "100", "needs_login")
    assert not t3["changed"] and not t3["went_unhealthy"]

    # needs_login → expired：仍不健康，但不算「新进入不健康」
    t4 = s.record("messenger", "100", "expired")
    assert t4["changed"] and not t4["went_unhealthy"] and not t4["recovered"]

    t5 = s.record("messenger", "100", "authorized")
    assert t5["recovered"] and s.is_unhealthy("messenger", "100") is False


def test_store_unknown_session_is_healthy():
    assert _store().is_unhealthy("messenger", "nobody") is False


def test_store_dump_and_prom():
    s = _store()
    s.record("messenger", "100", "expired", detail="crash-loop")
    s.record("whatsapp", "wa1", "authorized")
    d = s.dump()
    assert d["total_events"] == 2
    assert d["unhealthy_count"] == 1
    assert "messenger:100" in d["unhealthy"]
    assert d["sessions"]["whatsapp:wa1"]["status"] == "authorized"
    prom = s.dump_prom()
    assert 'platform_session_unhealthy{session="messenger:100"} 1' in prom
    assert 'platform_session_unhealthy{session="whatsapp:wa1"} 0' in prom
    assert 'platform_session_events_total{status="expired"} 1' in prom


def test_store_distinct_key_cap():
    s = _store()
    for i in range(200):
        s.record("messenger", f"acct{i}", "expired")
    assert len(s.dump()["sessions"]) <= 64  # 上限防脏数据撑爆
    assert s.dump()["total_events"] == 200  # 事件计数仍如实累计


# ── push 端点 + 告警发布 ─────────────────────────────────────────────────────

def test_endpoint_records_and_alerts_on_unhealthy():
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "login_id": "msg_x",
        "status": "needs_login", "detail": "cookies expired", "ts": 1780000000,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["went_unhealthy"] is True
    assert _store().is_unhealthy("messenger", "100") is True
    evs = _events()
    assert len(evs) == 1
    assert evs[0]["data"]["status"] == "needs_login"
    assert evs[0]["data"]["recovered"] is False


def test_endpoint_repeat_unhealthy_no_alert_storm():
    c = _client()
    for _ in range(3):
        c.post("/api/internal/protocol/session-status", json={
            "platform": "messenger", "account_id": "100",
            "status": "needs_login",
        })
    assert len(_events()) == 1  # 只在「进入不健康」时发一次


def test_endpoint_recovery_alert():
    c = _client()
    c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "status": "expired",
    })
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "status": "authorized",
        "detail": "connected",
    })
    assert r.json()["recovered"] is True
    evs = _events()
    assert len(evs) == 2
    assert evs[-1]["data"]["recovered"] is True
    assert _store().is_unhealthy("messenger", "100") is False


def test_endpoint_missing_fields_rejected_softly():
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={"status": "x"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    r2 = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger"})
    assert r2.json()["ok"] is False


def test_endpoint_keys_on_login_id_when_account_unknown():
    """restore 早期 accountId 可能还读不到 → 以 login_id 兜底登记，事件不丢。"""
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "login_id": "msg_abc",
        "status": "needs_login",
    })
    assert r.json()["ok"] is True
    assert _store().is_unhealthy("messenger", "msg_abc") is True


# ── 告警文案：platform_session_alert 有专属 _build_message 分支 ──────────────

def test_notifier_message_branch():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "platform_session" in _EVENT_ALIASES
    title, text = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100",
        "status": "needs_login", "detail": "cookies expired",
        "recovered": False,
    })
    assert "messenger" in title and title != "[platform_session_alert] 事件"
    assert "100" in text
    t2, _ = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100", "recovered": True,
    })
    assert "恢复" in t2
