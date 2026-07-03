"""Messenger 陌生人「消息请求」入站策略门禁。

产品口径（用户拍板）：陌生人首次来讯（Messenger「消息请求」）——
  · 「可能认识 / general」类 → 进收件箱且**允许人设自动回**（不强改档位，走全局默认 auto_ai）。
  · 「垃圾 / spam」类           → **只进收件箱、不自动回**（落库前预置 automation_mode=manual）。

关键不变量：auto-draft(System Z) 在 ingest_incoming 内部即触发，故 spam 会话必须在
**落库前**就把档位预置成 manual（否则回调按默认 auto_ai 已生成草稿 → 可能被 autosend）；
且预置仅在坐席未显式设过档位时进行，尊重人工覆盖。另：avatar_url 须透传落库（会话列表显真头像）。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.normalizer import conv_id
from src.web.routes.unified_inbox_account_routes import register_account_routes


def _client(tmp_path):
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None, config_manager=None)
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app), app.state.inbox_store


def test_spam_request_forced_manual_no_autoreply(tmp_path):
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_spam")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_spam", "name": "Sketchy Sender",
        "text": "click this link to win $$$", "ts": 1780000000.0,
        "direction": "in", "is_request": True, "request_category": "spam",
    })
    assert r.status_code == 200, r.text
    assert r.json().get("conversation_id") == cid
    # 垃圾请求：进收件箱（消息落库）但档位被预置为 manual → 不自动回。
    assert store.count_messages(cid) == 1
    assert store.get_automation_mode_if_set(cid) == "manual"
    store.close()


def test_general_request_left_default_and_avatar_stored(tmp_path):
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "stranger_ok")
    avatar = "/static/protocol_media/messenger/avatars/stranger_ok.jpg"
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "stranger_ok", "name": "Maybe Friend",
        "text": "hi there!", "ts": 1780000001.0, "direction": "in",
        "is_request": True, "request_category": "general",
        "avatar_url": avatar,
    })
    assert r.status_code == 200, r.text
    # 「可能认识」请求：预置 auto_ai → 人设自动 AI 回（本部署全局默认档位未设=review，
    # 若不预置则只出人审草稿、不真发；故必须显式预置成 auto_ai 才兑现「自动回」策略）。
    assert store.get_automation_mode_if_set(cid) == "auto_ai"
    # 头像透传落库（会话列表显真头像，兑现最初诉求）。
    conv = store.get_conversation(cid) or {}
    assert conv.get("avatar_url") == avatar
    store.close()


def test_spam_respects_operator_manual_override_precedence(tmp_path):
    """坐席已把该会话显式设成 auto_ai 时，spam 请求不得反向覆盖（尊重人工决策）。"""
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "known_but_spammy")
    store.set_automation_mode(cid, "auto_ai")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "known_but_spammy", "name": "VIP",
        "text": "promo", "ts": 1780000002.0, "direction": "in",
        "is_request": True, "request_category": "spam",
    })
    assert r.status_code == 200, r.text
    assert store.get_automation_mode_if_set(cid) == "auto_ai"  # 未被 spam 策略覆盖
    store.close()


def test_normal_inbound_not_treated_as_request(tmp_path):
    """非请求（普通好友入站）不受策略影响：不预置 manual。"""
    c, store = _client(tmp_path)
    cid = conv_id("messenger", "acc1", "friend")
    r = c.post("/api/internal/protocol/ingest", json={
        "platform": "messenger", "account_id": "acc1",
        "chat_key": "friend", "name": "Bestie", "text": "在吗",
        "ts": 1780000003.0, "direction": "in",
    })
    assert r.status_code == 200, r.text
    assert store.get_automation_mode_if_set(cid) is None
    store.close()
