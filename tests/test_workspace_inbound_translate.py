"""Phase 5-3：入站自动翻译。

P0-3/B8 默认翻转后语义：``enabled`` 未显式配置 = 跟随引擎可用性
（有可用引擎默认开 / **无引擎必须仍关**）；显式 true/false 始终优先。

2026-07 性能根治（/thread 每次 6 秒）新增回归：
- noop 标记：译文==原文（emoji/不可译）也写 store 目标语标记，重开不再重译；
- 失败负缓存：翻译失败 TTL 内不重试（防引擎宕机被 5s 轮询打满）;
- 同步预算：仅最新 N 条同步译，其余交后台任务写库（/thread 即时返回）；
- ingest 净化：message_obj 预填的 translated==text 占位不落库；
- live 主键修正：live 裸 message_id 经 overlay 携带 store 真主键，译文可持久化。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.workspace.inbound_translate as IT
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


@pytest.fixture(autouse=True)
def _clear_xlate_runtime_state():
    """清模块级运行态（失败负缓存 / 后台 in-flight），防跨用例串味。"""
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()
    yield
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()


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


def test_inbound_xlate_daily_noop_deferred_columns(tmp_path):
    """2026-07 扩列：noop/deferred 计入按日表（insert + upsert 两路径），trend 行携带。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.record_inbound_xlate(noop=2, deferred=5)             # 全靠新列也应落行
    store.record_inbound_xlate(translated=1, noop=1, deferred=3, by_lang={"vi": 1})
    s = store.get_inbound_xlate_stats(0)
    assert s["noop"] == 3 and s["deferred"] == 8
    assert s["translated"] == 1
    assert s["trend"][0]["deferred"] == 8 and s["trend"][0]["failed"] == 0
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


def _app_with_store(store):
    app = FastAPI()
    app.state.inbox_store = store
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


def _ingest_inbound(store, cid, pmid, text, lang="en", ts=100.0):
    from src.inbox.models import InboxConversation, InboxMessage
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="default",
        chat_key=cid.split(":")[-1], display_name="U", last_text=text, last_ts=ts,
    ))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id=pmid, direction="in",
        text=text, original_text=text, source_lang=lang, ts=ts,
    ))


@pytest.mark.asyncio
async def test_untranslatable_marked_noop_and_not_retried(tmp_path):
    """译文==原文（emoji/不可译）→ 写目标语标记；重开会话不再重译（svc 零调用）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:u1"
    _ingest_inbound(store, cid, "m1", "😭😭😭😭", lang="unknown")
    req = _app_with_store(store)

    calls = {"n": 0}

    class _EchoSvc:
        async def translate(self, text, **kw):
            calls["n"] += 1
            return TranslationResult(text, text, "unknown", "zh", True, provider="ai")

    cfg = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})
    mid = f"{cid}:m1"
    msgs = [message_obj(text="😭😭😭😭", direction="in", message_id=mid, ts=100.0)]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=cfg, translation_svc=_EchoSvc(),
    )
    assert stats["noop"] == 1 and calls["n"] == 1
    # store 行已带「已处理」标记：translated_text=原文 + target_lang=zh
    row = store.list_messages(cid)[0]
    assert row["translated_text"] == "😭😭😭😭"
    assert row["target_lang"] == "zh"

    # 第二次打开：overlay 识别标记 → 跳过，不再调翻译
    msgs2 = [message_obj(text="😭😭😭😭", direction="in", message_id=mid, ts=100.0)]
    out2, stats2 = await enrich_inbound_translations(
        req, msgs2, conversation_id=cid, config_manager=cfg, translation_svc=_EchoSvc(),
    )
    assert calls["n"] == 1                      # 未再调用
    assert stats2["noop"] == 0 and stats2["translated"] == 0
    store.close()


@pytest.mark.asyncio
async def test_failed_translation_negative_cached(tmp_path):
    """翻译失败 → 负缓存 TTL 内不重试；TTL 过期后恢复重试。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:u2"
    _ingest_inbound(store, cid, "m1", "hello there")
    req = _app_with_store(store)

    calls = {"n": 0}

    class _FailSvc:
        async def translate(self, text, **kw):
            calls["n"] += 1
            return TranslationResult(text, text, "en", "zh", False,
                                     provider="none", error="provider_unavailable")

    cfg = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})
    mid = f"{cid}:m1"

    def _mk():
        return [message_obj(text="hello there", direction="in", message_id=mid, ts=100.0)]

    _, s1 = await enrich_inbound_translations(
        req, _mk(), conversation_id=cid, config_manager=cfg, translation_svc=_FailSvc())
    assert s1["failed"] == 1 and calls["n"] == 1

    _, s2 = await enrich_inbound_translations(
        req, _mk(), conversation_id=cid, config_manager=cfg, translation_svc=_FailSvc())
    assert calls["n"] == 1                       # 冷却中，未重试
    assert s2["failed"] == 0 and s2["skipped"] >= 1

    # 模拟 TTL 过期 → 恢复重试
    IT._FAILED_AT[mid] = time.monotonic() - IT._FAILED_TTL_SEC - 1
    _, s3 = await enrich_inbound_translations(
        req, _mk(), conversation_id=cid, config_manager=cfg, translation_svc=_FailSvc())
    assert calls["n"] == 2
    store.close()


@pytest.mark.asyncio
async def test_sync_budget_defers_rest_to_background(tmp_path):
    """候选超同步预算（2 条）→ 其余交后台任务译完写库，/thread 响应即时返回。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:u3"
    for i in range(5):
        _ingest_inbound(store, cid, f"m{i}", f"hello number {i}", ts=100.0 + i)
    req = _app_with_store(store)

    class _OkSvc:
        async def translate(self, text, **kw):
            return TranslationResult(text, f"译:{text}", "en", "zh", True, provider="ai")

    cfg = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})
    msgs = [
        message_obj(text=f"hello number {i}", direction="in",
                    message_id=f"{cid}:m{i}", ts=100.0 + i)
        for i in range(5)
    ]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=cfg, translation_svc=_OkSvc(),
    )
    assert stats["translated"] == IT._SYNC_MAX_MSGS       # 同步只译预算内（最新 2 条）
    assert stats["deferred"] == 5 - IT._SYNC_MAX_MSGS     # 其余交后台

    # 等后台任务收尾（会话级 in-flight 锁释放即完成）
    for _ in range(200):
        if cid not in IT._BG_CONVS:
            break
        await asyncio.sleep(0.01)
    rows = store.list_messages(cid)
    translated = [r for r in rows if r["translated_text"].startswith("译:")]
    assert len(translated) == 5                            # 后台补齐全部写库
    store.close()


@pytest.mark.asyncio
async def test_live_message_translation_persists_via_store_mid(tmp_path):
    """live 聚合消息（裸平台 message_id）：overlay 按 text+ts 命中 store 行携带真主键，
    译文写进 store（此前主键不匹配 → 写库静默 no-op → 每次重译）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:u4"
    _ingest_inbound(store, cid, "pm9", "good morning", ts=123.0)
    req = _app_with_store(store)

    class _OkSvc:
        async def translate(self, text, **kw):
            return TranslationResult(text, "早上好", "en", "zh", True, provider="ai")

    cfg = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})
    # live 路径：message_id 是裸平台 id（非 store 主键），ts 为 int（store 是 REAL）
    msgs = [message_obj(text="good morning", direction="in", message_id="pm9", ts=123)]
    out, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=cfg, translation_svc=_OkSvc(),
    )
    assert stats["translated"] == 1
    row = store.list_messages(cid)[0]
    assert row["translated_text"] == "早上好"              # 写进了 store 真主键行
    assert row["target_lang"] == "zh"
    # 内部标注不泄漏进响应
    assert "_store_mid" not in out[0] and "_xlate_attempted" not in out[0]
    store.close()


@pytest.mark.asyncio
async def test_unknown_label_zh_text_not_translated(tmp_path):
    """语言标签 'unknown'（protocol push 未带 language）的中文消息：按正文重检 → 跳过。

    此前 unknown 标签直接送译「译成中文」，LLM 对同语输入自由发挥出闲聊句
    （「你这个是中文」→「嗯嗯，是的呀～」）被当译文写库，污染前端双行显示。
    """
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:u6"
    _ingest_inbound(store, cid, "m1", "你这个是中文", lang="unknown")
    req = _app_with_store(store)

    calls = {"n": 0}

    class _ChattySvc:
        async def translate(self, text, **kw):
            calls["n"] += 1
            return TranslationResult(text, "嗯嗯，是的呀～", "unknown", "zh", True, provider="ai")

    cfg = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})
    msgs = [message_obj(text="你这个是中文", direction="in", message_id=f"{cid}:m1", ts=100.0)]
    msgs[0]["language"] = "unknown"     # 模拟 store 行 language 标签
    _, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=cfg, translation_svc=_ChattySvc(),
    )
    assert calls["n"] == 0              # 正文检出 zh → 未送译
    assert stats["translated"] == 0 and stats["skipped"] >= 1
    row = store.list_messages(cid)[0]
    assert row["translated_text"] == ""  # 库未被污染
    store.close()


def test_ingest_strips_placeholder_translated_text(tmp_path):
    """ingest 净化：message_obj 预填 translated==text 的占位不落库（存空）。"""
    from src.inbox.ingest import ingest_thread
    store = InboxStore(tmp_path / "inbox.db")
    chat = {"conversation_id": "line:default:u5", "platform": "line",
            "account_id": "default", "chat_key": "u5", "name": "U"}
    msgs = [message_obj(text="xin chao ban", direction="in", ts=50.0)]
    assert msgs[0]["translated_text"] == "xin chao ban"    # 预填占位（现状）
    ingest_thread(store, chat, msgs)
    row = store.list_messages("line:default:u5")[0]
    assert row["translated_text"] == ""                    # 占位被净化
    # 真译文（≠原文）照常落库
    msgs2 = [dict(message_obj(text="hola amigo", direction="in", ts=51.0),
                  translated_text="你好朋友")]
    ingest_thread(store, chat, msgs2)
    row2 = [r for r in store.list_messages("line:default:u5") if r["text"] == "hola amigo"][0]
    assert row2["translated_text"] == "你好朋友"
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
