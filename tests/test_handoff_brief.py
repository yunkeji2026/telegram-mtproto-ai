"""M8 结构化转人工简报测试：build_handoff_brief 纯函数 + /api/workspace/handoff-brief 端点。"""

import time

from src.utils.handoff_brief import build_handoff_brief


def test_brief_assembles_profile_and_turns():
    meta = {
        "last_intent": "退款", "last_emotion": "愤怒", "emotion_trend": "rising",
        "last_risk": "high", "csat_score": 2, "summary": "客户要求退款被拒",
        "msg_count": 8,
    }
    msgs = [
        {"direction": "in", "text": "我要退款", "ts": 1},
        {"direction": "out", "text": "请稍等", "ts": 2},
        {"direction": "in", "text": "太慢了！", "ts": 3},
    ]
    b = build_handoff_brief("c1", meta, msgs, reason="risk_high", suggested_assignee="sup1")
    assert b["conversation_id"] == "c1"
    assert b["reason"] == "risk_high"
    assert b["suggested_assignee"] == "sup1"
    p = b["profile"]
    assert p["intent"] == "退款" and p["risk"] == "high" and p["csat"] == 2
    assert len(b["recent_turns"]) == 3
    assert b["recent_turns"][0]["who"] == "客户"
    assert b["recent_turns"][1]["who"] == "客服"
    # 高风险 + 负面情绪 + 上升 + 低 CSAT + 意图 都进 highlights
    joined = " ".join(b["highlights"])
    assert "高风险" in joined and "情绪负面" in joined and "上升" in joined
    assert "满意度偏低" in joined and "退款" in joined


def test_brief_truncates_to_max_turns():
    msgs = [{"direction": "in", "text": f"m{i}", "ts": i} for i in range(20)]
    b = build_handoff_brief("c", None, msgs, max_turns=5)
    assert len(b["recent_turns"]) == 5
    assert b["recent_turns"][-1]["text"] == "m19"   # 取最近 5 条


def test_brief_graceful_without_meta():
    b = build_handoff_brief("c", None, None)
    assert b["ok"] is True
    assert b["profile"]["intent"] == ""
    assert b["profile"]["csat"] is None
    assert b["recent_turns"] == []
    assert any("常规接手" in h for h in b["highlights"])


def test_brief_skips_empty_text():
    msgs = [
        {"direction": "in", "text": "  ", "ts": 1},
        {"direction": "in", "text": "real", "ts": 2},
    ]
    b = build_handoff_brief("c", {}, msgs)
    assert len(b["recent_turns"]) == 1 and b["recent_turns"][0]["text"] == "real"


def test_handoff_brief_endpoint(tmp_path):
    """端点：写入会话元数据 + 消息后，简报端点返回结构化画像。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.inbox.store import InboxStore
    from src.inbox.models import InboxConversation, InboxMessage
    from src.web.routes.unified_inbox_workspace_escalation_routes import (
        register_workspace_escalation_routes,
    )

    store = InboxStore(tmp_path / "inbox.db")
    cid = "tg:acc:peer"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="acc", chat_key="peer"))
    store.ingest_message(InboxMessage(
        conversation_id=cid, direction="in", text="你好我要咨询", ts=time.time()))
    store.update_conv_meta(cid, intent="咨询", emotion="平静", risk="low")

    app = FastAPI()
    app.state.inbox_store = store
    register_workspace_escalation_routes(app, api_auth=lambda r: None)
    client = TestClient(app)

    resp = client.get("/api/workspace/handoff-brief", params={"conversation_id": cid})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["conversation_id"] == cid
    assert data["profile"]["intent"] == "咨询"
    assert any(t["text"] == "你好我要咨询" for t in data["recent_turns"])


def test_handoff_brief_endpoint_requires_cid(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.unified_inbox_workspace_escalation_routes import (
        register_workspace_escalation_routes,
    )
    app = FastAPI()
    register_workspace_escalation_routes(app, api_auth=lambda r: None)
    client = TestClient(app)
    resp = client.get("/api/workspace/handoff-brief")
    assert resp.status_code == 400
