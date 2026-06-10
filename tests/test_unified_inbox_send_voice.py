"""收件箱「语音回复（文本→声音克隆TTS→发语音）」契约回归。

锁定 POST /api/unified-inbox/send-voice 的对外契约：
  1. 缺 text/chat_key → 400（前端 sendVoiceReply 依赖此校验）
  2. 非 protocol/未在线账号 → 501（前端据此提示"需协议多开在线账号"）
合成+发送的正路依赖 TTS 后端 + 在线协议账号，属环境相关，不在单测内打真实外呼。
"""


def test_send_voice_requires_text(auth_client):
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default", "chat_key": "123"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_send_voice_requires_chat_key(auth_client):
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default", "text": "你好"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_send_voice_non_protocol_returns_501(auth_client):
    """无在线协议账号时须 501（而非 500/静默），前端据 detail 提示走 RPA voice_output。"""
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={
            "platform": "telegram", "account_id": "no-such-acct",
            "chat_key": "123", "text": "你好，这是一条语音测试",
        },
        follow_redirects=False,
    )
    assert r.status_code == 501


def test_voice_profiles_contract(auth_client):
    """按人设选音色：/api/voice/profiles 须返回 {ok, default, profiles[]}，
    每个 profile 带 persona_id/name/is_clone/ready（前端音色下拉依赖这些字段）。"""
    r = auth_client.get("/api/voice/profiles", follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert isinstance(d.get("default"), dict)
    assert isinstance(d.get("profiles"), list)
    for p in d["profiles"]:
        assert "persona_id" in p and "name" in p
        assert "is_clone" in p and "ready" in p
