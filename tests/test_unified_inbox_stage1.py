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


class _LangStore:
    """最小 inbox_store stub：仅实现 outbound 自动翻译需要的 get_conversation。"""

    def __init__(self, language="zh"):
        self._language = language

    def get_conversation(self, cid):
        return {"id": cid, "language": self._language}

    def record_agent_send(self, *a, **k):
        return None


def test_send_without_target_lang_sends_original():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={"platform": "line", "account_id": "line-a", "chat_key": "line-room", "text": "hello"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["sent_text"] == "hello"
    assert data["original_text"] == "hello"
    assert data["translation"] is None


def test_send_with_target_lang_translates_before_send():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "zh",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["original_text"] == "hello friend"
    assert data["sent_text"] == "你好朋友"  # FakeAI 译文
    assert data["translation"]["ok"] is True


def test_send_skip_translate_sends_original():
    c = _client()
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello", "target_lang": "zh", "skip_translate": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "hello"
    assert data["translation"] is None


def test_send_target_auto_infers_conversation_language():
    c = _client()
    c.app.state.inbox_store = _LangStore(language="zh")
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello friend", "target_lang": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "你好朋友"
    assert data["translation"]["target_lang"] == "zh"


def test_send_target_auto_unknown_language_sends_original():
    c = _client()
    c.app.state.inbox_store = _LangStore(language="unknown")
    resp = c.post(
        "/api/unified-inbox/send",
        json={
            "platform": "line", "account_id": "line-a", "chat_key": "line-room",
            "text": "hello", "target_lang": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent_text"] == "hello"
    assert data["translation"] is None


def test_unified_inbox_template_contains_translation_controls():
    """重构后三栏布局：翻译控件、AI草稿、会话列表等核心功能校验。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 翻译控件（P55：双向翻译条 xlate-in/out + xlateMsg）
    assert "xlate-in" in html
    assert "xlate-out" in html
    assert "xlateMsg" in html
    assert "翻译" in html
    # A：发送翻译目标支持「自动（客户语言）」——前端解析为会话 language 后复用两击预览
    assert 'value="auto"' in html
    assert "自动（客户语言）" in html
    # 平台导航（三栏布局核心）
    assert "nav-rail" in html
    assert "conv-items" in html
    assert "chat-panel" in html
    # AI 草稿
    assert "toggleAiDraft" in html
    assert "draft-panel" in html
    # 回复功能
    assert "reply-textarea" in html
    assert "sendMsg" in html
    # 账号管理抽屉
    assert "account-drawer" in html
    assert "openDrawer" in html


def _client_with_auto_assign():
    """启用 auto_assign 的 chats API 客户端，预置一个在线坐席（内存 coordinator）。"""
    from src.workspace.agent_coordinator import AgentCoordinator

    class _CM:
        config = {"workspace": {"auto_assign": {"enabled": True}}}

    app = FastAPI()

    def page_auth(request: Request):
        return None

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
        config_manager=_CM(),
    )
    app.state.line_rpa_services = [LineSvc()]
    app.state.whatsapp_rpa_services = []
    app.state.messenger_rpa_service = None
    app.state.telegram_client = None
    app.state.ai_client = FakeAI()
    coord = AgentCoordinator(store=None)
    coord.set_presence("alice", display_name="Alice", status="online")
    app.state.agent_coordinator = coord
    return TestClient(app)


def test_chats_attach_suggested_agent_when_auto_assign_enabled():
    c = _client_with_auto_assign()
    resp = c.get("/api/unified-inbox/chats")
    assert resp.status_code == 200, resp.text
    chats = resp.json()["chats"]
    assert chats, "应至少有一个 LINE 会话"
    sugg = [ch.get("suggested_agent") for ch in chats if ch.get("suggested_agent")]
    assert sugg, "启用 auto_assign 后未认领会话应附 suggested_agent"
    assert sugg[0]["agent_id"] == "alice"


def test_chats_no_suggested_agent_when_disabled():
    c = _client()  # config_manager=None → auto_assign 默认关
    resp = c.get("/api/unified-inbox/chats")
    assert resp.status_code == 200, resp.text
    chats = resp.json()["chats"]
    assert all("suggested_agent" not in ch for ch in chats)


def test_unified_inbox_template_contains_drafts_panel():
    """三栏重构：草稿面板使用内嵌 draft-panel 设计，包含审批 API 调用。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    # 核心：草稿面板存在
    assert "待审草稿" in html
    assert "draft-panel" in html
    # 草稿 API 调用
    assert "approveDraft" in html
    assert "rejectDraft" in html
    assert "/api/drafts/" in html and "/resolve" in html  # 草稿处置端点
    assert "risk-badge" in html or "risk-" in html  # 风险徽章（样式类）
    # 内嵌面板校验
    assert "loadDrafts" in html
    assert "draft-card-mini" in html or "draft-panel-items" in html


def test_unified_inbox_template_contains_assign_suggestion():
    """自动派单：会话列表「建议你接管」徽章 + 判定逻辑入模板。"""
    path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "unified_inbox.html"
    html = path.read_text(encoding="utf-8")
    assert "_convSuggestMine" in html          # 判定函数
    assert "conv-suggest-chip" in html          # 徽章样式类
    assert "建议你接管" in html                 # 徽章文案
    assert "suggested_agent" in html            # 读取后端字段
