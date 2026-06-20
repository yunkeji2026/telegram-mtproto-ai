"""F+：会话级首选翻译引擎持久化单测。

覆盖：
- store：set_conversation_pref_engine 落库 + get_conversation 暴露 + 清除 + 不存在会话回 False；
- TranslationService.translate(engine=...)：指定可用引擎强制走它 / 失败回落 failover /
  引擎不可用回落 failover / 缓存按引擎分桶；
- 解析器 _resolve_conv_engine：读出会话偏好。
"""
import pytest

from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService


class _StubEngine:
    def __init__(self, name, *, available=True, out=None, ok=True):
        self.name = name
        self._available = available
        self._out = out if out is not None else f"[{name}]"
        self._ok = ok

    @property
    def available(self):
        return self._available

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        if not self._ok:
            return EngineResult("", self.name, False, error="boom")
        return EngineResult(self._out, self.name, True)


# ── store ────────────────────────────────────────────────────────────────────

def _new_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(str(tmp_path / "inbox.db"))


def _seed_conv(store):
    from src.inbox.models import InboxConversation
    from src.inbox.normalizer import conv_id
    cid = conv_id("line", "default", "line:user:U1")
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="default",
        chat_key="line:user:U1", display_name="U1", language="ja",
    ))
    return cid


def test_store_set_and_get_pref_engine(tmp_path):
    store = _new_store(tmp_path)
    cid = _seed_conv(store)
    assert store.set_conversation_pref_engine(cid, "DeepL") is True
    conv = store.get_conversation(cid)
    assert conv["pref_engine"] == "deepl"  # 归一小写


def test_store_clear_pref_engine(tmp_path):
    store = _new_store(tmp_path)
    cid = _seed_conv(store)
    store.set_conversation_pref_engine(cid, "deepl")
    assert store.set_conversation_pref_engine(cid, "") is True
    assert store.get_conversation(cid)["pref_engine"] == ""


def test_store_pref_engine_missing_conversation(tmp_path):
    store = _new_store(tmp_path)
    assert store.set_conversation_pref_engine("nope", "deepl") is False


# ── TranslationService.translate(engine=...) ─────────────────────────────────

async def test_translate_uses_preferred_engine():
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("deepl", out="DL")])
    res = await svc.translate("hi", target_lang="zh", source_lang="en", engine="deepl")
    assert res.ok and res.provider == "deepl" and res.translated_text == "DL"


async def test_translate_no_engine_uses_failover_primary():
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("deepl", out="DL")])
    res = await svc.translate("hi", target_lang="zh", source_lang="en")
    assert res.ok and res.provider == "ai"  # 无偏好 → 主引擎


async def test_translate_pref_engine_failure_falls_back():
    # 首选引擎返回失败 → 回落 failover（仍出译文，provider 为兜底引擎）
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("deepl", ok=False)])
    res = await svc.translate("hi", target_lang="zh", source_lang="en", engine="deepl")
    assert res.ok and res.provider == "ai"


async def test_translate_pref_engine_unavailable_falls_back():
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("deepl", available=False)])
    res = await svc.translate("hi", target_lang="zh", source_lang="en", engine="deepl")
    assert res.ok and res.provider == "ai"


async def test_translate_cache_keyed_per_engine():
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="AI"), _StubEngine("deepl", out="DL")])
    r1 = await svc.translate("hi", target_lang="zh", source_lang="en", engine="deepl")
    r2 = await svc.translate("hi", target_lang="zh", source_lang="en")  # 无偏好 → 不同桶
    assert r1.translated_text == "DL"
    assert r2.translated_text == "AI"


# ── 解析器 ────────────────────────────────────────────────────────────────────

def test_resolve_conv_engine(tmp_path):
    from types import SimpleNamespace
    from src.web.routes.unified_inbox_services import _resolve_conv_engine

    store = _new_store(tmp_path)
    cid = _seed_conv(store)
    store.set_conversation_pref_engine(cid, "deepl")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    got = _resolve_conv_engine(request, "line", "default", "line:user:U1")
    assert got == "deepl"


def test_resolve_conv_engine_empty_when_unset(tmp_path):
    from types import SimpleNamespace
    from src.web.routes.unified_inbox_services import _resolve_conv_engine

    store = _new_store(tmp_path)
    _seed_conv(store)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    assert _resolve_conv_engine(request, "line", "default", "line:user:U1") == ""


# ── F+2：GET 端点（前端切会话取徽标） ────────────────────────────────────────

def _app_with_store(store):
    from fastapi import FastAPI, Request
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.inbox_store = store
    return app


def test_get_conv_engine_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    store = _new_store(tmp_path)
    cid = _seed_conv(store)
    store.set_conversation_pref_engine(cid, "deepl")
    client = TestClient(_app_with_store(store))
    r = client.get("/api/unified-inbox/conv-engine",
                   params={"platform": "line", "account_id": "default",
                           "chat_key": "line:user:U1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["pref_engine"] == "deepl"


def test_get_conv_engine_missing_params(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_app_with_store(_new_store(tmp_path)))
    r = client.get("/api/unified-inbox/conv-engine", params={"platform": "line"})
    assert r.status_code == 200 and r.json()["pref_engine"] == ""
