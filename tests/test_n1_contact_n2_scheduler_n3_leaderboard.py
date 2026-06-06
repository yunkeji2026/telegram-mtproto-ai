"""N1 跨平台会话归档 + N2 定时简报推送 + N3 CSAT 排行榜 测试。

N1 — contact_id 跨平台会话归档:
  - update_conv_meta() 接受 contact_id 参数并持久化
  - contact_id 不传时保留已有值（不覆盖）
  - get_contact_sessions() 返回同一 contact_id 的所有会话
  - get_contact_csat_avg() 返回加权均值
  - K3 contact-profile API 含 cross_platform 字段

N2 — ScheduledReporter 定时简报推送:
  - status_snapshot() 字段完整
  - _check() 在时间匹配时触发日报
  - _check() 防重发：同一天只发一次
  - _check() 周报只在指定星期触发
  - trigger() 手动触发
  - stop() 关闭运行循环
  - 集成 L1 metrics：/api/workspace/metrics 含 scheduled_reporter

N3 — /api/workspace/leaderboard:
  - 非主管 → 403
  - 无数据时返回空列表
  - 有数据时按 avg_csat DESC 排序
  - 排名字段 rank / badge / csat_stars 正确
  - period=daily/weekly/monthly 均支持
  - limit 参数限制结果数量
  - 路由在 inventory 中

测试共 ≥ 30 用例
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.scheduled_reporter import ScheduledReporter


# ─────────────────────────── fixtures ──────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    return InboxStore(db_path=str(tmp_path / "n.db"))


def _add_session(store, cid, contact_id="", platform="tg", csat=None):
    store.update_conv_meta(cid, platform=platform, intent="order", contact_id=contact_id)
    if csat is not None:
        store.update_conv_csat(cid, csat)


# ─────────────────────────── N1 Tests ──────────────────────────────────────

class TestN1ContactLink:
    def test_contact_id_persisted(self, store):
        store.update_conv_meta("c1", platform="tg", contact_id="ctid_001")
        meta = store.get_conv_meta("c1")
        assert meta is not None
        assert meta.get("contact_id") == "ctid_001"

    def test_contact_id_not_overwritten_on_empty(self, store):
        store.update_conv_meta("c2", platform="tg", contact_id="ctid_002")
        store.update_conv_meta("c2", platform="tg", emotion="满意")  # 不传 contact_id
        meta = store.get_conv_meta("c2")
        assert meta["contact_id"] == "ctid_002"

    def test_contact_id_updatable(self, store):
        store.update_conv_meta("c3", platform="tg", contact_id="old_id")
        store.update_conv_meta("c3", platform="tg", contact_id="new_id")
        meta = store.get_conv_meta("c3")
        assert meta["contact_id"] == "new_id"

    def test_get_contact_sessions_empty(self, store):
        sessions = store.get_contact_sessions("nonexistent")
        assert sessions == []

    def test_get_contact_sessions_multiple_platforms(self, store):
        _add_session(store, "tg:c1", contact_id="ctid_x", platform="telegram")
        _add_session(store, "wa:c1", contact_id="ctid_x", platform="whatsapp")
        _add_session(store, "line:c1", contact_id="ctid_x", platform="line")
        sessions = store.get_contact_sessions("ctid_x")
        assert len(sessions) == 3
        platforms = {s["platform"] for s in sessions}
        assert "telegram" in platforms
        assert "whatsapp" in platforms
        assert "line" in platforms

    def test_get_contact_sessions_excludes_other_contacts(self, store):
        _add_session(store, "c_a", contact_id="ctid_a")
        _add_session(store, "c_b", contact_id="ctid_b")
        sessions = store.get_contact_sessions("ctid_a")
        assert len(sessions) == 1
        assert sessions[0]["conversation_id"] == "c_a"

    def test_get_contact_sessions_limit(self, store):
        for i in range(10):
            _add_session(store, f"c_{i}", contact_id="ctid_many")
        sessions = store.get_contact_sessions("ctid_many", limit=5)
        assert len(sessions) <= 5

    def test_get_contact_sessions_sorted_by_updated_at_desc(self, store):
        _add_session(store, "c_old", contact_id="ctid_ord")
        time.sleep(0.01)
        _add_session(store, "c_new", contact_id="ctid_ord")
        sessions = store.get_contact_sessions("ctid_ord")
        assert sessions[0]["conversation_id"] == "c_new"

    def test_get_contact_csat_avg_no_data(self, store):
        avg = store.get_contact_csat_avg("no_contact")
        assert avg is None

    def test_get_contact_csat_avg_single(self, store):
        _add_session(store, "ca1", contact_id="ctid_csat", csat=4.2)
        avg = store.get_contact_csat_avg("ctid_csat")
        assert avg == 4.2

    def test_get_contact_csat_avg_multiple(self, store):
        _add_session(store, "cb1", contact_id="ctid_multi", csat=4.0)
        _add_session(store, "cb2", contact_id="ctid_multi", csat=3.0)
        avg = store.get_contact_csat_avg("ctid_multi")
        assert avg is not None
        assert abs(avg - 3.5) < 0.1

    def test_get_contact_csat_avg_ignores_negative(self, store):
        _add_session(store, "cc1", contact_id="ctid_neg", csat=4.5)
        # 写一个 csat=-1 的（未评分）
        store.update_conv_meta("cc2", platform="tg", contact_id="ctid_neg")
        # csat_score 默认 -1，不参与均值
        avg = store.get_contact_csat_avg("ctid_neg")
        assert avg == 4.5


# ─────────────────────────── N1: K3 API cross_platform ─────────────────────

def _make_profile_app(tmp_path, role="admin"):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
    from src.inbox.store import InboxStore

    _store = InboxStore(db_path=str(tmp_path / "p.db"))
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request): return True

    register_unified_inbox_routes(
        app,
        api_auth=_api_auth,
        page_auth=_api_auth,
        templates=None,
        config_manager=None,
    )
    app.state.inbox_store = _store
    return app, _store


def test_contact_profile_cross_platform_none_without_contact_id(tmp_path):
    app, store = _make_profile_app(tmp_path)
    store.update_conv_meta("conv_no_contact", platform="tg", intent="order")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=conv_no_contact")
    assert r.status_code == 200
    data = r.json()
    assert "cross_platform" in data
    assert data["cross_platform"] is None


def test_contact_profile_cross_platform_with_contact_id(tmp_path):
    app, store = _make_profile_app(tmp_path)
    store.update_conv_meta("conv_linked", platform="tg", contact_id="ctid_profile")
    store.update_conv_meta("conv_other_platform", platform="line", contact_id="ctid_profile")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/unified-inbox/contact-profile?conversation_id=conv_linked")
    assert r.status_code == 200
    data = r.json()
    cp = data.get("cross_platform")
    assert cp is not None
    assert cp["contact_id"] == "ctid_profile"
    # conv_other_platform 在跨平台列表中（conv_linked 本身被排除）
    assert cp["session_count"] >= 1


# ─────────────────────────── N2: ScheduledReporter ─────────────────────────

class TestScheduledReporter:
    def test_status_snapshot_fields(self):
        cfg = {"daily_time": "09:00", "weekly_day": "monday", "tz_offset_hours": 8}
        rpt = ScheduledReporter(inbox_store=MagicMock(), config=cfg)
        snap = rpt.status_snapshot()
        assert "running" in snap
        assert "daily_time" in snap
        assert "weekly_day" in snap
        assert "tz_offset" in snap
        assert "last_daily" in snap
        assert "last_weekly" in snap
        assert "total_sent" in snap
        assert "total_errors" in snap

    def test_status_snapshot_default_not_running(self):
        rpt = ScheduledReporter(inbox_store=MagicMock())
        assert rpt.status_snapshot()["running"] is False

    def _make_ts(self, year, month, day, hour=9, minute=0) -> float:
        """构造 UTC 时间戳（tz_offset=0 时直接对应 UTC）。"""
        epoch = datetime.datetime(1970, 1, 1)
        dt = datetime.datetime(year, month, day, hour, minute, 0)
        return (dt - epoch).total_seconds()

    @pytest.mark.asyncio
    async def test_check_triggers_daily_at_target_time(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        cfg = {"daily_time": "09:00", "tz_offset_hours": 0}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        rpt._send = AsyncMock()

        ts_09 = self._make_ts(2026, 6, 6, 9, 0)
        rpt._now_local = lambda: ts_09
        await rpt._check()

        rpt._send.assert_called_once_with("daily")

    @pytest.mark.asyncio
    async def test_check_does_not_double_send_same_day(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        cfg = {"daily_time": "09:00", "tz_offset_hours": 0}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        rpt._send = AsyncMock()

        ts_09 = self._make_ts(2026, 6, 6, 9, 0)
        rpt._now_local = lambda: ts_09
        await rpt._check()  # 第一次 → 触发
        await rpt._check()  # 第二次 → 防重，不触发

        assert rpt._send.call_count == 1

    @pytest.mark.asyncio
    async def test_check_no_trigger_at_wrong_time(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        cfg = {"daily_time": "09:00", "tz_offset_hours": 0}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        rpt._send = AsyncMock()

        ts_10 = self._make_ts(2026, 6, 6, 10, 0)  # 10:00 ≠ 09:00
        rpt._now_local = lambda: ts_10
        await rpt._check()

        rpt._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_weekly_trigger_on_correct_weekday(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        cfg = {"daily_time": "09:00", "weekly_day": "saturday", "tz_offset_hours": 0}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        rpt._send = AsyncMock()

        # 2026-06-06 是周六（weekday=5）
        dt_sat = datetime.datetime(2026, 6, 6, 9, 0, 0)
        assert dt_sat.weekday() == 5, f"expected Saturday but got weekday={dt_sat.weekday()}"
        ts_sat = self._make_ts(2026, 6, 6, 9, 0)
        rpt._now_local = lambda: ts_sat
        await rpt._check()

        # 应触发日报 + 周报两次
        calls = [call.args[0] for call in rpt._send.call_args_list]
        assert "daily" in calls
        assert "weekly" in calls

    @pytest.mark.asyncio
    async def test_trigger_calls_send_directly(self):
        rpt = ScheduledReporter(inbox_store=MagicMock())
        rpt._send = AsyncMock()
        await rpt.trigger(period="weekly")
        rpt._send.assert_called_once_with("weekly")

    @pytest.mark.asyncio
    async def test_send_increments_total_sent(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        rpt = ScheduledReporter(inbox_store=store)
        # get_event_bus / ReportGenerator 都是在 _send 函数体内懒导入
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus, \
             patch("src.inbox.report_generator.ReportGenerator") as mock_gen_cls:
            mock_gen = MagicMock()
            mock_gen_cls.return_value = mock_gen
            mock_gen.generate.return_value = {"period_label": "今日"}
            mock_gen.format_text.return_value = "报告内容"
            mock_bus.return_value.publish = MagicMock()
            await rpt._send("daily")
        assert rpt.total_sent == 1

    @pytest.mark.asyncio
    async def test_send_increments_total_errors_on_failure(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        rpt = ScheduledReporter(inbox_store=store)
        # ReportGenerator 的导入路径是 src.inbox.report_generator
        with patch("src.inbox.report_generator.ReportGenerator") as mock_gen_cls:
            mock_gen_cls.side_effect = Exception("gen_fail")
            await rpt._send("daily")
        assert rpt.total_errors == 1

    @pytest.mark.asyncio
    async def test_stop_terminates_run_loop(self, tmp_path):
        store = InboxStore(db_path=str(tmp_path / "t.db"))
        cfg = {"tick_secs": 1}
        rpt = ScheduledReporter(inbox_store=store, config=cfg)
        rpt._check = AsyncMock()
        task = asyncio.create_task(rpt.run())
        await asyncio.sleep(0.1)
        rpt.stop()
        await asyncio.wait_for(task, timeout=3.0)
        assert rpt._running is False


# ─────────────────────────── N3: Leaderboard API ────────────────────────────

def _make_lb_app(tmp_path, role="master"):
    app = FastAPI()
    _store = InboxStore(db_path=str(tmp_path / "lb.db"))

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request): return True

    from src.web.routes.drafts_routes import register_leaderboard_route
    app.state.inbox_store = _store
    register_leaderboard_route(app, api_auth=_api_auth)
    return TestClient(app, raise_server_exceptions=False), _store


def test_leaderboard_requires_supervisor(tmp_path):
    client, _ = _make_lb_app(tmp_path, role="agent")
    r = client.get("/api/workspace/leaderboard")
    assert r.status_code == 403


def test_leaderboard_empty(tmp_path):
    client, _ = _make_lb_app(tmp_path)
    r = client.get("/api/workspace/leaderboard")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data["leaderboard"] == []


def test_leaderboard_sorted_by_csat_desc(tmp_path):
    client, store = _make_lb_app(tmp_path)
    # alice: CSAT 4.8, bob: CSAT 3.2
    store.update_conv_meta("ca", platform="tg")
    store.update_conv_csat("ca", 4.8)
    store.record_draft_audit("d1:ca", autopilot_level="L3", action="approved", agent_id="alice", conversation_id="ca")
    store.update_conv_meta("cb", platform="tg")
    store.update_conv_csat("cb", 3.2)
    store.record_draft_audit("d2:cb", autopilot_level="L3", action="approved", agent_id="bob", conversation_id="cb")
    r = client.get("/api/workspace/leaderboard?period=weekly")
    assert r.status_code == 200
    lb = r.json()["leaderboard"]
    assert len(lb) >= 2
    assert lb[0]["agent_id"] == "alice"
    assert lb[1]["agent_id"] == "bob"


def test_leaderboard_rank_and_badge(tmp_path):
    client, store = _make_lb_app(tmp_path)
    for i, (aid, csat_v) in enumerate([("p1", 5.0), ("p2", 4.0), ("p3", 3.0)]):
        cid = f"c_{aid}"
        store.update_conv_meta(cid, platform="tg")
        store.update_conv_csat(cid, csat_v)
        store.record_draft_audit(f"dr_{aid}:{cid}", autopilot_level="L3", action="approved", agent_id=aid, conversation_id=cid)
    r = client.get("/api/workspace/leaderboard")
    lb = r.json()["leaderboard"]
    assert lb[0]["rank"] == 1
    assert lb[0]["badge"] == "🏆"
    assert lb[1]["rank"] == 2
    assert lb[1]["badge"] == "🥈"
    assert lb[2]["rank"] == 3
    assert lb[2]["badge"] == "🥉"


def test_leaderboard_csat_stars_field(tmp_path):
    client, store = _make_lb_app(tmp_path)
    store.update_conv_meta("cx", platform="tg")
    store.update_conv_csat("cx", 4.0)
    store.record_draft_audit("drx:cx", autopilot_level="L3", action="approved", agent_id="alice_s", conversation_id="cx")
    r = client.get("/api/workspace/leaderboard")
    lb = r.json()["leaderboard"]
    alice_entry = next((e for e in lb if e["agent_id"] == "alice_s"), None)
    assert alice_entry is not None
    assert "csat_stars" in alice_entry
    assert "⭐" in alice_entry["csat_stars"]


def test_leaderboard_limit(tmp_path):
    client, store = _make_lb_app(tmp_path)
    for i in range(5):
        cid = f"lim_c{i}"
        store.update_conv_meta(cid, platform="tg")
        store.record_draft_audit(f"dr_{i}:{cid}", autopilot_level="L3", action="approved", agent_id=f"agent_{i}", conversation_id=cid)
    r = client.get("/api/workspace/leaderboard?limit=3")
    lb = r.json()["leaderboard"]
    assert len(lb) <= 3


def test_leaderboard_period_monthly(tmp_path):
    client, _ = _make_lb_app(tmp_path)
    r = client.get("/api/workspace/leaderboard?period=monthly")
    assert r.status_code == 200
    assert r.json()["period"] == "monthly"


def test_leaderboard_in_inventory(app):
    """确保 /api/workspace/leaderboard 在 admin app 路由表中。"""
    routes = {r.path for r in app.routes}
    assert "/api/workspace/leaderboard" in routes, f"missing leaderboard route, got: {sorted(routes)}"
