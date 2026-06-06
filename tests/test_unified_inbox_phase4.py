"""Phase 4 坐席工作台：快捷回复 / KB 检索 / Contacts 档案富化。"""

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.models import CHANNEL_LINE
from src.contacts.store import ContactStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class _CfgMgr:
    def __init__(self, cfg):
        self.config = cfg

    def get_dynamic_templates_config(self):
        return self.config.get("templates_dyn") or {}


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_chats(self, limit):
        return [{
            "chat_key": "u1", "name": "Line User",
            "last_peer_text": "hello", "last_ts": 100, "unread_count": 1,
        }]

    def status(self):
        return {"running": True, "serial": "s1"}


def _client(*, config_manager=None, contacts=None, kb_store=None):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
        config_manager=config_manager,
    )
    app.state.line_rpa_services = [LineSvc()]
    if contacts is not None:
        app.state.contacts = contacts
    if kb_store is not None:
        app.state.kb_store = kb_store
    return TestClient(app)


def test_templates_merges_workspace_and_messenger():
    cfg = _CfgMgr({
        "workspace": {
            "quick_templates": [{"label": "WS", "text": "工作台话术"}],
        },
        "messenger_rpa": {
            "approval_templates": [{"label": "MS", "text": "Messenger 话术"}],
        },
        "templates_dyn": {
            "greeting": ["动态问候"],
        },
    })
    c = _client(config_manager=cfg)
    resp = c.get("/api/unified-inbox/templates")
    assert resp.status_code == 200
    data = resp.json()
    labels = {t["label"] for t in data["templates"]}
    assert "WS" in labels
    assert "MS" in labels
    assert "greeting" in labels


def test_kb_search_returns_entries():
    class _Kb:
        def search(self, query, top_k=5, lang="zh", query_vec=None):
            return {
                "entries": [{
                    "id": "e1",
                    "title": "退款政策",
                    "example_reply_zh": "7 天内可退",
                    "category": "售后",
                    "_score": 0.9,
                    "_mode": "bm25",
                }],
                "search_mode": "bm25",
            }

    c = _client(kb_store=_Kb())
    resp = c.get("/api/unified-inbox/kb-search?q=退款")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["entries"][0]["title"] == "退款政策"
    assert "7 天内" in data["entries"][0]["answer"]


def test_kb_search_unavailable():
    c = _client(kb_store=None)
    resp = c.get("/api/unified-inbox/kb-search?q=test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_profile_enriched_with_contacts_journey(tmp_path):
    from src.contacts import ContactGateway, GatewayContactHooks, HandoffTokenService, MergeService

    store = ContactStore(tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    hooks = GatewayContactHooks(gw)
    ctx = hooks.on_message(
        channel=CHANNEL_LINE, account_id="line-a", external_id="u1",
        direction="in", text_preview="你好",
    )
    assert ctx is not None
    store.update_journey(ctx.journey.journey_id, intimacy_score=72.5, _touch=False)
    store.update_contact(ctx.contact.contact_id, primary_name="张三")

    contacts = SimpleNamespace(store=store)
    c = _client(contacts=contacts)
    resp = c.get("/api/unified-inbox/profile?platform=line&account_id=line-a&chat_key=u1")
    assert resp.status_code == 200
    prof = resp.json()["profile"]
    assert prof["display_name"] == "张三"
    assert prof["contacts"]["funnel_stage"] == "ENGAGED"
    assert prof["contacts"]["intimacy_score"] == 72.5
    assert "深入互动" in prof["relationship"]["stage"]
    assert any("高亲密" in t for t in prof["tags"])
    store.close()
