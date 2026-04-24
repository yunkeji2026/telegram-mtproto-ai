"""Domain Web Isolation Tests — verify non-payment domains render cleanly
without payment-specific UI elements leaking through."""

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient
from src.utils.config_manager import ConfigManager
from src.utils.audit_store import AuditStore
from src.web.admin import create_app


@pytest.fixture()
def general_config_dir(tmp_path):
    """Config directory for general domain (no payment)."""
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "general",
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
    }
    tpl = {"greeting": ["hello"]}
    rates = {"channels": {}}
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "templates.yaml").write_text(
        yaml.dump(tpl, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump(rates, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump({"strategies": {}, "intent_strategy_map": {}}, allow_unicode=True),
        encoding="utf-8",
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    domain_dir = tmp_path.parent / "domains" / "general"
    domain_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "general",
        "display_name": "通用助手",
        "version": "1.0",
        "web": {"routes": False, "pages": [], "dashboard_widgets": []},
    }
    (domain_dir / "manifest.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def general_app(general_config_dir):
    cm = ConfigManager(str(general_config_dir / "config.yaml"))
    asyncio.get_event_loop().run_until_complete(cm.load())
    audit = AuditStore(db_path=general_config_dir / "audit.db")
    return create_app(cm, audit_store=audit, boot_ts=0)


@pytest.fixture()
def general_client(general_app, general_config_dir):
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore
    with TestClient(general_app, raise_server_exceptions=True) as c:
        store = WebUserStore(general_config_dir / "web_users.db")
        if store.user_count() == 0:
            store.create_user("admin", "pass123", ROLE_MASTER)
        c.get("/login")
        c.post("/login", data={"username": "admin", "password": "pass123"},
               follow_redirects=True)
        c.cookies.set("ui_mode", "full")
        c.headers.update({"Authorization": "Bearer test-token-123"})
        yield c


class TestGeneralDomainNoChannelLeak:
    """Verify general domain dashboard/sidebar have no payment-specific elements."""

    def test_dashboard_no_channel_text(self, general_client):
        resp = general_client.get("/")
        assert resp.status_code == 200
        raw = resp.content
        assert "通道健康度".encode("utf-8") not in raw
        assert "管理通道".encode("utf-8") not in raw
        assert "通道数量".encode("utf-8") not in raw

    def test_dashboard_loads(self, general_client):
        resp = general_client.get("/")
        assert resp.status_code == 200

    def test_sidebar_no_channel_link(self, general_client):
        resp = general_client.get("/")
        body = resp.content.decode("utf-8", errors="replace")
        assert 'href="/channels"' not in body

    def test_channels_route_404(self, general_client):
        resp = general_client.get("/channels")
        assert resp.status_code == 404

    def test_api_channels_404(self, general_client):
        resp = general_client.get("/api/channels")
        assert resp.status_code == 404

    def test_config_summary_no_channels(self, general_client):
        resp = general_client.get("/api/config/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "channels" not in data

    def test_system_info_no_channels_count(self, general_client):
        resp = general_client.get("/api/system-info")
        assert resp.status_code == 200
        data = resp.json()
        assert "channels_count" not in data

    def test_knowledge_page_no_conflict_ui(self, general_client):
        resp = general_client.get("/knowledge")
        assert resp.status_code == 200
        raw = resp.content
        assert "通道数据冲突".encode("utf-8") not in raw
        assert b"_CH_DATA_KW" not in raw

    def test_kb_conflict_checker_empty(self, general_app):
        checkers = getattr(general_app.state, "kb_conflict_checkers", [])
        assert len(checkers) == 0

    def test_intent_display_names_no_payment(self, general_app):
        extra = getattr(general_app.state, "intent_display_names_extra", {})
        assert "channel_info" not in extra
        assert "gxp_command" not in extra


class TestWebContextStructure:
    """Verify WebContext dataclass fields."""

    def test_webcontext_fields(self):
        from src.web.web_context import WebContext
        ctx = WebContext(
            config_manager=None,
            audit_store=None,
            event_tracker=None,
            templates=None,
            user_store=None,
            domain_name="test",
        )
        assert ctx.domain_name == "test"
        assert ctx.domain_web_pages == []
        assert ctx.page_auth is None
        assert ctx.api_write_factory is None


class TestPaymentDomainRegistrations:
    """Verify payment domain properly registers its extensions."""

    def test_kb_conflict_checker_registered(self, app):
        checkers = getattr(app.state, "kb_conflict_checkers", [])
        assert len(checkers) >= 1

    def test_intent_names_registered(self, app):
        extra = getattr(app.state, "intent_display_names_extra", {})
        assert "channel_info" in extra
        assert "order_query" in extra

    def test_kb_conflict_detects_channel_data(self, app):
        checkers = app.state.kb_conflict_checkers
        warnings = checkers[0]({"title": "EP通道费率", "category": "通道状态"})
        assert len(warnings) >= 1
        assert any("通道" in w for w in warnings)

    def test_kb_conflict_clean_for_normal_entry(self, app):
        checkers = app.state.kb_conflict_checkers
        warnings = checkers[0]({"title": "问候语", "category": "常规"})
        assert len(warnings) == 0
