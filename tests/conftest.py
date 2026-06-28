"""pytest 共享 fixtures — Web 管理面板集成测试"""

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

# ─────────────────────────────────────────────────────────
# 配置目录 fixture
# ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _config_data():
    """最小可用的配置文件内容"""
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
    }
    tpl = {
        "greeting": ["hello", "hi", "hey there"],
        "farewell": "goodbye",
        "follow_up": "let me know if you need anything else",
    }
    rates = {
        "channels": {
            "ep": {
                "display_name": "EP通道", "fee_rate": "0.5%", "status": "正常",
                "limits": {"default": "100-20000"}, "names": ["EP"],
            },
            "usdt": {
                "display_name": "USDT通道", "fee_rate": "1.0%", "status": "正常",
                "limits": {"default": "50-10000"}, "names": ["USDT"],
            },
        }
    }
    strategies = {
        "strategies": {
            "standard": {
                "temperature": 0.7, "max_tokens": 800,
                "context_rounds": 3, "enabled": True,
            }
        },
        "intent_strategy_map": {"default": "standard"},
    }
    return {"cfg": cfg, "tpl": tpl, "rates": rates, "strategies": strategies}


@pytest.fixture()
def config_dir(tmp_path, _config_data):
    d = _config_data
    (tmp_path / "config.yaml").write_text(
        yaml.dump(d["cfg"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "templates.yaml").write_text(
        yaml.dump(d["tpl"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump(d["rates"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump(d["strategies"], allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    # Create minimal domain pack for payment to enable domain web routes in tests
    domain_dir = tmp_path.parent / "domains" / "payment" / "web"
    domain_tpl_dir = domain_dir / "templates"
    domain_dir.mkdir(parents=True, exist_ok=True)
    domain_tpl_dir.mkdir(exist_ok=True)
    _real_ch_tpl = Path(__file__).resolve().parent.parent / "domains" / "payment" / "web" / "templates" / "channels.html"
    if _real_ch_tpl.exists():
        import shutil
        shutil.copy2(str(_real_ch_tpl), str(domain_tpl_dir / "channels.html"))
    manifest = {
        "name": "payment",
        "display_name": "支付通道客服",
        "version": "1.0",
        "web": {
            "routes": True,
            "pages": [
                {"key": "ch", "path": "/channels", "label": "通道管理",
                 "label_simple": "通道状态", "icon": "globe",
                 "section": "ops", "show_in_simple": True,
                 "roles": ["master", "admin", "viewer"],
                 "cmd_keys": "channels 通道 状态 管理"},
            ],
            "dashboard_widgets": [
                {"key": "channel_health", "section": "pro-only"},
            ],
        },
    }
    (domain_dir.parent / "manifest.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def config_manager(config_dir):
    cm = ConfigManager(str(config_dir / "config.yaml"))
    asyncio.run(cm.load())
    return cm


@pytest.fixture()
def audit_store(config_dir):
    return AuditStore(db_path=config_dir / "audit.db")


# ─────────────────────────────────────────────────────────
# App & Client fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture()
def app(config_manager, audit_store):
    return create_app(
        config_manager,
        audit_store=audit_store,
        boot_ts=0,
        telegram_client=None,
        event_tracker=None,
        log_buffer=None,
    )


@pytest.fixture()
def client(app):
    """未认证的测试客户端"""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    # P22-B: clean rate-limit state after the test
    reset_fn = getattr(app.state, "intent_tags_rate_limit_reset", None)
    if callable(reset_fn):
        try: reset_fn()
        except Exception: pass


@pytest.fixture()
def auth_client(client, config_dir):
    """
    已认证为 master：
    - 无用户时先创建 admin（与 change-password 等测试对齐）
    - 优先用户名/密码登录；若仍停留在 /login 则回退 legacy auth_token
    - 为 JSON API 设置 Bearer（与 web_admin.auth_token 一致），绕过 CSRF 对 application/json 的拦截
    """
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore

    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("admin", "test-token-123", ROLE_MASTER)

    client.get("/login")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "test-token-123"},
        follow_redirects=True,
    )
    if "/login" in str(getattr(r, "url", "")):
        client.post(
            "/login",
            data={"auth_token": "test-token-123"},
            follow_redirects=True,
        )
    # JSON API 的 CSRF：Bearer 与 web_admin.auth_token 一致
    client.headers.update({"Authorization": "Bearer test-token-123"})
    return client


# ─────────────────────────────────────────────────────────
# Contacts integration fixtures (Phase 1+ e2e 复用)
# ─────────────────────────────────────────────────────────

@pytest.fixture()
def contacts_store(tmp_path):
    """真 SQLite ContactStore（每测试独立 db）。"""
    from src.contacts.store import ContactStore
    db_path = tmp_path / "contacts_e2e.db"
    store = ContactStore(str(db_path))
    yield store
    try:
        store.close()
    except Exception:
        pass


@pytest.fixture()
def contacts_gateway(contacts_store):
    """真 ContactGateway（含 HandoffTokenService + MergeService）。"""
    from src.contacts.gateway import ContactGateway
    from src.contacts.handoff import HandoffTokenService
    from src.contacts.merge import MergeService
    return ContactGateway(
        contacts_store,
        HandoffTokenService(contacts_store, ttl_seconds=3600),
        MergeService(contacts_store),
    )


@pytest.fixture()
def contacts_hooks(contacts_gateway):
    """真 GatewayContactHooks。"""
    from src.contacts.rpa_hooks import GatewayContactHooks
    return GatewayContactHooks(contacts_gateway)


@pytest.fixture()
def mock_ai_client_ja():
    """mock AIClient.chat 默认返日文 portrait JSON（可在测试中 override）。"""
    from unittest.mock import AsyncMock, MagicMock
    ai = MagicMock()
    ai.chat = AsyncMock(return_value=(
        '{"language":"ja","tone":"casual_friendly",'
        '"interests":["旅行","料理"],"recent_topics":["週末の予定"],'
        '"key_facts":["日本在住"],"intimacy_signal":"warming"}'
    ))
    return ai


# ─────────────────────────────────────────────────────────
# P20-D: 自动重置 intent_tags 编辑滑窗（防测试间污染）
# ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_intent_tags_edit_window():
    """每个 test 前后清 rpa_shared._INTENT_TAGS_EDIT_WINDOW + counter，
    避免持久化 sidecar 跨测试漏数。仅在 rpa_shared 已加载时生效。

    P21-D: 关闭持久化防抖（测试中每次写都要触发 sidecar 更新）。
    """
    try:
        from src.integrations import rpa_shared as _shr
        _shr.reset_intent_tags_edit_window()
        # P21-D: tests run faster than 1s throttle would tolerate
        _shr._STATS_SAVE_MIN_INTERVAL_SEC = 0.0
        _shr._stats_last_save_ts = 0.0
    except Exception:
        pass
    yield
    try:
        from src.integrations import rpa_shared as _shr
        _shr.reset_intent_tags_edit_window()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_metrics_store_singleton():
    """每个 test 前后重置全局 MetricsStore 单例，从根上隔离指标串测。

    MetricsStore 是进程内单例（_instance）。「写指标」的测试（draft 事件 /
    safe_skip / deferred 队列 / startup advisory 等）若不清理，会经 xdist 同
    worker 泄漏到「读指标」的测试（HealthWatchdog._check_draft_quality、
    ops incidents、Prometheus 导出等），表现为「本地 worker 分布恰好不串 →
    绿，CI 分布串 → 红」的偶发 flaky（曾导致 #74 的 test_ops_incidents）。

    集中在 conftest 做 autouse 重置后，所有读单例的测试对任何泄漏免疫，无需
    再逐文件加隔离 fixture（与上方 _reset_intent_tags_edit_window 同模式）。
    各测试若需取「干净的 store 引用」，仍可在用例内显式重置并 get_metrics_store()。
    """
    try:
        from src.monitoring import metrics_store as _ms
        _ms.MetricsStore._instance = None
    except Exception:
        pass
    yield
    try:
        from src.monitoring import metrics_store as _ms
        _ms.MetricsStore._instance = None
    except Exception:
        pass


@pytest.fixture()
def viewer_client(app, config_dir):
    """已认证为 viewer 角色的客户端"""
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": "Bearer test-token-123"})
        # 先用 master 登录创建 viewer 用户
        c.post("/login", data={"auth_token": "test-token-123"}, follow_redirects=True)
        c.post(
            "/users/create",
            data={"username": "testviewer", "password": "viewer123", "role": "viewer"},
            follow_redirects=True,
        )
        c.get("/logout", follow_redirects=True)
        # 用 viewer 登录
        c.post(
            "/login",
            data={"username": "testviewer", "password": "viewer123"},
            follow_redirects=True,
        )
        yield c
