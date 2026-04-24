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
    asyncio.get_event_loop().run_until_complete(cm.load())
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
