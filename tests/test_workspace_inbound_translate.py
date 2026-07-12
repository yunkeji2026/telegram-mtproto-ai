"""Phase 5-3：入站自动翻译。

P0-3/B8 默认翻转后语义：``enabled`` 未显式配置 = 跟随引擎可用性
（有可用引擎默认开 / **无引擎必须仍关**）；显式 true/false 始终优先。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationResult, TranslationService
from src.inbox.normalizer import message_obj
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
from src.workspace.inbound_translate import (
    enrich_inbound_translations,
    parse_auto_translate_cfg,
    resolve_auto_translate_enabled,
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


class _StubEngine:
    """可用引擎桩：固定回中文译文（让「有引擎默认开」用例真的译出）。"""

    name = "stub"
    available = True

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        return EngineResult("你好", self.name, True)


def _svc_with_engine() -> TranslationService:
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine()])
    return svc


def _req(app=None):
    app = app or FastAPI()
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


@pytest.mark.asyncio
async def test_parse_auto_translate_cfg():
    cfg = parse_auto_translate_cfg(_Cfg())
    assert cfg["enabled"] is True
    assert cfg["target_lang"] == "zh"
    assert "en" in cfg["source_langs"]


def test_parse_cfg_unset_enabled_is_tristate_none():
    """B8：未配置 enabled → None（三态），由 resolve_auto_translate_enabled 收敛。"""
    cfg = parse_auto_translate_cfg(SimpleNamespace(config={}))
    assert cfg["enabled"] is None
    # B9：默认 max_per_thread 收紧到 5（控成本）
    assert cfg["max_per_thread"] == 5


def test_resolve_enabled_tristate():
    """B8 纯函数：显式配置优先；未配置跟随引擎可用性；无引擎必须关。"""
    assert resolve_auto_translate_enabled({"enabled": None}, True) is True
    assert resolve_auto_translate_enabled({"enabled": None}, False) is False
    assert resolve_auto_translate_enabled({"enabled": False}, True) is False   # 显式关不被翻转
    assert resolve_auto_translate_enabled({"enabled": True}, False) is True    # 显式开尊重运营（引擎恢复即生效）


@pytest.mark.asyncio
async def test_enrich_disabled_returns_unchanged():
    """B8 新语义：空 config（未配置）+ **无可用引擎** → 仍关（原「默认关」用例升级为
    「无引擎仍关」硬护栏：否则无引擎环境每次开会话都空跑攒 failed + 前端红徽标）。"""
    msgs = [message_obj(text="hello", direction="in")]
    out, stats = await enrich_inbound_translations(
        _req(), msgs, conversation_id="line:a:1", config_manager=SimpleNamespace(config={}),
        translation_svc=TranslationService(),   # 无 ai_client → 无可用引擎
    )
    assert stats["enabled"] is False
    assert stats["translated"] == 0 and stats["failed"] == 0
    assert out[0]["translated_text"] == "hello"


@pytest.mark.asyncio
async def test_enrich_auto_on_when_engine_available():
    """B8：空 config（未配置）+ 有可用引擎 → 自动开并真的译出。"""
    msgs = [message_obj(text="hello there", direction="in", message_id="m1")]
    out, stats = await enrich_inbound_translations(
        _req(), msgs, conversation_id="line:a:1", config_manager=SimpleNamespace(config={}),
        translation_svc=_svc_with_engine(),
    )
    assert stats["enabled"] is True
    assert stats["translated"] == 1
    assert out[0]["translated_text"] == "你好"


@pytest.mark.asyncio
async def test_enrich_explicit_false_wins_over_engines():
    """B8：显式 enabled:false + 有可用引擎 → 仍关（运营意志优先，不被自动翻转）。"""
    cm = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": False}}})
    msgs = [message_obj(text="hello there", direction="in", message_id="m1")]
    out, stats = await enrich_inbound_translations(
        _req(), msgs, conversation_id="line:a:1", config_manager=cm,
        translation_svc=_svc_with_engine(),
    )
    assert stats["enabled"] is False
    assert stats["translated"] == 0
    assert out[0]["translated_text"] == "hello there"


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


def test_inbound_xlate_daily_roundtrip(tmp_path):
    """P3：入站翻译漏斗按日 record/get 往返 + 全 0 不写 + by_source_lang 合并。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.record_inbound_xlate(translated=2, failed=1, by_lang={"en": 2})
    store.record_inbound_xlate(translated=1, by_lang={"ja": 1})
    store.record_inbound_xlate(translated=0, failed=0)  # 全 0 不写
    s = store.get_inbound_xlate_stats(0)
    assert s["translated"] == 3
    assert s["failed"] == 1
    assert s["by_source_lang"] == {"en": 2, "ja": 1}
    assert len(s["trend"]) == 1
    assert s["trend"][0]["translated"] == 3
    store.close()


@pytest.mark.asyncio
async def test_enrich_records_inbound_funnel(tmp_path):
    """P3：开启入站翻译且挂 store 时，新译出按客户来源语言落入站漏斗。"""
    store = InboxStore(tmp_path / "inbox.db")
    app = FastAPI()
    app.state.inbox_store = store
    app.state.ai_client = MagicMock()
    svc = TranslationService(ai_client=app.state.ai_client)

    async def _fake_chat(prompt, ctx=None):
        return "你好"

    app.state.ai_client.chat = AsyncMock(side_effect=_fake_chat)
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})

    msgs = [message_obj(text="hello there", direction="in", message_id="m1")]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id="line:a:1", config_manager=_Cfg(), translation_svc=svc,
    )
    assert stats["translated"] == 1
    s = store.get_inbound_xlate_stats(0)
    assert s["translated"] == 1
    assert s["by_source_lang"].get("en", 0) == 1
    store.close()


class _LineSvc:
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


def _thread_app(store):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth,
        templates=_Templates(), config_manager=SimpleNamespace(config={"workspace": {}}),
    )
    app.state.line_rpa_services = [_LineSvc()]
    app.state.inbox_store = store
    return app


def test_thread_endpoint_returns_auto_translate_meta(tmp_path):
    """B8 新语义：/thread 的 auto_translate.enabled 不再是「空 config 恒 False」，
    而是引擎可用性——本 app 无 ai_client/无引擎 → False（无引擎仍关的端到端面）。"""
    store = InboxStore(tmp_path / "inbox.db")
    c = TestClient(_thread_app(store))
    r = c.get("/api/unified-inbox/thread?platform=line&account_id=line-a&chat_key=u1")
    assert r.status_code == 200
    data = r.json()
    assert "auto_translate" in data
    assert data["auto_translate"]["enabled"] is False   # 无可用引擎 → 仍关
    store.close()


def test_thread_endpoint_auto_translate_on_with_engine(tmp_path):
    """B8：空 config + 挂了有可用引擎的 TranslationService → /thread 自动开并译出。"""
    store = InboxStore(tmp_path / "inbox.db")
    app = _thread_app(store)
    app.state.translation_service = _svc_with_engine()
    c = TestClient(app)
    r = c.get("/api/unified-inbox/thread?platform=line&account_id=line-a&chat_key=u1")
    assert r.status_code == 200
    data = r.json()
    assert data["auto_translate"]["enabled"] is True
    assert data["auto_translate"]["translated"] >= 1
    store.close()
