"""冒烟测试：运营 Copilot + 测试纠错端点（copilot_routes 抽出后可调不崩）。

telegram_client=None → copilot 的 ctx_store/sm 为 None → 走 KB/报表数据采集分支，
AI 生成跳过，仍结构化返回；chat/test/correct 用 kb_store 落库。均不得 500。
"""

from __future__ import annotations


def test_copilot_query_smoke(auth_client):
    r = auth_client.post("/api/copilot/query", json={"question": "今天知识库命中率怎么样"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "answer" in body and "data_sources" in body and "raw_data" in body


def test_copilot_query_empty_question_400(auth_client):
    r = auth_client.post("/api/copilot/query", json={"question": ""})
    assert r.status_code == 400


def test_chat_test_correct_smoke(auth_client):
    r = auth_client.post("/api/chat/test/correct", json={
        "user_message": "怎么退货",
        "wrong_reply": "不知道",
        "correct_reply": "支持 7 天无理由退货",
        "category": "其他",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "feedback_id" in body and "example_id" in body


def test_chat_test_correct_missing_fields_400(auth_client):
    r = auth_client.post("/api/chat/test/correct", json={"user_message": ""})
    assert r.status_code == 400
