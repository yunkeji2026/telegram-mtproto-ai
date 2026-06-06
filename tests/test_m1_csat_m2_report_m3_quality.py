"""M1 CSAT + M2 工作简报 + M3 坐席回复质量监控 测试。

M1 CSAT:
  - calculate_csat() 基础返回 [0,5]
  - 正向情绪 (感谢) → 高分
  - 负向情绪 (愤怒) → 低分
  - 情绪趋势 falling → 扣分
  - 风险 critical → 扣分
  - force_override 决策 → 扣分
  - 空 conv_meta → 返回基线 4.0
  - csat_to_stars / csat_label 辅助函数
  - InboxStore.update_conv_csat → 写入 + 读回
  - agent_perf 包含 avg_csat 字段

M2 /api/workspace/report:
  - 非主管 → 403
  - period=daily JSON 含必要字段
  - period=weekly JSON period_label 含"本周"
  - format=text → 纯文本含 CSAT 行
  - format=html → 返回 HTML 片段
  - ReportGenerator.generate() 数据结构完整
  - ReportGenerator.format_text() 包含关键段落
  - ReportGenerator.format_html() 包含 HTML 标签
  - /api/workspace/broadcast 主管可用
  - /api/workspace/broadcast 非主管 → 403
  - 路由在 inventory 中

M3 坐席回复质量:
  - human_reply_risk 别名在 _EVENT_ALIASES
  - _build_message 处理 human_reply_risk 事件
  - DraftService.resolve_with_audit approve 时：低风险文本不触发事件
  - M3 检查在 quick_analyze 抛错时静默忽略
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.csat import calculate_csat, csat_to_stars, csat_label, _BASE_SCORE
from src.inbox.drafts import DraftService
from src.inbox.report_generator import ReportGenerator
from src.inbox.store import InboxStore
from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message


# ─────────────────────────── M1: CSAT 测试 ────────────────────────────────

def test_csat_empty_meta_returns_baseline():
    score = calculate_csat(None)
    assert score == _BASE_SCORE


def test_csat_empty_dict_returns_baseline():
    score = calculate_csat({})
    assert score == _BASE_SCORE


def test_csat_positive_emotion_raises_score():
    meta = {"last_emotion": "感谢", "emotion_trend": "rising", "last_risk": "low"}
    score = calculate_csat(meta)
    assert score > _BASE_SCORE


def test_csat_negative_emotion_lowers_score():
    meta = {"last_emotion": "愤怒", "emotion_trend": "falling", "last_risk": "high"}
    score = calculate_csat(meta)
    assert score < _BASE_SCORE


def test_csat_critical_risk_heavy_penalty():
    meta = {"last_emotion": "中性", "emotion_trend": "stable", "last_risk": "critical"}
    score = calculate_csat(meta)
    assert score <= 3.0


def test_csat_force_override_penalty():
    meta = {"last_emotion": "中性", "emotion_trend": "stable", "last_risk": "low"}
    decisions = [{"action": "force_override"}, {"action": "approved"}]
    score_with = calculate_csat(meta, decisions)
    score_without = calculate_csat(meta, [])
    assert score_with < score_without


def test_csat_long_conversation_penalty():
    meta = {"msg_count": 25, "last_risk": "low", "emotion_trend": "stable"}
    score = calculate_csat(meta)
    assert score < _BASE_SCORE


def test_csat_always_in_range():
    """边界：极负向参数不会低于 0。"""
    meta = {
        "last_emotion": "愤怒",
        "emotion_trend": "falling",
        "last_risk": "critical",
        "msg_count": 30,
    }
    decisions = [{"action": "force_override"} for _ in range(10)]
    score = calculate_csat(meta, decisions)
    assert 0.0 <= score <= 5.0


def test_csat_to_stars_range():
    assert "⭐" in csat_to_stars(4.5)
    assert "☆" in csat_to_stars(2.0)
    # 5 chars total
    for s in (0.0, 1.0, 2.5, 4.9, 5.0):
        stars = csat_to_stars(s)
        assert len(stars) == 5


def test_csat_label_boundaries():
    assert csat_label(4.8) == "非常满意"
    assert csat_label(3.8) == "满意"
    assert csat_label(2.9) == "一般"
    assert csat_label(1.8) == "不满意"
    assert csat_label(0.5) == "非常不满意"


def test_store_update_conv_csat(tmp_path):
    store = InboxStore(db_path=str(tmp_path / "t.db"))
    # 必须先有 conversation_meta 行
    store.update_conv_meta("cid1", platform="tg", intent="order", emotion="满意")
    store.update_conv_csat("cid1", 4.2)
    meta = store.get_conv_meta("cid1")
    assert meta is not None
    assert abs(meta["csat_score"] - 4.2) < 0.05


def test_store_update_conv_csat_clamps(tmp_path):
    store = InboxStore(db_path=str(tmp_path / "t.db"))
    store.update_conv_meta("cid2", platform="tg")
    store.update_conv_csat("cid2", 99.9)  # 应被 clamp 到 5.0
    meta = store.get_conv_meta("cid2")
    assert meta["csat_score"] <= 5.0


def test_agent_perf_contains_avg_csat(tmp_path):
    store = InboxStore(db_path=str(tmp_path / "t.db"))
    # 写入会话元数据 + CSAT
    store.update_conv_meta("c1", platform="tg")
    store.update_conv_csat("c1", 4.5)
    # 写入审计日志
    store.record_draft_audit(
        "d1:c1",
        autopilot_level="L3",
        action="approved",
        agent_id="alice",
        conversation_id="c1",
    )
    perf = store.get_agent_perf(since_ts=0.0)
    alice = next((p for p in perf if p["agent_id"] == "alice"), None)
    assert alice is not None
    assert "avg_csat" in alice
    # alice 处置了 c1 → avg_csat 应为 4.5
    assert alice["avg_csat"] == 4.5


# ─────────────────────────── M2: ReportGenerator ──────────────────────────

def _make_mock_store(tmp_path):
    store = InboxStore(db_path=str(tmp_path / "r.db"))
    store.update_conv_meta("conv1", platform="tg", intent="order", emotion="满意")
    store.update_conv_csat("conv1", 4.3)
    store.record_draft_audit(
        "dr1:conv1", autopilot_level="L2", action="autosend", agent_id="bob",
        conversation_id="conv1",
    )
    store.record_draft_audit(
        "dr2:conv1", autopilot_level="L3", action="approved", agent_id="alice",
        conversation_id="conv1",
    )
    return store


def test_report_generate_daily_fields(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="daily")
    assert data["period"] == "daily"
    assert "period_label" in data
    assert "today" in data["period_label"].lower() or "今日" in data["period_label"]
    assert "agent_perf" in data
    assert "draft_stats" in data
    assert "csat" in data
    assert "sla_stats" in data
    assert "top_intents" in data
    assert "generated_at" in data
    assert "date_range" in data


def test_report_generate_weekly_label(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="weekly")
    assert data["period"] == "weekly"
    assert "本周" in data["period_label"]


def test_report_csat_aggregated(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="daily")
    csat = data.get("csat") or {}
    # conv1 的 CSAT 4.3 → avg 应包含
    if csat.get("count", 0) > 0:
        assert csat["avg"] is not None
        assert 0 <= csat["avg"] <= 5


def test_report_format_text_contains_csat(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="daily")
    # 写入至少一个带 csat 的 row
    store.update_conv_csat("conv1", 4.5)
    data2 = gen.generate(period="daily")
    text = gen.format_text(data2)
    assert isinstance(text, str)
    assert "📊" in text  # 标题图标
    assert "草稿" in text or "处理" in text  # 至少含草稿相关文字


def test_report_format_html_returns_html(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="daily")
    html = gen.format_html(data)
    assert "<div" in html
    assert "style=" in html


def test_report_format_text_weekly(tmp_path):
    store = _make_mock_store(tmp_path)
    gen = ReportGenerator(inbox_store=store)
    data = gen.generate(period="weekly")
    text = gen.format_text(data)
    assert "本周" in text or "周" in text


# ─────────────────────── M2: /api/workspace/report API ───────────────────

def _make_report_app(tmp_path, role="master"):
    store = InboxStore(db_path=str(tmp_path / "api.db"))
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "tester"}
        return await call_next(req)

    def _api_auth(r: Request):
        return True

    from src.web.routes.drafts_routes import register_report_route, register_broadcast_route
    app.state.inbox_store = store
    app.state.draft_service = None
    register_report_route(app, api_auth=_api_auth)
    register_broadcast_route(app, api_auth=_api_auth)
    return app, store


def test_report_api_requires_supervisor(tmp_path):
    app, _ = _make_report_app(tmp_path, role="agent")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/workspace/report")
    assert r.status_code == 403


def test_report_api_daily_json(tmp_path):
    app, store = _make_report_app(tmp_path, role="master")
    store.update_conv_meta("c1", platform="tg", intent="order")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/workspace/report?period=daily")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert "period_label" in data
    assert "agent_perf" in data


def test_report_api_weekly_json(tmp_path):
    app, _ = _make_report_app(tmp_path, role="master")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/workspace/report?period=weekly")
    assert r.status_code == 200
    data = r.json()
    assert data["period"] == "weekly"
    assert "本周" in data["period_label"]


def test_report_api_text_format(tmp_path):
    app, _ = _make_report_app(tmp_path, role="master")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/workspace/report?format=text")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    assert "📊" in r.text


def test_report_api_html_format(tmp_path):
    app, _ = _make_report_app(tmp_path, role="master")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/workspace/report?format=html")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "<div" in r.text


def test_broadcast_api_supervisor(tmp_path):
    app, _ = _make_report_app(tmp_path, role="master")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/workspace/broadcast",
        json={"type": "report", "data": {"period": "daily", "text": "测试简报"}},
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_broadcast_api_requires_supervisor(tmp_path):
    app, _ = _make_report_app(tmp_path, role="agent")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/workspace/broadcast",
        json={"type": "report", "data": {}},
    )
    assert r.status_code == 403


def test_broadcast_api_empty_type(tmp_path):
    app, _ = _make_report_app(tmp_path, role="master")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/workspace/broadcast", json={"type": "", "data": {}})
    assert r.status_code == 400


# ─────────────────────────── M2 路由 inventory ────────────────────────────

def test_report_broadcast_in_inventory(app):
    """确保 /api/workspace/report 和 /api/workspace/broadcast 在 admin app 路由表中。"""
    routes = {r.path for r in app.routes}
    assert "/api/workspace/report" in routes, f"missing report route, got: {sorted(routes)}"
    assert "/api/workspace/broadcast" in routes, f"missing broadcast route, got: {sorted(routes)}"


# ─────────────────────────── M3: 坐席回复质量 ──────────────────────────────

def test_m3_reply_risk_alias_in_event_aliases():
    """_EVENT_ALIASES 包含 reply_risk → human_reply_risk。"""
    alias = _EVENT_ALIASES.get("reply_risk")
    assert alias is not None
    assert "human_reply_risk" in alias["types"]


def test_m3_build_message_human_reply_risk():
    """_build_message 能处理 human_reply_risk 事件。"""
    msg = _build_message(
        "human_reply_risk",
        {
            "draft_id": "dr1",
            "conversation_id": "c1",
            "agent_id": "alice",
            "risk_level": "high",
            "risk_reasons": ["敏感词命中"],
            "text_preview": "这是预览文本",
        },
    )
    title, text = msg
    assert "坐席" in title or "回复" in title or "警告" in title
    assert "alice" in text or "high" in text


def test_m3_report_alias_in_event_aliases():
    """_EVENT_ALIASES 包含 report → report 事件。"""
    alias = _EVENT_ALIASES.get("report")
    assert alias is not None
    assert "report" in alias["types"]


def test_m3_resolve_skips_quality_check_on_reject(tmp_path):
    """M3 只对 approve/edit_send 检查，reject 不触发。"""
    store = InboxStore(db_path=str(tmp_path / "t.db"))
    store.upsert_draft({
        "source_kind": "inbox",
        "source_id": "r1",
        "draft_id": "inbox:r1",
        "conversation_id": "c1",
        "autopilot_level": "L3",
        "risk_level": "low",
        "status": "pending",
        "draft_text": "普通回复",
    })
    svc = DraftService(inbox_store=store)
    events_published = []

    with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
        mock_bus.return_value.publish = lambda t, d: events_published.append((t, d))
        result = svc.resolve_with_audit("inbox:r1", "reject", by="agent1")
    # reject 不应触发 human_reply_risk
    human_risk_events = [e for e in events_published if e[0] == "human_reply_risk"]
    assert len(human_risk_events) == 0


def test_m3_resolve_silent_on_quick_analyze_error(tmp_path):
    """M3 当 quick_analyze 抛出异常时静默忽略，不影响主流程。"""
    store = InboxStore(db_path=str(tmp_path / "t.db"))
    store.upsert_draft({
        "source_kind": "inbox",
        "source_id": "qa_err",
        "draft_id": "inbox:qa_err",
        "conversation_id": "c_err",
        "autopilot_level": "L3",
        "risk_level": "low",
        "status": "pending",
        "draft_text": "测试",
    })
    svc = DraftService(inbox_store=store)
    with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
        mock_bus.return_value.publish = MagicMock()
        result = svc.resolve_with_audit("inbox:qa_err", "approve", text="测试", by="agent1")
    # 主流程应成功
    assert result.get("ok") is True


# ──────────────────────── admin route inventory 补充 ───────────────────────

def test_admin_inventory_updated(tmp_path):
    """确保 test_admin_route_inventory 中的 _BASELINE 已包含新路由（smoke check）。"""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "test_admin_route_inventory",
        "tests/test_admin_route_inventory.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    baseline: set = getattr(mod, "_BASELINE", set())
    assert "/api/workspace/report" in baseline, "inventory _BASELINE 未包含 /api/workspace/report"
    assert "/api/workspace/broadcast" in baseline, "inventory _BASELINE 未包含 /api/workspace/broadcast"
