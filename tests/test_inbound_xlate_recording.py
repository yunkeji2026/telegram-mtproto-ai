"""P4-B：入站显示翻译（客户→坐席）成本按日聚合——/api/unified-inbox/translate 记账。

只有 ``purpose=inbound_display`` 的调用计入 inbound_xlate_daily（命中缓存不计），
确保「常驻双语」功能的真实 API 成本在经理看板（/api/workspace/dashboard）可见，
且不与出向漏斗（send 路径已记）双计。
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class FakeAI:
    async def chat(self, prompt, context=None):
        return "你好朋友"


def _client(tmp_path):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth, templates=_Templates(),
    )
    app.state.ai_client = FakeAI()
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app)


def test_inbound_display_translate_records_funnel(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/unified-inbox/translate",
               json={"text": "hello friend", "target_lang": "zh", "purpose": "inbound_display"})
    assert r.status_code == 200 and r.json()["ok"] is True
    s = c.app.state.inbox_store.get_inbound_xlate_stats(0)
    assert s["translated"] >= 1


def test_translate_without_purpose_is_not_recorded(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/unified-inbox/translate", json={"text": "hello friend", "target_lang": "zh"})
    assert r.status_code == 200 and r.json()["ok"] is True
    s = c.app.state.inbox_store.get_inbound_xlate_stats(0)
    assert s["translated"] == 0
    assert s["failed"] == 0
