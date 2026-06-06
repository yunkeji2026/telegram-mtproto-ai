"""
Q1 (对话摘要自动归档) + Q2 (草稿质量评分) + Q3 (KB命中率监控) 测试套件
"""
from __future__ import annotations

import time
import uuid
import pytest
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI
from starlette.requests import Request


# ─── helpers ────────────────────────────────────────────────────

def _fresh_store(tmp_path):
    from src.inbox.store import InboxStore
    return InboxStore(str(tmp_path / f"test_{uuid.uuid4().hex[:6]}.db"))


def _session_mw(role="master", user_id="tester"):
    class Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.scope["session"] = {"role": role, "user_id": user_id}
            return await call_next(request)
    return Mw


def _make_api_auth():
    async def _auth(request: Request): return None
    return _auth


# ══════════════════════════════════════════════════════════════════
# Q1: 对话摘要生成测试
# ══════════════════════════════════════════════════════════════════

class TestQ1SummaryFunction:
    def test_basic_summary_generated(self):
        from src.inbox.summary import generate_conv_summary
        s = generate_conv_summary(
            conv_meta={
                "last_emotion": "angry",
                "last_intent": "退款",
                "msg_count": 5,
                "csat_score": 3.0,
            },
            action="approve",
            agent_id="agent001",
            sent_text="非常抱歉给您带来困扰，退款将在3个工作日内到账。",
            created_ts=time.time() - 300,
        )
        assert s
        assert "愤怒" in s or "angry" in s.lower() or "😡" in s
        assert "approve" in s or "批准" in s
        assert "agent001" in s

    def test_summary_contains_csat(self):
        from src.inbox.summary import generate_conv_summary
        s = generate_conv_summary(
            conv_meta={"last_emotion": "happy", "csat_score": 4.5},
            action="autosend",
            agent_id="bot",
        )
        assert "4.5" in s or "⭐" in s

    def test_summary_no_csat_when_unscored(self):
        from src.inbox.summary import generate_conv_summary
        s = generate_conv_summary(
            conv_meta={"last_emotion": "neutral", "csat_score": -1},
            action="approve",
            agent_id="sup",
        )
        # -1 说明未评分，不应出现在摘要中
        assert "-1" not in s

    def test_summary_includes_time_elapsed(self):
        from src.inbox.summary import generate_conv_summary
        s = generate_conv_summary(
            conv_meta={"last_emotion": "neutral"},
            action="approve",
            agent_id="sup",
            created_ts=time.time() - 200,  # 200s ago
        )
        # 耗时字段应被计算
        assert "s" in s or "min" in s or "h" in s

    def test_summary_high_risk_prefix(self):
        from src.inbox.summary import generate_conv_summary
        s = generate_conv_summary(
            conv_meta={"last_emotion": "angry", "last_risk": "high"},
            action="force_override",
            agent_id="sup",
        )
        assert "高风险" in s or "【" in s

    def test_summary_includes_reply_preview(self):
        from src.inbox.summary import generate_conv_summary
        sent = "您好，您的订单已成功发货，预计明天到达。"
        s = generate_conv_summary(
            conv_meta={"last_emotion": "neutral"},
            action="approve",
            agent_id="sup",
            sent_text=sent,
        )
        assert sent[:20] in s or "订单" in s

    def test_enrich_adds_intent_flow(self):
        from src.inbox.summary import generate_conv_summary, enrich_summary_with_history
        base = generate_conv_summary(
            conv_meta={"last_emotion": "neutral"},
            action="approve",
            agent_id="sup",
        )
        enriched = enrich_summary_with_history(
            base,
            intent_history=["问候", "退款", "物流查询"],
            emotion_history=["happy", "angry", "neutral"],
        )
        assert "意图流转" in enriched
        assert "退款" in enriched

    def test_enrich_no_change_single_intent(self):
        from src.inbox.summary import generate_conv_summary, enrich_summary_with_history
        base = generate_conv_summary(
            conv_meta={"last_emotion": "neutral"},
            action="approve",
            agent_id="sup",
        )
        enriched = enrich_summary_with_history(base, ["退款"], ["neutral"])
        assert enriched == base  # 单意图不追加


class TestQ1StoreMethod:
    def test_update_conv_summary_writes(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("conv_s1", platform="tg")
        store.update_conv_summary("conv_s1", "这是一段测试摘要。")
        meta = store.get_conv_meta("conv_s1")
        assert meta is not None
        assert "测试摘要" in (meta.get("summary") or "")

    def test_update_conv_summary_empty_id_noop(self, tmp_path):
        store = _fresh_store(tmp_path)
        # 不应抛出异常
        store.update_conv_summary("", "summary")

    def test_update_conv_summary_overwrites(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.update_conv_meta("conv_s2", platform="tg")
        store.update_conv_summary("conv_s2", "第一版摘要")
        store.update_conv_summary("conv_s2", "第二版摘要（更新）")
        meta = store.get_conv_meta("conv_s2")
        assert "第二版" in (meta.get("summary") or "")

    def test_contact_profile_includes_summary(self, tmp_path):
        """Q1：/api/unified-inbox/contact-profile 返回 conv_summary 字段。"""
        from fastapi import FastAPI
        from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

        app = FastAPI()
        app.add_middleware(_session_mw(role="master"))
        store = _fresh_store(tmp_path)
        store.update_conv_meta("cq1", platform="tg")
        store.update_conv_summary("cq1", "这是自动生成的摘要。")
        app.state.inbox_store = store

        from unittest.mock import MagicMock
        register_unified_inbox_routes(app, api_auth=_make_api_auth(),
                                      page_auth=_make_api_auth(),
                                      templates=MagicMock())
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/unified-inbox/contact-profile?conversation_id=cq1")
        assert r.status_code == 200
        d = r.json()
        assert "conv_summary" in d
        assert "摘要" in (d["conv_summary"] or "")


# ══════════════════════════════════════════════════════════════════
# Q2: 草稿质量评分测试
# ══════════════════════════════════════════════════════════════════

class TestQ2QualityFunction:
    def test_empty_text_scores_zero(self):
        from src.inbox.quality import calculate_draft_quality
        score, bd = calculate_draft_quality("")
        assert score == 0.0
        assert bd["grade"] == "🔴 待改进"

    def test_ideal_text_high_score(self):
        from src.inbox.quality import calculate_draft_quality
        text = "您好，感谢您的耐心等待！您的订单已经发货，预计明天上午到达，请您注意查收。如有任何问题请随时联系我们，祝您生活愉快！"
        score, bd = calculate_draft_quality(text, peer_text="我的包裹到哪了？", risk_level="low")
        assert score >= 60
        assert bd["grade"] in ("🟢 优秀", "🟡 良好")

    def test_short_text_low_score(self):
        from src.inbox.quality import calculate_draft_quality
        score, bd = calculate_draft_quality("好")
        assert score < 40

    def test_high_risk_no_soothe_penalty(self):
        from src.inbox.quality import calculate_draft_quality
        # 高风险但无安抚词 → risk_match 应低
        score_bad, bd_bad = calculate_draft_quality(
            "明天再说吧", peer_text="你们这是骗子！", risk_level="high"
        )
        score_good, bd_good = calculate_draft_quality(
            "非常抱歉给您造成困扰，我们立刻帮您核实并解决这个问题，请放心。",
            peer_text="你们这是骗子！",
            risk_level="high",
        )
        assert bd_good["risk_match"] > bd_bad["risk_match"]

    def test_lang_mismatch_penalty(self):
        from src.inbox.quality import calculate_draft_quality
        # 客户发中文，但草稿用英文回复 → lang_match 低
        _, bd_mismatch = calculate_draft_quality(
            "I'm sorry for the inconvenience.", peer_text="您好，我想查询订单。"
        )
        _, bd_match = calculate_draft_quality(
            "非常抱歉给您造成困扰。", peer_text="您好，我想查询订单。"
        )
        assert bd_match["lang_match"] >= bd_mismatch["lang_match"]

    def test_quality_badge_html(self):
        from src.inbox.quality import quality_to_badge
        badge = quality_to_badge(85.0)
        assert "🟢" in badge
        assert "85" in badge
        assert "<span" in badge

    def test_quality_badge_poor(self):
        from src.inbox.quality import quality_to_badge
        badge = quality_to_badge(25.0)
        assert "🔴" in badge


class TestQ2StoreMethod:
    def test_update_and_get_draft_quality(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.upsert_draft({"draft_id": "inbox:q2d", "source_kind": "inbox", "conversation_id": "c1"})
        store.update_draft_quality("inbox:q2d", 75.5, {"length": 25, "grade": "🟡 良好"})
        result = store.get_draft_quality("inbox:q2d")
        assert result is not None
        assert result["quality_score"] == 75.5
        assert result["breakdown"]["grade"] == "🟡 良好"

    def test_get_draft_quality_not_found(self, tmp_path):
        store = _fresh_store(tmp_path)
        result = store.get_draft_quality("nonexistent")
        assert result is None

    def test_list_draft_quality_stats_distribution(self, tmp_path):
        store = _fresh_store(tmp_path)
        for i, score in enumerate([85, 70, 55, 30, 90]):
            did = f"inbox:q2_{i}"
            store.upsert_draft({
                "draft_id": did, "source_kind": "inbox",
                "source_id": f"q2src_{i}", "conversation_id": f"c{i}"
            })
            store.update_draft_quality(did, score)
        stats = store.list_draft_quality_stats()
        assert stats["count"] == 5
        assert stats["excellent"] == 2  # 85, 90
        assert stats["good"] == 1       # 70
        assert stats["fair"] == 1       # 55
        assert stats["poor"] == 1       # 30
        assert stats["avg"] is not None

    def test_quality_stats_empty(self, tmp_path):
        store = _fresh_store(tmp_path)
        stats = store.list_draft_quality_stats()
        assert stats["count"] == 0
        assert stats["avg"] is None


def _make_quality_api_app(tmp_path, role="master"):
    from src.web.routes.drafts_routes import register_kb_stats_route
    app = FastAPI()
    app.add_middleware(_session_mw(role=role))
    store = _fresh_store(tmp_path)
    app.state.inbox_store = store
    register_kb_stats_route(app, api_auth=_make_api_auth())
    return app, store


class TestQ2QualityAPI:
    def test_quality_stats_200(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/quality-stats")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "count" in d

    def test_quality_stats_403_agent(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/workspace/quality-stats")
        assert r.status_code == 403

    def test_quality_stats_with_data(self, tmp_path):
        app, store = _make_quality_api_app(tmp_path)
        store.upsert_draft({"draft_id": "inbox:qd1", "source_kind": "inbox", "source_id": "qd1s", "conversation_id": "c1"})
        store.update_draft_quality("inbox:qd1", 82.0)
        client = TestClient(app)
        r = client.get("/api/workspace/quality-stats?days=7")
        d = r.json()
        assert d["count"] == 1
        assert d["excellent"] == 1


# ══════════════════════════════════════════════════════════════════
# Q3: KB 命中率监控测试
# ══════════════════════════════════════════════════════════════════

class TestQ3KbHitStore:
    def test_record_and_get_kb_stats(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.record_kb_recommendation(
            rec_id="r1", entry_id="e1", entry_title="退款流程",
            conversation_id="c1", agent_id="a1",
        )
        stats = store.get_kb_hit_stats()
        assert len(stats) == 1
        assert stats[0]["entry_id"] == "e1"
        assert stats[0]["recommended"] == 1
        assert stats[0]["clicked"] == 0
        assert stats[0]["hit_rate"] == 0.0

    def test_click_kb_recommendation(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.record_kb_recommendation(rec_id="r2", entry_id="e2", entry_title="物流查询")
        store.click_kb_recommendation(rec_id="r2", used_in_draft=True, draft_id="inbox:d1")
        stats = store.get_kb_hit_stats()
        assert stats[0]["clicked"] == 1
        assert stats[0]["used"] == 1
        assert stats[0]["hit_rate"] == 100.0
        assert stats[0]["use_rate"] == 100.0

    def test_hit_rate_calculation(self, tmp_path):
        store = _fresh_store(tmp_path)
        # 推荐 4 次，点击 2 次
        for i in range(4):
            store.record_kb_recommendation(rec_id=f"r{i}", entry_id="e3", entry_title="尺寸问题")
        store.click_kb_recommendation(rec_id="r0")
        store.click_kb_recommendation(rec_id="r1")
        stats = store.get_kb_hit_stats()
        e3 = next(s for s in stats if s["entry_id"] == "e3")
        assert e3["hit_rate"] == 50.0

    def test_multiple_entries_sorted_by_clicks(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.record_kb_recommendation(rec_id="ra1", entry_id="eA", entry_title="A")
        store.record_kb_recommendation(rec_id="rb1", entry_id="eB", entry_title="B")
        store.record_kb_recommendation(rec_id="rb2", entry_id="eB", entry_title="B")
        store.click_kb_recommendation(rec_id="rb1")
        store.click_kb_recommendation(rec_id="rb2")
        stats = store.get_kb_hit_stats()
        # eB 点击更多，应排在前面
        assert stats[0]["entry_id"] == "eB"

    def test_since_ts_filter(self, tmp_path):
        store = _fresh_store(tmp_path)
        # 写一条旧记录（手动写 past timestamp）
        store.record_kb_recommendation(rec_id="old_r", entry_id="eOLD", entry_title="Old")
        with store._conn as c:
            c.execute("UPDATE kb_recommendation_log SET recommended_ts=? WHERE id=?",
                      (time.time() - 10000, "old_r"))
        store.record_kb_recommendation(rec_id="new_r", entry_id="eNEW", entry_title="New")
        # since_ts = 5 min ago → 旧记录被过滤
        since = time.time() - 300
        stats = store.get_kb_hit_stats(since_ts=since)
        ids = [s["entry_id"] for s in stats]
        assert "eNEW" in ids
        assert "eOLD" not in ids


class TestQ3KbAPI:
    def test_kb_stats_200(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/kb-stats")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "entries" in d
        assert "low_hit_entries" in d

    def test_kb_stats_403_agent(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path, role="agent")
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/workspace/kb-stats")
        assert r.status_code == 403

    def test_kb_click_endpoint(self, tmp_path):
        app, store = _make_quality_api_app(tmp_path)
        store.record_kb_recommendation(rec_id="test_rec", entry_id="e1", entry_title="x")
        client = TestClient(app)
        r = client.post("/api/workspace/kb-click", json={
            "rec_id": "test_rec",
            "used_in_draft": True,
            "draft_id": "inbox:d1",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 验证点击已记录
        stats = store.get_kb_hit_stats()
        assert stats[0]["clicked"] == 1

    def test_kb_click_400_empty_rec_id(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/workspace/kb-click", json={"rec_id": ""})
        assert r.status_code == 400

    def test_kb_stats_low_hit_entries_threshold(self, tmp_path):
        """Q3：low_hit_entries 仅包含推荐>=3次且命中率<30%的条目。"""
        app, store = _make_quality_api_app(tmp_path)
        # 推荐 5 次，点击 0 次 → 命中率 0% → 应出现在 low_hit_entries
        for i in range(5):
            store.record_kb_recommendation(rec_id=f"low_r{i}", entry_id="eLOW", entry_title="低命中条目")
        client = TestClient(app)
        r = client.get("/api/workspace/kb-stats?days=7")
        d = r.json()
        low_ids = [e["entry_id"] for e in d["low_hit_entries"]]
        assert "eLOW" in low_ids

    def test_kb_stats_days_param(self, tmp_path):
        app, _ = _make_quality_api_app(tmp_path)
        client = TestClient(app)
        r = client.get("/api/workspace/kb-stats?days=30")
        assert r.status_code == 200
        assert r.json()["days"] == 30
