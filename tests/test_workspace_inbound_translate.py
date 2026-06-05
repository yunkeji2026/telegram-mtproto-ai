"""Phase 5-3：入站自动翻译。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.translation_service import TranslationResult, TranslationService
from src.inbox.normalizer import message_obj
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
from src.workspace.inbound_translate import (
    enrich_inbound_translations,
    parse_auto_translate_cfg,
)


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page not used")


class _Cfg:
    config = {
        "workspace": {
            "auto_translate_inbound": {
                "enabled": True,
                "target_lang": "zh",
                "max_per_thread": 5,
                "source_langs": ["en"],
            },
        },
    }


@pytest.mark.asyncio
async def test_parse_auto_translate_cfg():
    cfg = parse_auto_translate_cfg(_Cfg())
    assert cfg["enabled"] is True
    assert cfg["target_lang"] == "zh"
    assert "en" in cfg["source_langs"]


@pytest.mark.asyncio
async def test_enrich_disabled_returns_unchanged():
    app = FastAPI()
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})

    msgs = [message_obj(text="hello", direction="in")]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id="line:a:1", config_manager=SimpleNamespace(config={}),
        translation_svc=TranslationService(),
    )
    assert stats["enabled"] is False
    assert out[0]["translated_text"] == "hello"


@pytest.mark.asyncio
async def test_enrich_translates_inbound_en():
    app = FastAPI()
    app.state.ai_client = MagicMock()
    svc = TranslationService(ai_client=app.state.ai_client)

    async def _fake_chat(prompt, ctx=None):
        return "你好"

    app.state.ai_client.chat = AsyncMock(side_effect=_fake_chat)

    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})

    msgs = [
        message_obj(text="hello there", direction="in", message_id="m1"),
        message_obj(text="こんにちは", direction="in", message_id="m2"),
    ]
    msgs[1]["language"] = "ja"  # 不在 source_langs [en]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id="line:a:1", config_manager=_Cfg(), translation_svc=svc,
    )
    assert stats["translated"] == 1
    assert out[0]["translated_text"] == "你好"
    assert out[1]["translated_text"] == "こんにちは"


@pytest.mark.asyncio
async def test_store_overlay_before_api_call(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    from src.inbox.models import InboxConversation, InboxMessage

    cid = "line:default:u1"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="default", chat_key="u1",
        display_name="U", last_text="hello", last_ts=100,
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m1", direction="in",
        text="hello", original_text="hello", translated_text="你好",
        source_lang="en", target_lang="zh", ts=100,
    ))
    store.close()

    store2 = InboxStore(tmp_path / "inbox.db")
    app = FastAPI()
    app.state.inbox_store = store2
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})

    msgs = [message_obj(text="hello", direction="in", message_id="line:default:u1:m1", ts=100)]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=_Cfg(),
        translation_svc=TranslationService(),  # 不应被调用
    )
    assert stats["from_store"] == 1
    assert stats["translated"] == 0
    assert out[0]["translated_text"] == "你好"
    store2.close()


def test_thread_endpoint_returns_auto_translate_meta(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")

    class LineSvc:
        account_id = "line-a"
        _merged_cfg = {"label": "L"}

        def list_chats(self, limit):
            return [{
                "chat_key": "u1", "name": "User",
                "last_peer_text": "hello", "last_ts": 100, "unread_count": 0,
                "messages": [{"text": "hello", "ts": 100, "direction": "in", "message_id": "x1"}],
            }]

        def status(self):
            return {"running": True}

    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth,
        templates=_Templates(), config_manager=SimpleNamespace(config={"workspace": {}}),
    )
    app.state.line_rpa_services = [LineSvc()]
    app.state.inbox_store = store
    c = TestClient(app)
    r = c.get("/api/unified-inbox/thread?platform=line&account_id=line-a&chat_key=u1")
    assert r.status_code == 200
    data = r.json()
    assert "auto_translate" in data
    assert data["auto_translate"]["enabled"] is False
    store.close()
