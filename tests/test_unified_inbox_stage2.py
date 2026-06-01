"""统一收件箱 Phase A 持久层端到端测试（store-backed）。

验证：旁路写入 + automation_mode 持久化（重启不丢）+ store 缺失时回落。
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_chats(self, limit):
        return [{
            "chat_key": "line-room", "name": "Line User",
            "last_peer_text": "こんにちは", "last_ts": 100, "unread_count": 2,
        }]

    def status(self):
        return {"running": True, "serial": "line-serial"}


class TelegramClient:
    running = True
    _recent_messages = [
        {"chat_id": "tg-room", "user_name": "TG User", "text": "你好", "ts": 130},
        {"chat_id": "tg-room", "user_name": "TG User", "text": "hello again", "ts": 131},
    ]


def _client(inbox_store=None):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=page_auth, api_auth=api_auth, templates=_Templates())
    app.state.line_rpa_services = [LineSvc()]
    app.state.telegram_client = TelegramClient()
    if inbox_store is not None:
        app.state.inbox_store = inbox_store
    return TestClient(app)


def test_chats_request_bypass_writes_to_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store)
    resp = c.get("/api/unified-inbox/chats?limit=10")
    assert resp.status_code == 200
    # 旁路写入应已落库（LINE + Telegram 至少各一条会话）
    convs = store.list_conversations()
    platforms = {row["platform"] for row in convs}
    assert "line" in platforms
    assert store.count_messages() >= 1
    store.close()


def test_automation_mode_persists_across_restart(tmp_path):
    db = tmp_path / "inbox.db"
    store = InboxStore(db)
    c = _client(inbox_store=store)

    set_resp = c.post("/api/unified-inbox/automation", json={
        "platform": "telegram", "account_id": "default", "chat_key": "tg-room", "mode": "auto_ai",
    })
    assert set_resp.status_code == 200
    assert set_resp.json()["mode"] == "auto_ai"
    store.close()

    # 模拟重启：新 store 指向同一 db，新 app 复用
    store2 = InboxStore(db)
    c2 = _client(inbox_store=store2)
    get_resp = c2.get("/api/unified-inbox/automation?platform=telegram&account_id=default&chat_key=tg-room")
    assert get_resp.status_code == 200
    assert get_resp.json()["mode"] == "auto_ai"  # 重启后仍是 auto_ai（修掉进程内 dict 丢失）
    store2.close()


def test_thread_open_persists_history(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store)
    resp = c.get("/api/unified-inbox/thread?platform=telegram&account_id=default&chat_key=tg-room")
    assert resp.status_code == 200
    rows = store.list_messages("telegram:default:tg-room")
    assert [r["text"] for r in rows] == ["你好", "hello again"]
    store.close()


def test_without_store_falls_back_to_process_dict():
    # 不挂 inbox_store：automation 仍可用（回落进程内 dict），不报错
    c = _client(inbox_store=None)
    set_resp = c.post("/api/unified-inbox/automation", json={
        "platform": "line", "account_id": "line-a", "chat_key": "line-room", "mode": "multi_choice",
    })
    assert set_resp.status_code == 200
    get_resp = c.get("/api/unified-inbox/automation?platform=line&account_id=line-a&chat_key=line-room")
    assert get_resp.json()["mode"] == "multi_choice"
