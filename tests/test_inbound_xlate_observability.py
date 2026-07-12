"""入站翻译观测闭环 + 存量消化 worker（2026-07 /thread 性能重构收尾）。

覆盖：
- InboundTranslationStats 计数/dump/dump_prom/runtime 注入；
- enrich 埋点：同步三态 / deferred / 冷却拦截计入单例；
- /api/workspace/metrics 暴露 inbound_translation（json + prometheus）；
- InboundXlateBackfillWorker：默认关空转、开启后消化存量、每 tick 开工上限、
  与在线路径共用会话级 in-flight 锁。
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request

import src.workspace.inbound_translate as IT
from src.ai.inbound_translation_stats import (
    InboundTranslationStats,
    get_inbound_translation_stats,
)
from src.ai.translation_service import TranslationResult
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.normalizer import message_obj
from src.inbox.store import InboxStore
from src.workspace.inbound_backfill import (
    InboundXlateBackfillWorker,
    parse_backfill_cfg,
)
from src.workspace.inbound_translate import enrich_inbound_translations


@pytest.fixture(autouse=True)
def _clear_state():
    get_inbound_translation_stats().reset()
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()
    yield
    get_inbound_translation_stats().reset()
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()


def _seed(store, cid, n, text_fn=None, lang="en"):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="default",
        chat_key=cid.split(":")[-1], display_name="U", last_text="x", last_ts=100.0 + n,
    ))
    for i in range(n):
        text = text_fn(i) if text_fn else f"hello msg {i}"
        store.ingest_message(InboxMessage(
            conversation_id=cid, platform_msg_id=f"m{i}", direction="in",
            text=text, original_text=text, source_lang=lang, ts=100.0 + i,
        ))


def _req(store):
    app = FastAPI()
    app.state.inbox_store = store
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


_CFG = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {"enabled": True}}})


class _OkSvc:
    async def translate(self, text, **kw):
        return TranslationResult(text, f"译:{text}", "en", "zh", True, provider="ai")


# ─────────────────────── stats 单例 ───────────────────────

def test_stats_counts_and_prom():
    s = InboundTranslationStats()
    s.record_sync("ok"); s.record_sync("noop"); s.record_sync("fail")
    s.record_bg("ok"); s.record_bg("fail")
    s.record_deferred(3)
    s.record_skipped_cooldown()
    d = s.dump(runtime={"bg_convs": 1, "inflight_mids": 2, "failed_cached": 3})
    assert d["sync_ok"] == 1 and d["sync_noop"] == 1 and d["sync_fail"] == 1
    assert d["bg_ok"] == 1 and d["bg_fail"] == 1
    assert d["bg_fail_rate"] == 0.5
    assert d["deferred_total"] == 3 and d["bg_spawned"] == 1
    assert d["skipped_cooldown"] == 1
    assert d["runtime"]["bg_convs"] == 1
    prom = s.dump_prom()
    assert 'inbound_xlate_sync_total{outcome="ok"} 1' in prom
    assert 'inbound_xlate_bg_total{outcome="fail"} 1' in prom
    assert "inbound_xlate_deferred_total 3" in prom
    s.reset()
    assert s.dump()["sync_ok"] == 0


@pytest.mark.asyncio
async def test_enrich_records_into_singleton(tmp_path):
    """同步三态 + deferred + 冷却拦截都计入进程级单例。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:s1"
    _seed(store, cid, 5)
    req = _req(store)
    msgs = [message_obj(text=f"hello msg {i}", direction="in",
                        message_id=f"{cid}:m{i}", ts=100.0 + i) for i in range(5)]
    _, stats = await enrich_inbound_translations(
        req, msgs, conversation_id=cid, config_manager=_CFG, translation_svc=_OkSvc())
    assert stats["translated"] == IT._SYNC_MAX_MSGS and stats["deferred"] == 3
    for _ in range(200):
        if cid not in IT._BG_CONVS:
            break
        await asyncio.sleep(0.01)
    d = get_inbound_translation_stats().dump()
    assert d["sync_ok"] == IT._SYNC_MAX_MSGS
    assert d["deferred_total"] == 3 and d["bg_spawned"] == 1
    assert d["bg_ok"] == 3                      # 后台补译全部成功计数

    # 冷却拦截计数：人工把一条置入失败负缓存后重开
    IT._mark_failed(f"{cid}:m0")
    store2_msgs = [message_obj(text="hello msg 0", direction="in",
                               message_id=f"{cid}:m0", ts=100.0)]
    # 抹掉已写库的译文让它重新成为候选（模拟只剩冷却拦截）
    store._conn.execute("UPDATE messages SET translated_text='', target_lang='' "
                        "WHERE conversation_id=?", (cid,))
    store._conn.commit()
    _, s2 = await enrich_inbound_translations(
        req, store2_msgs, conversation_id=cid, config_manager=_CFG, translation_svc=_OkSvc())
    assert get_inbound_translation_stats().dump()["skipped_cooldown"] >= 1
    store.close()


def test_metrics_endpoint_exposes_inbound_translation(tmp_path):
    """/api/workspace/metrics：json 有 inbound_translation 块，prometheus 有对应 counter。"""
    from fastapi.testclient import TestClient
    from src.web.routes.drafts_routes import register_metrics_route

    get_inbound_translation_stats().record_sync("ok")
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(request: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    c = TestClient(app, raise_server_exceptions=True)
    d = c.get("/api/workspace/metrics").json()
    inx = d.get("inbound_translation")
    assert inx is not None and inx["sync_ok"] == 1
    assert "runtime" in inx and "bg_convs" in inx["runtime"]
    p = c.get("/api/workspace/metrics?format=prometheus").text
    assert 'inbound_xlate_sync_total{outcome="ok"} 1' in p


# ─────────────────────── 存量消化 worker ───────────────────────

def test_parse_backfill_cfg_defaults_and_clamp():
    assert parse_backfill_cfg(None)["enabled"] is False
    cfg = parse_backfill_cfg(SimpleNamespace(config={"workspace": {"auto_translate_inbound": {
        "backfill": {"enabled": True, "interval_sec": 5, "scan_convs": 999,
                     "max_active_convs": 99}}}}))
    assert cfg["enabled"] is True
    assert cfg["interval_sec"] == 30.0       # 下限钳制
    assert cfg["scan_convs"] == 100          # 上限钳制
    assert cfg["max_active_convs"] == 10


@pytest.mark.asyncio
async def test_backfill_tick_digests_stock(tmp_path):
    """开启后一个 tick 消化存量：同步 2 条 + 其余转后台，全部写库。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:bf1"
    _seed(store, cid, 5)
    cm = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {
        "enabled": True, "backfill": {"enabled": True}}}})
    w = InboundXlateBackfillWorker(
        inbox_store=store, config_manager=cm, translation_svc_getter=lambda: _OkSvc())
    await w._tick(parse_backfill_cfg(cm))
    for _ in range(200):
        if cid not in IT._BG_CONVS:
            break
        await asyncio.sleep(0.01)
    rows = store.list_messages(cid)
    assert sum(1 for r in rows if r["translated_text"].startswith("译:")) == 5
    assert w.total_convs_worked == 1
    assert w.total_translated == IT._SYNC_MAX_MSGS and w.total_deferred == 3
    store.close()


@pytest.mark.asyncio
async def test_backfill_respects_max_active_convs(tmp_path):
    """每 tick 开工会话数受 max_active_convs 限制（防打满翻译引擎）。"""
    store = InboxStore(tmp_path / "inbox.db")
    for i in range(4):
        _seed(store, f"line:default:c{i}", 1, text_fn=lambda _i, i=i: f"hola {i}")
    cm = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {
        "enabled": True, "backfill": {"enabled": True, "max_active_convs": 2}}}})
    w = InboundXlateBackfillWorker(
        inbox_store=store, config_manager=cm, translation_svc_getter=lambda: _OkSvc())
    await w._tick(parse_backfill_cfg(cm))
    assert w.total_convs_worked == 2           # 只开工 2 个，其余留给下 tick
    store.close()


@pytest.mark.asyncio
async def test_backfill_skips_conv_inflight(tmp_path):
    """在线路径正在后台补译的会话（_BG_CONVS 锁）：worker 的 enrich 不重复开工。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:default:busy"
    _seed(store, cid, 3)
    IT._BG_CONVS.add(cid)                       # 模拟在线路径已挂后台任务
    cm = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {
        "enabled": True, "backfill": {"enabled": True}}}})
    w = InboundXlateBackfillWorker(
        inbox_store=store, config_manager=cm, translation_svc_getter=lambda: _OkSvc())
    await w._tick(parse_backfill_cfg(cm))
    # 同步预算仍会译 2 条（这是打开会话同款行为），但剩余候选**不 spawn** 重复任务
    assert w.total_deferred == 0
    IT._BG_CONVS.discard(cid)
    store.close()


@pytest.mark.asyncio
async def test_backfill_noop_when_svc_missing(tmp_path):
    """translation_service 未就绪（启动早期）→ tick 空转不炸。"""
    store = InboxStore(tmp_path / "inbox.db")
    cm = SimpleNamespace(config={"workspace": {"auto_translate_inbound": {
        "enabled": True, "backfill": {"enabled": True}}}})
    w = InboundXlateBackfillWorker(
        inbox_store=store, config_manager=cm, translation_svc_getter=lambda: None)
    await w._tick(parse_backfill_cfg(cm))
    assert w.total_convs_scanned == 0
    store.close()


def test_backfill_status_snapshot():
    w = InboundXlateBackfillWorker(
        inbox_store=None, config_manager=None, translation_svc_getter=lambda: None)
    snap = w.status_snapshot()
    assert snap["running"] is False and snap["enabled"] is False
    assert "total_convs_worked" in snap
