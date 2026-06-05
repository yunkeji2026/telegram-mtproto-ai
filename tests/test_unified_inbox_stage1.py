from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pathlib import Path

from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_chats(self, limit):
        return [{
            "chat_key": "line-room",
            "name": "Line User",
            "last_peer_text": "こんにちは",
            "last_ts": 100,
            "unread_count": 2,
        }]

    def status(self):
        return {"running": True, "serial": "line-serial"}

    async def send_to_chat(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class WhatsAppSvc:
    account_id = "wa-a"
    _merged_cfg = {"label": "WA-A"}

    def list_pending(self, status="pending", limit=20):
        return [{
            "chat_key": "wa-room",
            "peer_name": "WA User",
            "peer_text": "hello friend",
            "ts": 110,
        }]

    def status(self):
        return {"running": True, "serial": "wa-serial"}

    async def send_to_chat(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class MessengerSvc:
    is_running = True

    def list_approvals(self, status="pending", limit=20):
        return [{
            "account_id": "ms-a",
            "chat_key": "ms-room",
            "name": "Messenger User",
            "peer_text": "hola, gracias",
            "ts": 120,
        }]

    async def send_to_chat_name(self, chat_name, text):
        return {"chat_name": chat_name, "text": text}


class TelegramClient:
    running = True
    _recent_messages = [
        {"chat_id": "tg-room", "user_name": "TG User", "text": "你好", "ts": 130},
        {"chat_id": "tg-room", "user_name": "TG User", "text": "hello again", "ts": 131},
    ]

    async def send_message(self, chat_key, text):
        return {"chat_key": chat_key, "text": text}


class FakeAI:
    async def chat(self, prompt, context=None):
        return "你好朋友"


def _client():
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
    )
    app.state.line_rpa_services = [LineSvc()]
    app.state.whatsapp_rpa_services = [WhatsAppSvc()]
    app.state.messenger_rpa_service = MessengerSvc()
    app.state.telegram_client = TelegramClient()
    app.state.ai_client = FakeAI()
    return TestClient(app)


def test_unified_inbox_chats_returns_four_platforms_and_message_shape():
    c = _client()
    resp = c.get("/api/unified-inbox/chats?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    platforms = {row["platform"] for row in data["chats"]}
    assert {"line", "whatsapp", "messenger", "telegram"} <= platforms
    row = data["chats"][0]
    assert "conversation_id" in row
    assert "last_message" in row
    assert "language" in row["last_message"]
    assert row["can_send"] is True
    assert "multi_choice" in row["send_modes"]


def test_unified_inbox_thread_returns_telegram_history():
    c = _client()
    resp = c.get("/api/unified-inbox/thread?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert [m["text"] for m in data["messages"]] == ["你好", "hello again"]


def test_unified_inbox_translate_endpoint_uses_service():
    c = _client()
    resp = c.post("/api/unified-inbox/translate", json={"text": "hello friend", "target_lang": "zh"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["translation"]["translated_text"] == "你好朋友"


def test_unified_inbox_analyze_endpoint_returns_suggestions():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/analyze",
        json={"text": "hi", "messages": [{"text": "hi"}], "chat": {"language": "en"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["analysis"]["intent"] == "打招呼"
    assert len(data["analysis"]["suggestions"]) == 3


def test_unified_inbox_profile_endpoint_returns_contact_shape():
    c = _client()
    resp = c.get("/api/unified-inbox/profile?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200
    data = resp.json()
    profile = data["profile"]
    assert profile["display_name"] == "TG User"
    assert profile["relationship"]["stage"] in {"初识", "升温", "稳定陪伴"}
    assert profile["activity"]["message_count"] == 2
    assert "tags" in profile


def test_unified_inbox_automation_mode_roundtrip():
    c = _client()
    get_resp = c.get("/api/unified-inbox/automation?platform=telegram&account_id=default&chat_key=tg-room")
    assert get_resp.status_code == 200
    assert get_resp.json()["mode"] == "review"

    set_resp = c.post(
        "/api/unified-inbox/automation",
        json={"platform": "telegram", "account_id": "default", "chat_key": "tg-room", "mode": "multi_choice"},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["mode"] == "multi_choice"

    chats = c.get("/api/unified-inbox/chats?limit=10").json()["chats"]
    tg = next(row for row in chats if row["platform"] == "telegram")
    assert tg["automation_mode"] == "multi_choice"


def test_unified_inbox_send_supports_four_platforms():
    c = _client()
    cases = [
        ("line", "line-a", "line-room"),
        ("whatsapp", "wa-a", "wa-room"),
        ("messenger", "ms-a", "ms-room"),
        ("telegram", "default", "tg-room"),
    ]
    for platform, account_id, chat_key in cases:
        resp = c.post(
            "/api/unified-inbox/send",
            json={"platform": platform, "account_id": account_id, "chat_key": chat_key, "text": "hi"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


def test_unified_inbox_template_contains_translation_controls():
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    assert "翻译为中文" in html
    assert "翻译预览" in html
    assert "翻译成" in html  # Phase 4：翻译后发送控件
    assert "/api/unified-inbox/translate" in html
    assert "/api/unified-inbox/analyze" in html
    assert "/api/unified-inbox/profile" in html
    assert "/api/voice/tts-test" in html
    assert "/api/unified-inbox/automation" in html
    assert "多答案建议" in html
    assert "客户档案" in html
    # Phase 4：账号分栏 + AI 接管开关 + 翻译后发送
    assert "renderPlatformBar" in html
    assert "data-filter" in html
    assert "ai-takeover-btn" in html
    assert "toggleAiTakeover" in html
    assert "xlate-on" in html
    assert "生成语音预览" in html
    assert "AI 助手" in html


def test_unified_inbox_template_contains_drafts_panel():
    """P0-a：统一收件箱接入 /api/drafts 待审草稿队列（Phase B 可视化）。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    assert "待审草稿" in html
    assert "/api/drafts?status=pending" in html
    assert "/api/drafts/stats" in html
    assert "/resolve" in html
    assert "UI.showDrafts" in html
    assert "UI.resolveDraft" in html
    assert "risk-badge" in html  # 风险徽章
    # P0-b：订单/物流事实卡片（事实校验可视化）
    assert "renderOrderLookup" in html
    assert "ai-order" in html
    assert "事实校验" in html
