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
