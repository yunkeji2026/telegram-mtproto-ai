"""
R1 (智能问候触发器) + R2 (坐席工作负荷均衡) + R3 (CSAT问卷) 测试套件
"""
from __future__ import annotations

import asyncio
import time
import uuid
import pytest
from unittest.mock import patch, MagicMock

from starlette.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI
from starlette.requests import Request


# ─── helpers ────────────────────────────────────────────────────

def _fresh_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(str(tmp_path / f"test_{uuid.uuid4().hex[:6]}.db"))


def _session_mw(role="master", user_id="sup1"):
    class Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.scope["session"] = {"role": role, "user_id": user_id}
            return await call_next(request)
    return Mw


def _make_api_auth():
    async def _auth(req: Request): return None
    return _auth


# ══════════════════════════════════════════════════════════════════
# R1: 智能问候触发器
# ══════════════════════════════════════════════════════════════════

class TestR1Greeting:
    def test_get_time_slot_morning(self):
        from src.inbox.greeting import get_time_slot
        assert get_time_slot(8) == "morning"
        assert get_time_slot(11) == "morning"

    def test_get_time_slot_afternoon(self):
        from src.inbox.greeting import get_time_slot
        assert get_time_slot(12) == "afternoon"
        assert get_time_slot(17) == "afternoon"

    def test_get_time_slot_evening(self):
        from src.inbox.greeting import get_time_slot
        assert get_time_slot(18) == "evening"
        assert get_time_slot(21) == "evening"

    def test_get_time_slot_night(self):
        from src.inbox.greeting import get_time_slot
        assert get_time_slot(0) == "night"
        assert get_time_slot(5) == "night"
        assert get_time_slot(23) == "night"

    def test_select_greeting_text_zh_morning(self):
        from src.inbox.greeting import select_greeting_text
        text = select_greeting_text("zh", "morning")
        assert text
        assert "早" in text or "好" in text

    def test_select_greeting_text_en_evening(self):
        from src.inbox.greeting import select_greeting_text
        text = select_greeting_text("en", "evening")
        assert text
        assert "evening" in text.lower() or "good" in text.lower()

    def test_select_greeting_text_unknown_lang_fallback(self):
        from src.inbox.greeting import select_greeting_text
        # 未知语言应回退到中文
        text = select_greeting_text("xx", "morning")
        assert text  # 不应为空

    def test_should_auto_greet_new_conv(self):
        from src.inbox.greeting import should_auto_greet
        assert should_auto_greet(None) is True  # 全新会话

    def test_should_auto_greet_first_message(self):
        from src.inbox.greeting import should_auto_greet
        meta = {"msg_count": 1, "intent_history": []}
        assert should_auto_greet(meta) is True

    def test_should_not_greet_existing_conv(self):
        from src.inbox.greeting import should_auto_greet
        meta = {"msg_count": 5, "intent_history": ["退款", "物流"]}
        assert should_auto_greet(meta) is False

    def test_should_not_greet_when_disabled(self):
        from src.inbox.greeting import should_auto_greet
        assert should_auto_greet(None, enabled=False) is False

    def test_build_greeting_draft_structure(self):
        from src.inbox.greeting import build_greeting_draft
        conv = {
            "conversation_id": "c1",
            "platform": "telegram",
            "account_id": "acc1",
            "chat_key": "tg:123",
        }
        draft = build_greeting_draft(conv, "zh", time_slot="morning")
        assert draft["conversation_id"] == "c1"
        assert draft["source_kind"] == "inbox"
        assert "greet_" in draft["source_id"]  # 幂等键
        assert draft["risk_level"] == "low"
        assert "早" in draft["draft_text"] or "好" in draft["draft_text"]

    def test_build_greeting_draft_idempotent_source_id(self):
        from src.inbox.greeting import build_greeting_draft
        conv = {"conversation_id": "cX"}
        d1 = build_greeting_draft(conv, "zh")
        d2 = build_greeting_draft(conv, "zh")
        assert d1["source_id"] == d2["source_id"]  # 同一会话 source_id 相同

    def test_greeting_from_template_store(self, tmp_path):
        """R1：优先从 reply_templates 库检索问候模板。"""
        from src.inbox.greeting import select_greeting_text
        store = _fresh_store(tmp_path)
        store.create_template(
            title="早安问候",
            content="早上好！欢迎联系我们，请问有什么可以帮您？",
            scene="greeting",
            language="zh",
        )
        text = select_greeting_text("zh", "morning", templates_store=store)
        assert "早上好" in text  # 应优先使用模板库中的问候语

    def test_auto_generate_draft_triggers_greeting(self, tmp_path):
        """R1：auto_generate_draft 在第一条消息时生成问候草稿。"""
        from src.inbox.drafts import DraftService
        store = _fresh_store(tmp_path)
        cfg = {"auto_greeting": {"enabled": True}}
        svc = DraftService(inbox_store=store, cfg=cfg)
        conv = {
            "conversation_id": "cgreet",
            "platform": "telegram",
            "account_id": "default",
            "chat_key": "tg:999",
            "display_name": "测试用户",
        }
        draft_id = svc.auto_generate_draft(conv, "hello")
        assert draft_id is not None
        draft = store.get_draft(draft_id)
        assert draft is not None
        # 问候草稿应包含问候语（中英文皆可）
        text = draft.get("draft_text") or ""
        assert "good" in text.lower() or "hello" in text.lower() or "thank" in text.lower() or "好" in text

    def test_greeting_disabled_by_config(self, tmp_path):
        """R1：auto_greeting.enabled=False 时不触发问候，走普通草稿生成。"""
        from src.inbox.drafts import DraftService
        store = _fresh_store(tmp_path)
        cfg = {"auto_greeting": {"enabled": False}}
        svc = DraftService(inbox_store=store, cfg=cfg)
        conv = {"conversation_id": "cno_greet", "platform": "tg"}
        draft_id = svc.auto_generate_draft(conv, "您好请问怎么退款")
        # 应返回普通草稿（不是问候草稿）
        if draft_id:
            draft = store.get_draft(draft_id)
            # source_id 不应是 greet_ 开头（正常草稿 source_id 为 conv_id）
            assert not str(draft.get("source_id") or "").startswith("greet_")


# ══════════════════════════════════════════════════════════════════
# R2: 坐席工作负荷均衡
# ══════════════════════════════════════════════════════════════════

class TestR2WorkloadStore:
    def test_get_agent_workload_empty(self, tmp_path):
        store = _fresh_store(tmp_path)
        wl = store.get_agent_workload("agent_x")
        assert wl["agent_id"] == "agent_x"
        assert wl["active_convs"] == 0
        assert wl["recent_actions"] == 0

    def test_get_agent_workload_with_claims(self, tmp_path):
        store = _fresh_store(tmp_path)
        # 添加 conversation_claims
        now = time.time()
        store.upsert_agent_presence("a1", status="online")
        with store._conn as c:
            c.execute(
                "INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                ("conv1", "a1", now, now + 3600),
            )
            c.execute(
                "INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                ("conv2", "a1", now, now + 3600),
            )
        wl = store.get_agent_workload("a1")
        assert wl["active_convs"] == 2
        assert wl["status"] == "online"

    def test_list_agent_workloads_sorted_asc(self, tmp_path):
        store = _fresh_store(tmp_path)
        now = time.time()
        for aid in ["a1", "a2", "a3"]:
            store.upsert_agent_presence(aid, status="online")
        # a3 has 2 claims, a1 has 0
        with store._conn as c:
            c.execute("INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                      ("c1", "a3", now, now + 3600))
            c.execute("INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                      ("c2", "a3", now, now + 3600))
        wls = store.list_agent_workloads()
        # Should be sorted by active_convs ascending
        convs = [w["active_convs"] for w in wls]
        assert convs == sorted(convs)

    def test_get_lightest_agent(self, tmp_path):
        store = _fresh_store(tmp_path)
        now = time.time()
        for aid in ["b1", "b2"]:
            store.upsert_agent_presence(aid, status="online")
        with store._conn as c:
            c.execute("INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                      ("conv_b2", "b2", now, now + 3600))
        lightest = store.get_lightest_agent()
        assert lightest == "b1"  # b1 has 0 active convs

    def test_get_lightest_agent_excludes_overloaded(self, tmp_path):
        store = _fresh_store(tmp_path)
        now = time.time()
        for aid in ["c1", "c2"]:
            store.upsert_agent_presence(aid, status="online")
        # Both have 3 claims each
        for ci in ["cv1", "cv2", "cv3"]:
            with store._conn as c:
                c.execute("INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                          (ci+"_c1", "c1", now, now + 3600))
                c.execute("INSERT OR REPLACE INTO conversation_claims (conversation_id, agent_id, claimed_at, expires_at) VALUES (?,?,?,?)",
                          (ci+"_c2", "c2", now, now + 3600))
        # max_load_cap=2 → both overloaded → None
        lightest = store.get_lightest_agent(max_load_cap=2)
        assert lightest is None  # all overloaded


def _make_workload_app(tmp_path, role="master"):
    from src.web.routes.drafts_routes import register_workload_route
    app = FastAPI()
    app.add_middleware(_session_mw(role=role))
    store = _fresh_store(tmp_path)
    store.upsert_agent_presence("w_agent1", status="online")
    store.upsert_agent_presence("w_agent2", status="busy")
    app.state.inbox_store = store
    app.state.cfg = {}
    register_workload_route(app, api_auth=_make_api_auth())
    return app, store


class TestR2WorkloadAPI:
    def test_workload_list_200(self, tmp_path):
        app, _ = _make_workload_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workload")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "workloads" in d
        assert "total_agents" in d

    def test_workload_403_agent(self, tmp_path):
        app, _ = _make_workload_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/workspace/workload")
        assert r.status_code == 403

    def test_workload_single_agent(self, tmp_path):
        app, _ = _make_workload_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workload?agent_id=w_agent1")
        assert r.status_code == 200
        d = r.json()
        assert d["workload"]["agent_id"] == "w_agent1"

    def test_workload_includes_lightest_agent(self, tmp_path):
        app, _ = _make_workload_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workload")
        d = r.json()
        assert "lightest_agent" in d

    def test_workload_shows_overloaded_count(self, tmp_path):
        app, _ = _make_workload_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/workload")
        d = r.json()
        assert "overloaded_count" in d


# ══════════════════════════════════════════════════════════════════
# R3: CSAT 问卷系统
# ══════════════════════════════════════════════════════════════════

class TestR3SurveyStore:
    def test_schedule_and_list_due(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs1", platform="tg")
        store.schedule_csat_survey(
            survey_id="srv_001",
            conversation_id="cs1",
            draft_id="inbox:d1",
            agent_id="sup",
            delay_seconds=0,  # 立即到期
        )
        due = store.list_due_surveys()
        assert len(due) == 1
        assert due[0]["conversation_id"] == "cs1"

    def test_mark_survey_sent(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs2", platform="tg")
        store.schedule_csat_survey(survey_id="srv_002", conversation_id="cs2",
                                   draft_id="inbox:d2", agent_id="sup", delay_seconds=0)
        store.mark_survey_sent("srv_002")
        due = store.list_due_surveys()
        assert all(d["id"] != "srv_002" for d in due)

    def test_record_survey_response(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs3", platform="tg")
        store.schedule_csat_survey(survey_id="srv_003", conversation_id="cs3",
                                   draft_id="inbox:d3", agent_id="sup", delay_seconds=0)
        store.mark_survey_sent("srv_003")
        matched = store.record_survey_response("cs3", 4)
        assert matched is True
        meta = store.get_conv_meta("cs3")
        assert meta["csat_score"] == 4.0

    def test_record_survey_response_no_pending(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs4", platform="tg")
        matched = store.record_survey_response("cs4", 5)
        assert matched is False  # 没有待回复问卷

    def test_survey_awaiting_flag(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs5", platform="tg")
        assert store.is_survey_awaiting("cs5") is False
        store.set_conv_survey_awaiting("cs5", True)
        assert store.is_survey_awaiting("cs5") is True
        store.set_conv_survey_awaiting("cs5", False)
        assert store.is_survey_awaiting("cs5") is False

    def test_survey_response_score_clamped(self, tmp_path):
        """R3：score 自动 clamp 到 1-5 范围。"""
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs6", platform="tg")
        store.schedule_csat_survey(survey_id="srv_006", conversation_id="cs6",
                                   draft_id="inbox:d6", agent_id="sup", delay_seconds=0)
        store.mark_survey_sent("srv_006")
        store.record_survey_response("cs6", 10)  # 超过5
        meta = store.get_conv_meta("cs6")
        assert meta["csat_score"] <= 5.0

    def test_future_survey_not_due(self, tmp_path):
        """R3：delay_seconds > 0 的问卷不应出现在 list_due_surveys 里。"""
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cs7", platform="tg")
        store.schedule_csat_survey(survey_id="srv_007", conversation_id="cs7",
                                   draft_id="inbox:d7", agent_id="sup", delay_seconds=9999)
        due = store.list_due_surveys()
        assert all(d["id"] != "srv_007" for d in due)


class TestR3SurveyWorker:
    def test_worker_disabled_by_config(self):
        from src.inbox.survey_worker import SurveyWorker
        worker = SurveyWorker(MagicMock(), cfg={"workspace": {"csat_survey": {"enabled": False}}})
        assert worker.is_enabled() is False

    def test_worker_enabled_by_config(self):
        from src.inbox.survey_worker import SurveyWorker
        worker = SurveyWorker(MagicMock(), cfg={"workspace": {"csat_survey": {"enabled": True}}})
        assert worker.is_enabled() is True

    def test_worker_delay_minutes(self):
        from src.inbox.survey_worker import SurveyWorker
        worker = SurveyWorker(MagicMock(), cfg={"workspace": {"csat_survey": {"delay_minutes": 10}}})
        assert worker._delay_seconds() == 600.0

    def test_worker_status_snapshot(self):
        from src.inbox.survey_worker import SurveyWorker
        worker = SurveyWorker(MagicMock())
        snap = worker.status_snapshot()
        assert "enabled" in snap
        assert "total_sent" in snap
        assert "delay_minutes" in snap

    def test_worker_sends_due_surveys(self, tmp_path):
        """R3：SurveyWorker 处理到期问卷，标记为已发并设置 survey_awaiting。"""
        from src.inbox.survey_worker import SurveyWorker
        store = _fresh_store(tmp_path)
        store.update_conv_meta("sw_conv1", platform="tg")
        store.schedule_csat_survey(
            survey_id="sw_srv1", conversation_id="sw_conv1",
            draft_id="inbox:sw_d1", agent_id="sup", delay_seconds=0,
        )

        published = []
        class FakeEB:
            def publish(self, t, d): published.append((t, d))

        worker = SurveyWorker(
            store,
            cfg={"workspace": {"csat_survey": {"enabled": True, "delay_minutes": 0}}},
        )
        loop = asyncio.new_event_loop()
        try:
            with patch("src.integrations.shared.event_bus.get_event_bus", return_value=FakeEB()):
                loop.run_until_complete(worker._send_due_surveys())
        finally:
            loop.close()

        assert worker._total_sent == 1
        assert store.is_survey_awaiting("sw_conv1") is True
        survey_events = [e for t, e in published if t == "survey_sent"]
        assert len(survey_events) == 1
        assert survey_events[0]["survey_id"] == "sw_srv1"

    def test_get_survey_message_multilang(self):
        from src.inbox.survey_worker import get_survey_message
        zh = get_survey_message("zh")
        en = get_survey_message("en")
        assert "满意" in zh
        assert "satisfied" in en.lower()


class TestR3IngestIntegration:
    def test_ingest_parses_survey_response(self, tmp_path):
        """R3：当会话 survey_awaiting=True 且客户发 1-5 数字，自动更新 CSAT。"""
        from src.inbox.ingest import ingest_collected_chats
        store = _fresh_store(tmp_path)
        store.update_conv_meta("ingest_sv_conv", platform="telegram")
        store.schedule_csat_survey(
            survey_id="isrv1", conversation_id="ingest_sv_conv",
            draft_id="inbox:isd1", agent_id="sup", delay_seconds=0,
        )
        store.mark_survey_sent("isrv1")
        store.set_conv_survey_awaiting("ingest_sv_conv", True)

        chat = {
            "conversation_id": "ingest_sv_conv",
            "platform": "telegram",
            "account_id": "default",
            "chat_key": "tg:999",
            "display_name": "User",
            "last_message": {"text": "4", "direction": "in", "message_id": "msg_csat_1", "ts": time.time()},
        }
        ingest_collected_chats(store, [chat], publish_events=False)

        meta = store.get_conv_meta("ingest_sv_conv")
        assert meta["csat_score"] == 4.0
        assert store.is_survey_awaiting("ingest_sv_conv") is False

    def test_ingest_ignores_non_survey_number(self, tmp_path):
        """R3：无 survey_awaiting 时数字不被误识别为问卷回复。"""
        from src.inbox.ingest import ingest_collected_chats
        store = _fresh_store(tmp_path)
        store.update_conv_meta("normal_conv", platform="telegram")

        chat = {
            "conversation_id": "normal_conv",
            "platform": "telegram",
            "account_id": "default",
            "chat_key": "tg:111",
            "display_name": "User",
            "last_message": {"text": "3", "direction": "in", "message_id": "msg_normal", "ts": time.time()},
        }
        ingest_collected_chats(store, [chat], publish_events=False)
        meta = store.get_conv_meta("normal_conv")
        # csat_score 应仍为 -1（未评分）
        assert float(meta.get("csat_score") or -1) == -1.0
