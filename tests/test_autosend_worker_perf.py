"""Phase A + C1 测试：AutosendWorker + 坐席绩效 API。

覆盖：
  AutosendWorker
    - 基础生命周期（stop/status_snapshot）
    - 无 L2 草稿时 sent=0
    - L2 草稿批量发送（mock resolve_with_audit）
    - 熔断器：连续错误触发 circuit_open
    - 自适应间隔：有发送→min，无发送→扩张
    - process_batch 单条隔离（一条异常不影响其他）

  InboxStore.get_agent_perf / get_agent_perf_timeline
    - 空表返回 []
    - 聚合正确性（total / approved / rejected / force_override）
    - 按 agent_id 过滤
    - timeline 按天分桶

  GET /api/workspace/agent-perf（API）
    - 非主管 403
    - 主管 200 + 正确 agents 列表
    - 无 InboxStore 503

  GET /api/workspace/agent-perf/timeline
    - 主管 200
    - 非主管 403

  GET /workspace/agent-perf（页面）
    - 主管 200
    - 非主管 302→/workspace

  GET /api/drafts/autosend-status
    - 无 worker → note 字段
    - 主管 200
    - 非主管 403

  openapi 基线
    - autosend-status、agent-perf、agent-perf/timeline 均注册
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore
from src.web.routes.drafts_routes import (
    register_drafts_routes,
    register_agent_perf_routes,
)


# ──────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────

class _Templates:
    def TemplateResponse(self, request, name, context):
        return HTMLResponse(content=f"<html>{name}</html>")


def _make_api_app(store=None, role: str = "", worker=None):
    app = FastAPI()
    if role:
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1", "username": "u1"}
            return await call_next(request)

    def api_auth(request: Request) -> None: return None
    register_drafts_routes(app, api_auth=api_auth)

    if store is not None:
        app.state.inbox_store = store
    if worker is not None:
        app.state.autosend_worker = worker
    return TestClient(app, raise_server_exceptions=True)


def _make_perf_app(store=None, role: str = "admin"):
    app = FastAPI()
    if role:
        @app.middleware("http")
        async def _inj(request: Request, call_next):
            request.scope["session"] = {"role": role, "user_id": "u1", "username": "u1"}
            return await call_next(request)

    def api_auth(request: Request) -> None: return None
    def page_auth(request: Request) -> None: return None

    register_agent_perf_routes(
        app, api_auth=api_auth, page_auth=page_auth, templates=_Templates()
    )
    if store is not None:
        app.state.inbox_store = store
    return TestClient(app, raise_server_exceptions=True)


def _seed_audit(store: InboxStore, rows: list[dict]) -> None:
    for row in rows:
        store.record_draft_audit(
            draft_id=row.get("draft_id", "d1"),
            autopilot_level=row.get("autopilot_level", "L1"),
            action=row.get("action", "approved"),
            agent_id=row.get("agent_id", "agent1"),
            reason=row.get("reason", ""),
            risk_level=row.get("risk_level", "low"),
            conversation_id=row.get("conversation_id", "conv1"),
        )


# ──────────────────────────────────────────────────────
# AutosendWorker — 单元
# ──────────────────────────────────────────────────────

class TestAutosendWorker:
    def _make_svc(self, drafts=None, ok=True):
        svc = MagicMock()
        svc.list_drafts.return_value = drafts or []
        svc.resolve_with_audit.return_value = {"ok": ok}
        return svc

    def test_status_snapshot_initial(self):
        svc = self._make_svc()
        w = AutosendWorker(draft_service=svc, config={"enabled": True})
        snap = w.status_snapshot()
        assert snap["enabled"] is True
        assert snap["running"] is False
        assert snap["total_sent"] == 0
        assert snap["circuit_open"] is False

    def test_process_batch_no_l2(self):
        svc = self._make_svc(drafts=[
            {"draft_id": "d1", "autopilot_level": "L3", "status": "pending"},
        ])
        w = AutosendWorker(draft_service=svc, config={})
        sent, errors, to_deliver = w._process_batch()
        assert sent == 0
        assert errors == 0
        assert to_deliver == []

    def test_process_batch_sends_l2(self):
        svc = self._make_svc(drafts=[
            {"draft_id": "inbox:a1", "autopilot_level": "L2", "status": "pending"},
            {"draft_id": "inbox:a2", "autopilot_level": "L2", "status": "pending"},
            {"draft_id": "inbox:a3", "autopilot_level": "L4", "status": "pending"},
        ], ok=True)
        w = AutosendWorker(draft_service=svc, config={})
        sent, errors, to_deliver = w._process_batch()
        assert sent == 2
        assert errors == 0
        # 无 send_callback 时不收集投递载荷（保持旧行为：仅 DB 标记）
        assert to_deliver == []

    def test_process_batch_isolates_errors(self):
        svc = MagicMock()
        svc.list_drafts.return_value = [
            {"draft_id": "d1", "autopilot_level": "L2", "status": "pending"},
            {"draft_id": "d2", "autopilot_level": "L2", "status": "pending"},
        ]
        # d1 raises, d2 succeeds
        def _resolve(did, action, **kw):
            if did == "d1":
                raise RuntimeError("platform error")
            return {"ok": True}
        svc.resolve_with_audit.side_effect = _resolve

        w = AutosendWorker(draft_service=svc, config={})
        sent, errors, _ = w._process_batch()
        assert sent == 1
        assert errors == 1

    def test_process_batch_collects_deliver_payload_when_callback(self):
        """注入 send_callback 后，resolve 成功的 L2 草稿收集投递载荷（含 text/平台路由键）。"""
        async def _noop(*a, **k):
            return {"ok": True}
        svc = self._make_svc(drafts=[
            {"draft_id": "inbox:a1", "autopilot_level": "L2", "status": "pending",
             "platform": "line", "account_id": "line-a", "chat_key": "room1",
             "conversation_id": "line:line-a:room1", "draft_text": "您好，已收到"},
            {"draft_id": "inbox:a2", "autopilot_level": "L2", "status": "pending",
             "platform": "telegram", "chat_key": "tg1", "draft_text": "hi",
             "final_text": "您好（终）"},
        ], ok=True)
        w = AutosendWorker(draft_service=svc, config={}, send_callback=_noop)
        sent, errors, to_deliver = w._process_batch()
        assert sent == 2 and errors == 0
        assert len(to_deliver) == 2
        assert to_deliver[0]["platform"] == "line"
        assert to_deliver[0]["chat_key"] == "room1"
        assert to_deliver[0]["text"] == "您好，已收到"
        # final_text 优先于 draft_text
        assert to_deliver[1]["text"] == "您好（终）"

    def test_tick_delivers_via_callback(self):
        """_tick：resolve 成功后真正调用 send_callback 投递，更新 delivered 计数。"""
        delivered = []

        async def _cb(platform, account_id, chat_key, text):
            delivered.append((platform, account_id, chat_key, text))
            return {"ok": True}

        svc = self._make_svc(drafts=[
            {"draft_id": "inbox:a1", "autopilot_level": "L2", "status": "pending",
             "platform": "line", "account_id": "line-a", "chat_key": "room1",
             "draft_text": "您好"},
        ], ok=True)
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 0}, send_callback=_cb)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(w._tick())
        finally:
            loop.close()
        assert delivered == [("line", "line-a", "room1", "您好")]
        assert w.total_delivered == 1
        assert w.total_deliver_errors == 0
        snap = w.status_snapshot()
        assert snap["deliver_enabled"] is True
        assert snap["total_delivered"] == 1

    def test_tick_deliver_failure_counts_not_resend(self):
        """投递失败计入 deliver_errors，但草稿已 resolve（不会重发，避免刷屏）。"""
        async def _cb(platform, account_id, chat_key, text):
            raise RuntimeError("platform down")

        svc = self._make_svc(drafts=[
            {"draft_id": "inbox:a1", "autopilot_level": "L2", "status": "pending",
             "platform": "line", "chat_key": "room1", "draft_text": "您好"},
        ], ok=True)
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 0}, send_callback=_cb)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(w._tick())
        finally:
            loop.close()
        assert w.total_delivered == 0
        assert w.total_deliver_errors == 1
        # resolve 成功 → sent 计数仍 +1（投递失败不回滚审计，宁丢不重发）
        assert w.total_sent == 1

    def test_tick_deliver_failure_writes_audit(self):
        """投递失败时调用 DraftService.record_autosend_failure（写 autosend_failed 审计）。"""
        async def _cb(platform, account_id, chat_key, text):
            raise RuntimeError("platform down")

        svc = self._make_svc(drafts=[
            {"draft_id": "inbox:a1", "autopilot_level": "L2", "status": "pending",
             "platform": "line", "chat_key": "room1", "draft_text": "您好",
             "conversation_id": "line:default:room1"},
        ], ok=True)
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 0}, send_callback=_cb)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(w._tick())
        finally:
            loop.close()
        svc.record_autosend_failure.assert_called_once()
        _args, _kw = svc.record_autosend_failure.call_args
        assert _args[0] == "inbox:a1"
        assert _kw.get("conversation_id") == "line:default:room1"

    def test_startup_delay_interrupted_by_event(self):
        """首发延迟可被新 L2 事件提前唤醒（不傻等满 startup_delay）。"""
        svc = self._make_svc(drafts=[])
        # startup_delay 很大，但事件触发应让 run() 几乎立即跳过等待进入循环
        w = AutosendWorker(
            draft_service=svc,
            config={"startup_delay_sec": 30, "min_interval_sec": 0, "max_interval_sec": 0},
        )

        async def _drive():
            task = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.02)
            w.notify_new_l2()          # 模拟新 L2 草稿落库
            await asyncio.sleep(0.05)  # 给 run() 机会跑过 startup 等待 + 首个 _tick
            w.stop()
            w.notify_new_l2()          # 唤醒循环让其检查 _running=False 退出
            await asyncio.wait_for(task, timeout=2.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drive())
        finally:
            loop.close()
        # 若 startup 等待未被打断，0.05s 内不可能 cycles>=1（startup_delay=30s）
        assert w.cycles >= 1
        assert w.event_triggers >= 1

    def test_circuit_breaker_opens(self):
        svc = MagicMock()
        svc.list_drafts.return_value = [
            {"draft_id": "d1", "autopilot_level": "L2", "status": "pending"},
        ]
        svc.resolve_with_audit.return_value = {"ok": False, "error": "err"}

        w = AutosendWorker(draft_service=svc, config={"circuit_threshold": 3, "min_interval_sec": 0})
        loop = asyncio.new_event_loop()
        try:
            for _ in range(3):
                loop.run_until_complete(w._tick())
        finally:
            loop.close()

        assert w._circuit_open is True
        assert w._consecutive_errors >= 3

    def test_adaptive_interval_shrinks_on_send(self):
        svc = self._make_svc(drafts=[
            {"draft_id": "d1", "autopilot_level": "L2", "status": "pending"},
        ])
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 60, "max_interval_sec": 600})
        # manually inflate the interval
        w._current_interval = 300
        w._adapt_interval(sent=1)
        assert w._current_interval == 60  # reset to min

    def test_adaptive_interval_grows_on_idle(self):
        svc = self._make_svc()
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 60, "max_interval_sec": 600})
        w._current_interval = 60
        w._adapt_interval(sent=0)
        assert w._current_interval == pytest.approx(90.0)  # 60 * 1.5

    def test_adaptive_interval_caps_at_max(self):
        svc = self._make_svc()
        w = AutosendWorker(draft_service=svc, config={"min_interval_sec": 60, "max_interval_sec": 600})
        w._current_interval = 500
        w._adapt_interval(sent=0)
        assert w._current_interval == 600  # capped

    def test_disabled_worker(self):
        svc = self._make_svc()
        w = AutosendWorker(draft_service=svc, config={"enabled": False})
        snap = w.status_snapshot()
        assert snap["enabled"] is False


# ──────────────────────────────────────────────────────
# InboxStore.get_agent_perf / get_agent_perf_timeline
# ──────────────────────────────────────────────────────

class TestAgentPerfStore:
    def test_empty(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        rows = store.get_agent_perf(since_ts=0.0)
        assert rows == []

    def test_aggregation(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [
            {"agent_id": "alice", "action": "approved"},
            {"agent_id": "alice", "action": "approved"},
            {"agent_id": "alice", "action": "rejected"},
            {"agent_id": "bob",   "action": "force_override"},
            {"agent_id": "bob",   "action": "autosend"},
        ])
        rows = store.get_agent_perf(since_ts=0.0)
        by_agent = {r["agent_id"]: r for r in rows}
        assert by_agent["alice"]["total"] == 3
        assert by_agent["alice"]["approved"] == 2
        assert by_agent["alice"]["rejected"] == 1
        assert by_agent["bob"]["force_override"] == 1
        assert by_agent["bob"]["autosend"] == 1

    def test_agent_id_filter(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [
            {"agent_id": "alice", "action": "approved"},
            {"agent_id": "bob",   "action": "approved"},
        ])
        rows = store.get_agent_perf(since_ts=0.0, agent_id="alice")
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "alice"

    def test_since_ts_filter(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [{"agent_id": "alice", "action": "approved"}])
        future = time.time() + 100
        rows = store.get_agent_perf(since_ts=future)
        assert rows == []

    def test_timeline_buckets(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [
            {"agent_id": "alice", "action": "approved"},
            {"agent_id": "alice", "action": "rejected"},
        ])
        tl = store.get_agent_perf_timeline(since_ts=0.0, bucket_sec=86400)
        assert len(tl) >= 1
        row = tl[0]
        assert "bucket_ts" in row
        assert row["total"] >= 2

    def test_timeline_agent_filter(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [
            {"agent_id": "alice", "action": "approved"},
            {"agent_id": "bob",   "action": "approved"},
        ])
        tl = store.get_agent_perf_timeline(since_ts=0.0, agent_id="alice")
        for row in tl:
            assert row["agent_id"] == "alice"


# ──────────────────────────────────────────────────────
# GET /api/workspace/agent-perf (API)
# ──────────────────────────────────────────────────────

class TestAgentPerfAPI:
    def test_403_for_non_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_perf_app(store=store, role="agent")
        r = c.get("/api/workspace/agent-perf")
        assert r.status_code == 403

    def test_200_for_supervisor(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        _seed_audit(store, [{"agent_id": "alice", "action": "approved"}])
        c = _make_perf_app(store=store, role="admin")
        r = c.get("/api/workspace/agent-perf")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["total_agents"] == 1

    def test_503_no_store(self):
        c = _make_perf_app(store=None, role="admin")
        r = c.get("/api/workspace/agent-perf")
        assert r.status_code == 503

    def test_timeline_403(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_perf_app(store=store, role="agent")
        r = c.get("/api/workspace/agent-perf/timeline")
        assert r.status_code == 403

    def test_timeline_200(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_perf_app(store=store, role="admin")
        r = c.get("/api/workspace/agent-perf/timeline")
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ──────────────────────────────────────────────────────
# GET /workspace/agent-perf (页面)
# ──────────────────────────────────────────────────────

class TestAgentPerfPage:
    def test_supervisor_200(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_perf_app(store=store, role="admin")
        r = c.get("/workspace/agent-perf", follow_redirects=False)
        assert r.status_code == 200
        assert b"agent_perf.html" in r.content

    def test_non_supervisor_redirect(self, tmp_path):
        store = InboxStore(tmp_path / "inbox.db")
        c = _make_perf_app(store=store, role="agent")
        r = c.get("/workspace/agent-perf", follow_redirects=False)
        assert r.status_code == 302
        assert "/workspace" in r.headers.get("location", "")


# ──────────────────────────────────────────────────────
# GET /api/drafts/autosend-status
# ──────────────────────────────────────────────────────

class TestAutosendStatusAPI:
    def test_403_non_supervisor(self):
        c = _make_api_app(role="agent")
        r = c.get("/api/drafts/autosend-status")
        assert r.status_code == 403

    def test_no_worker_returns_note(self):
        c = _make_api_app(role="admin")
        r = c.get("/api/drafts/autosend-status")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["worker"] is None

    def test_with_worker_returns_snapshot(self):
        svc = MagicMock()
        svc.list_drafts.return_value = []
        w = AutosendWorker(draft_service=svc, config={})
        c = _make_api_app(role="admin", worker=w)
        r = c.get("/api/drafts/autosend-status")
        assert r.status_code == 200
        body = r.json()
        snap = body["worker"]
        assert "total_sent" in snap
        assert "circuit_open" in snap
        assert "current_interval_sec" in snap


# ──────────────────────────────────────────────────────
# openapi 基线
# ──────────────────────────────────────────────────────

class TestOpenAPIBaseline:
    def test_autosend_status_registered(self):
        c = _make_api_app(role="admin")
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/drafts/autosend-status" in paths

    def test_agent_perf_registered(self):
        c = _make_perf_app(role="admin")
        paths = c.get("/openapi.json").json().get("paths", {})
        assert "/api/workspace/agent-perf" in paths
        assert "/api/workspace/agent-perf/timeline" in paths
        assert "/workspace/agent-perf" in paths
