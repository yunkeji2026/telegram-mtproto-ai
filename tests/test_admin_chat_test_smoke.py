"""冒烟测试：全链路对话自测端点（chat_test_routes 抽出后可调不崩）。

telegram_client=None → SkillManager 不可用 → 端点返回 {ok:False, error:...}（不 500）；
空 message → 400；多轮 session 状态由模块内闭包缓存维护。
"""

from __future__ import annotations


def test_chat_test_no_skill_manager(auth_client):
    r = auth_client.post("/api/chat/test", json={"message": "你好"})
    assert r.status_code == 200          # 不 500
    body = r.json()
    # 测试 app telegram_client=None → SkillManager 未初始化
    assert body["ok"] is False
    assert "SkillManager" in body.get("error", "")


def test_chat_test_empty_message_400(auth_client):
    r = auth_client.post("/api/chat/test", json={"message": ""})
    assert r.status_code == 400
