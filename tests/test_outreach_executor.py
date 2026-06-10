"""P61-4：触达执行器 + execute/batch 端点 契约测试。"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation
from src.inbox.outreach_executor import OutreachExecutor, render_template
from src.inbox.outreach_planner import OutreachTarget
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

DAY = 86400.0


def _target(cid="telegram:a:1", account="a", name="小明", silent=5.0):
    return OutreachTarget(
        conversation_id=cid, platform="telegram", account_id=account,
        chat_key=cid.split(":")[-1], display_name=name,
        last_ts=0, silent_days=silent, tags=[], rel_stage="",
    )


class _Limiter:
    def __init__(self, caps):
        self._caps = dict(caps)

    def remaining_for(self, account_id, *, now=None):
        return self._caps.get(account_id, 0)

    def check_and_reserve(self, account_id, *, now=None):
        class _D:
            pass
        d = _D()
        if self._caps.get(account_id, 0) > 0:
            self._caps[account_id] -= 1
            d.ok = True
            d.reason = "reserved"
        else:
            d.ok = False
            d.reason = "account_cap_exceeded"
        return d


# ── render_template ────────────────────────────────────────────────────────
def test_render_template_placeholders():
    t = _target(name="Alice", silent=7.0)
    assert render_template("Hi {name}, 你 {silent_days} 天没来 ({platform})", t) \
        == "Hi Alice, 你 7 天没来 (telegram)"


def test_render_template_empty_name_fallback():
    t = _target(name="")
    assert render_template("{name}你好", t) == "朋友你好"


# ── executor ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_happy_path_records_sent():
    store = InboxStore(":memory:")
    sent = []

    async def send_fn(target, text):
        sent.append((target.conversation_id, text))
        return {"ok": True}

    ex = OutreachExecutor(store, send_fn, limiter=_Limiter({"a": 5}))
    res = await ex.execute([_target("telegram:a:1"), _target("telegram:a:2")],
                           "Hi {name}", batch_id="b1")
    assert res["sent"] == 2 and res["failed"] == 0
    assert len(sent) == 2
    assert store.outreach_batch_stats("b1")["by_status"] == {"sent": 2}


@pytest.mark.asyncio
async def test_execute_send_failure_records_failed():
    store = InboxStore(":memory:")

    async def send_fn(target, text):
        raise RuntimeError("rpa down")

    ex = OutreachExecutor(store, send_fn, limiter=_Limiter({"a": 5}))
    res = await ex.execute([_target("telegram:a:1")], "Hi", batch_id="b1")
    assert res["sent"] == 0 and res["failed"] == 1
    assert store.outreach_batch_stats("b1")["by_status"] == {"failed": 1}


@pytest.mark.asyncio
async def test_execute_cap_skip_not_logged():
    """配额拒绝 → 跳过、不发、不写 outreach_log（cooldown 语义干净）。"""
    store = InboxStore(":memory:")
    calls = []

    async def send_fn(target, text):
        calls.append(target.conversation_id)

    ex = OutreachExecutor(store, send_fn, limiter=_Limiter({"a": 1}))
    res = await ex.execute([_target("telegram:a:1"), _target("telegram:a:2")],
                           "Hi", batch_id="b1")
    assert res["sent"] == 1 and res["skipped"] == 1
    assert calls == ["telegram:a:1"]
    # 仅 1 条真实发送写入 log
    assert store.outreach_batch_stats("b1")["total"] == 1


@pytest.mark.asyncio
async def test_execute_max_send_limits():
    store = InboxStore(":memory:")

    async def send_fn(target, text):
        pass

    ex = OutreachExecutor(store, send_fn, limiter=_Limiter({"a": 99}))
    res = await ex.execute([_target(f"telegram:a:{i}") for i in range(5)],
                           "Hi", batch_id="b1", max_send=2)
    assert res["attempted"] == 2 and res["sent"] == 2


# ── 端点 ──────────────────────────────────────────────────────────────────
class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


class _Adapter:
    platform = "telegram"
    sent = []

    async def send(self, request, account_id, chat_key, text):
        _Adapter.sent.append((account_id, chat_key, text))
        return {"ok": True, "conversation_id": f"telegram:{account_id}:{chat_key}"}


def _client(cfg, store):
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    app.state.config_manager = FakeCM(cfg)
    app.state.inbox_store = store
    return TestClient(app)


def _seed(store):
    for i in range(2):
        cid = f"telegram:a:{i}"
        conv = InboxConversation(
            conversation_id=cid, platform="telegram", account_id="a",
            chat_key=str(i), display_name=f"用户{i}", last_ts=1000 - 10 * DAY,
        )
        store.ingest_batch(conv, [])


def test_execute_endpoint_disabled_by_default():
    store = InboxStore(":memory:")
    _seed(store)
    r = _client({"outreach": {"enabled": False}}, store).post(
        "/api/unified-inbox/outreach/execute",
        json={"template": "Hi", "confirm": True}).json()
    assert r["ok"] is False and r["reason"] == "outreach_disabled"


def test_execute_endpoint_requires_confirm():
    store = InboxStore(":memory:")
    _seed(store)
    r = _client({"outreach": {"enabled": True}}, store).post(
        "/api/unified-inbox/outreach/execute",
        json={"template": "Hi", "confirm": False}).json()
    assert r["ok"] is False and r["reason"] == "confirm_required"


def test_execute_endpoint_empty_template():
    store = InboxStore(":memory:")
    _seed(store)
    r = _client({"outreach": {"enabled": True}}, store).post(
        "/api/unified-inbox/outreach/execute",
        json={"template": "  ", "confirm": True}).json()
    assert r["ok"] is False and r["reason"] == "empty_template"


def test_execute_endpoint_sends_and_batch_stats():
    _Adapter.sent = []
    store = InboxStore(":memory:")
    _seed(store)
    cfg = {"outreach": {"enabled": True, "per_send_seconds": 0, "default_account_cap": 10,
                        "cooldown_days": 0, "max_batch": 50}}
    client = _client(cfg, store)
    # 替换适配器为假适配器（模块级 _INBOX_ADAPTERS）
    import src.web.routes.unified_inbox_routes as mod
    old = mod._INBOX_ADAPTERS
    mod._INBOX_ADAPTERS = [_Adapter()]
    try:
        r = client.post("/api/unified-inbox/outreach/execute",
                        json={"filters": {"min_silent_days": 3}, "template": "Hi {name}",
                              "confirm": True, "batch_id": "bx"}).json()
        assert r["ok"] is True
        assert r["sent"] == 2 and r["failed"] == 0
        assert len(_Adapter.sent) == 2
        # batch 查询
        b = client.get("/api/unified-inbox/outreach/batch?batch_id=bx").json()
        assert b["ok"] is True and b["by_status"].get("sent") == 2
    finally:
        mod._INBOX_ADAPTERS = old


def test_execute_endpoint_max_batch_hard_cap():
    _Adapter.sent = []
    store = InboxStore(":memory:")
    _seed(store)
    cfg = {"outreach": {"enabled": True, "per_send_seconds": 0, "default_account_cap": 10,
                        "cooldown_days": 0, "max_batch": 1}}
    client = _client(cfg, store)
    import src.web.routes.unified_inbox_routes as mod
    old = mod._INBOX_ADAPTERS
    mod._INBOX_ADAPTERS = [_Adapter()]
    try:
        r = client.post("/api/unified-inbox/outreach/execute",
                        json={"filters": {"min_silent_days": 3}, "template": "Hi",
                              "confirm": True, "max_send": 99}).json()
        # 硬上限 max_batch=1 生效
        assert r["attempted"] == 1
    finally:
        mod._INBOX_ADAPTERS = old
