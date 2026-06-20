"""R9b Web：/api/crisis-events 列表 + /handle 标记处置（鉴权 + 接线）。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app


async def _load_cm(tmp_path: Path) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump(
            {
                "strategies": {
                    "standard": {
                        "temperature": 0.7,
                        "max_tokens": 800,
                        "context_rounds": 3,
                        "enabled": True,
                    }
                },
                "intent_strategy_map": {"default": "standard"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def crisis_client(tmp_path):
    from src.utils.crisis_event_store import CrisisEventStore

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    store = CrisisEventStore(tmp_path / "crisis.db")
    store.record(user_id="u1", level="severe", category="self_harm",
                 streak=2, escalated=True, excerpt="我不想活了")
    store.record(user_id="u2", level="elevated", excerpt="好绝望")

    tc = MagicMock()
    sm = MagicMock()
    sm.crisis_list_for_admin.side_effect = lambda **kw: store.list_recent(
        limit=kw.get("limit", 50),
        only_unhandled=kw.get("only_unhandled", False),
        user_prefix=kw.get("user_prefix", ""),
    )
    sm.crisis_count_for_admin.side_effect = lambda **kw: store.count(
        only_unhandled=kw.get("only_unhandled", False)
    )
    sm.crisis_mark_handled_for_admin.side_effect = lambda eid, **kw: store.mark_handled(
        eid, handled_by=kw.get("handled_by", ""), note=kw.get("note", "")
    )
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        from src.utils.web_user_store import ROLE_MASTER, WebUserStore

        wstore = WebUserStore(tmp_path / "web_users.db")
        if wstore.user_count() == 0:
            wstore.create_user("admin", "test-token-123", ROLE_MASTER)
        client.get("/login")
        client.post(
            "/login",
            data={"username": "admin", "password": "test-token-123"},
            follow_redirects=True,
        )
        client.headers.update({"Authorization": "Bearer test-token-123"})
        yield client, store


def test_list_crisis_events(crisis_client):
    client, _ = crisis_client
    r = client.get("/api/crisis-events")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["count"] == 2
    assert d["unhandled_total"] == 2


def test_list_only_unhandled_filter(crisis_client):
    client, store = crisis_client
    # 处理掉一条
    first_id = store.list_recent()[-1]["id"]
    store.mark_handled(first_id, handled_by="x")
    r = client.get("/api/crisis-events?only_unhandled=true")
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_handle_marks_event(crisis_client):
    client, store = crisis_client
    eid = store.list_recent()[0]["id"]
    r = client.post(f"/api/crisis-events/{eid}/handle", json={"note": "已电话联系"})
    assert r.status_code == 200
    assert r.json()["handled"] == eid
    row = [x for x in store.list_recent() if x["id"] == eid][0]
    assert row["handled"] is True
    assert row["note"] == "已电话联系"


def test_handle_missing_event_404(crisis_client):
    client, _ = crisis_client
    r = client.post("/api/crisis-events/99999/handle", json={})
    assert r.status_code == 404


def test_list_requires_auth(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=None)
    with TestClient(app, raise_server_exceptions=True) as client:
        r = client.get("/api/crisis-events", headers={"Authorization": "Bearer wrong"})
        assert r.status_code in (401, 403)


def test_crisis_audit_page_loads(crisis_client):
    client, _ = crisis_client
    r = client.get("/crisis-audit")
    assert r.status_code == 200
    assert 'id="ca-body"' in r.text
    assert "/api/crisis-events" in r.text


def test_alert_status_crisis_unhandled(crisis_client):
    client, _ = crisis_client
    r = client.get("/api/alert-status")
    assert r.status_code == 200
    alerts = r.json().get("alerts") or []
    crisis = [a for a in alerts if a.get("type") == "crisis"]
    assert len(crisis) == 1
    assert crisis[0]["level"] == "critical"
    assert "/crisis-audit" in crisis[0].get("action_url", "")
