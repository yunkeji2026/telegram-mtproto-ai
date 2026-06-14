"""锁定 /api/drafts 外层鉴权接线。

历史缺陷：main.py 的 _drafts_api_auth 依赖 web_app.state.require_role，但该属性
从未被挂载 → hasattr 恒 False → 外层鉴权空操作（坐席端点对未登录请求也放行）。

本测试复刻 main.py 的真实接线（create_app → 暴露 state.api_auth → drafts 子路由经其鉴权），
验证：
  1. create_app 暴露可调用的 state.api_auth / state.require_role；
  2. 未携带凭据的 /api/drafts 请求被 401 挡下（修复前会是 200）；
  3. 携带 Bearer token 的请求正常放行。
"""

import asyncio

import pytest
import yaml
from fastapi import Request
from fastapi.testclient import TestClient

from src.web.admin import create_app
from src.utils.config_manager import ConfigManager
from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.web.routes.drafts_routes import register_drafts_routes


class _LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_pending(self, *, status=None, limit=50):
        return [{
            "id": 11, "chat_key": "lk", "chat_name": "U", "peer_text": "hi",
            "draft_reply": "你好", "status": status or "pending", "ts": 100,
        }]

    def resolve_pending(self, pending_id, *, action, final_reply=None, by=""):
        return {"id": pending_id, "status": "approved"}


@pytest.fixture
def app(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(yaml.dump({"channels": {}}), encoding="utf-8")

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    application = create_app(cm)

    # 复刻 main.py 的 drafts 接线：经 state.api_auth 鉴权
    def _drafts_api_auth(request: Request):
        fn = getattr(application.state, "api_auth", None)
        if fn is not None:
            fn(request)

    register_drafts_routes(application, api_auth=_drafts_api_auth)
    store = InboxStore(":memory:")
    application.state.draft_service = DraftService(inbox_store=store, line_services=[_LineSvc()])
    try:
        yield application
    finally:
        store.close()


def test_create_app_exposes_callable_api_auth(app):
    assert callable(getattr(app.state, "api_auth", None))
    assert callable(getattr(app.state, "require_role", None))


def test_drafts_list_rejects_unauthenticated(app):
    c = TestClient(app)
    r = c.get("/api/drafts?status=pending&limit=10")
    # 修复前（外层空操作）此处会是 200；修复后必须被鉴权挡下。
    assert r.status_code == 401


def test_drafts_list_allows_bearer_token(app):
    c = TestClient(app)
    r = c.get(
        "/api/drafts?status=pending&limit=10",
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
