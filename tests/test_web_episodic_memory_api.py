"""Web：/api/bot-metrics 记忆字段、/api/episodic-memory/backfill 行为。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump(
            {
                "channels": {
                    "ep": {
                        "display_name": "EP",
                        "fee_rate": "0.5%",
                        "status": "正常",
                        "limits": {"default": "1-100"},
                        "names": ["EP"],
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
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
def episodic_app_and_client(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_backfill_embeddings = AsyncMock(
        return_value={"ok": True, "processed": 2, "updated": 2}
    )
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        yield client, sm


def test_bot_metrics_includes_memory(auth_client):
    r = auth_client.get("/api/bot-metrics")
    assert r.status_code == 200
    d = r.json()
    assert "memory" in d
    assert isinstance(d["memory"], dict)


def test_bot_metrics_includes_startup_advisories(auth_client):
    from src.monitoring.metrics_store import get_metrics_store

    get_metrics_store().set_startup_advisory_counts(3, 1)
    get_metrics_store().set_startup_advisory_audit_logged(1)
    r = auth_client.get("/api/bot-metrics")
    assert r.status_code == 200
    sa = r.json().get("startup_advisories") or {}
    assert sa.get("total") == 3
    assert sa.get("warnings") == 1
    assert sa.get("audit_logged_warnings") == 1


def test_episodic_backfill_503_without_telegram_bot(auth_client):
    r = auth_client.post("/api/episodic-memory/backfill?limit=5")
    assert r.status_code == 503


def test_episodic_list_passes_source_filter(episodic_app_and_client):
    """R13：?source= 透传给 episodic_list_for_admin。"""
    client, sm = episodic_app_and_client
    sm.episodic_list_for_admin = MagicMock(return_value=[
        {"id": 1, "memory_key": "u1", "content": "x", "category": "llm",
         "created_at": 0, "has_embedding": False,
         "source": "ai_inferred", "tier": "raw", "hits": 1},
    ])
    r = client.get("/api/episodic-memory?source=ai_inferred&limit=10")
    assert r.status_code == 200
    items = r.json().get("items") or []
    assert items and items[0]["source"] == "ai_inferred"
    sm.episodic_list_for_admin.assert_called_once_with(
        prefix="", limit=10, source="ai_inferred"
    )


def test_episodic_list_invalid_source_ignored(episodic_app_and_client):
    client, sm = episodic_app_and_client
    sm.episodic_list_for_admin = MagicMock(return_value=[])
    r = client.get("/api/episodic-memory?source=garbage")
    assert r.status_code == 200
    sm.episodic_list_for_admin.assert_called_once_with(
        prefix="", limit=100, source=""
    )


def test_episodic_confirm_calls_skill_manager(episodic_app_and_client):
    """R15：POST /api/episodic-memory/{id}/confirm → episodic_confirm_for_admin。"""
    client, sm = episodic_app_and_client
    sm.episodic_confirm_for_admin = MagicMock(return_value=True)
    r = client.post("/api/episodic-memory/42/confirm")
    assert r.status_code == 200
    assert r.json().get("confirmed") == 42
    sm.episodic_confirm_for_admin.assert_called_once_with(42)


def test_episodic_confirm_404_when_not_inferred(episodic_app_and_client):
    client, sm = episodic_app_and_client
    sm.episodic_confirm_for_admin = MagicMock(return_value=False)
    r = client.post("/api/episodic-memory/7/confirm")
    assert r.status_code == 404


def test_episodic_confirm_503_without_bot(auth_client):
    r = auth_client.post("/api/episodic-memory/1/confirm")
    assert r.status_code == 503


def test_episodic_confirm_writes_audit(tmp_path):
    """R16：确认成功后落审计（action=episodic_confirm_inferred + content）。"""
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_confirm_for_admin = MagicMock(return_value="用户可能是工程师")
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.post("/api/episodic-memory/55/confirm")
    assert r.status_code == 200
    rows = audit.query(limit=10, action="episodic_confirm_inferred")
    assert rows and rows[0]["target"] == "55"
    assert "工程师" in rows[0]["new_val"]


def test_episodic_correction_stats_aggregates(tmp_path):
    """R17：correction-stats 聚合审计确认量 + 库内待确认 + 采纳率。"""
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    # 3 条确认审计（2 人）
    audit.log("alice", "episodic_confirm_inferred", target="1", new_val="事实甲")
    audit.log("alice", "episodic_confirm_inferred", target="2", new_val="事实乙")
    audit.log("bob", "episodic_confirm_inferred", target="3", new_val="事实丙")
    audit.log("alice", "other_action", target="9", new_val="无关")  # 不计
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_inferred_counts = MagicMock(return_value={"pending": 1, "total": 10})
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.get("/api/episodic-memory/correction-stats?days=30")
    assert r.status_code == 200
    d = r.json()
    assert d["confirmed"] == 3
    assert d["pending_inferred"] == 1
    assert d["total_inferred"] == 10
    # 采纳率 = 3 / (3 + 1) = 0.75
    assert abs(d["adoption_rate"] - 0.75) < 1e-6
    actors = {a["actor"]: a["count"] for a in d["by_actor"]}
    assert actors == {"alice": 2, "bob": 1}
    assert len(d["recent"]) == 3


def test_episodic_correction_stats_empty(episodic_app_and_client):
    client, sm = episodic_app_and_client
    sm.episodic_inferred_counts = MagicMock(return_value={"pending": 0, "total": 0})
    r = client.get("/api/episodic-memory/correction-stats")
    assert r.status_code == 200
    d = r.json()
    assert d["confirmed"] == 0
    assert d["adoption_rate"] == 0.0
    assert d["by_actor"] == []


def test_episodic_backfill_calls_skill_manager(episodic_app_and_client):
    client, sm = episodic_app_and_client
    r = client.post("/api/episodic-memory/backfill?limit=8&prefix=abc")
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert r.json().get("updated") == 2
    sm.episodic_backfill_embeddings.assert_awaited_once_with(
        8, memory_key_prefix="abc"
    )


def test_episodic_backfill_400_vector_disabled(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_backfill_embeddings = AsyncMock(
        return_value={"ok": False, "error": "vector_disabled"}
    )
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.post("/api/episodic-memory/backfill")
    assert r.status_code == 400
    assert "向量" in str(r.json().get("detail", ""))


def test_episodic_backfill_429_budget(tmp_path):
    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_backfill_embeddings = AsyncMock(
        return_value={
            "ok": False,
            "error": "daily_embed_budget_exceeded",
            "budget_remaining": 0,
        }
    )
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.post("/api/episodic-memory/backfill")
    assert r.status_code == 429
