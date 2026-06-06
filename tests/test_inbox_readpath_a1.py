"""A1 读路径灰度切换测试。

覆盖：
- store_row_to_chat 纯函数：store 行 → live chat 行形状映射正确；
- /chats 默认（flag off）= 实时聚合（原行为，零变化）；
- /chats flag on + store 可用 = store-backed 列表（from_store 标记）；
- 影子读一致性：实时聚合 ingest 后，store-backed 视图覆盖同一批 conversation_id；
- flag on 但 store 缺失 → 自动回落实时聚合（不报错）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.normalizer import store_row_to_chat
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class _Cfg:
    """最小 config_manager 桩：仅暴露 .config dict。"""

    def __init__(self, read_from_store: bool):
        self.config = {"inbox": {"enabled": True, "read_from_store": read_from_store}}


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


def _client(inbox_store=None, read_from_store=False):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=page_auth, api_auth=api_auth,
                                  templates=_Templates())
    app.state.line_rpa_services = [LineSvc()]
    app.state.telegram_client = TelegramClient()
    app.state.config_manager = _Cfg(read_from_store)
    if inbox_store is not None:
        app.state.inbox_store = inbox_store
    return TestClient(app)


# ── 纯函数 ─────────────────────────────────────────────────────────

def test_store_row_to_chat_shape():
    row = {
        "conversation_id": "line:line-a:line-room", "platform": "line",
        "account_id": "line-a", "chat_key": "line-room",
        "display_name": "Line User", "language": "ja",
        "last_text": "こんにちは", "last_ts": 100, "unread": 2,
        "risk_level": "low",
    }
    chat = store_row_to_chat(row, automation_mode="auto_ai", message_count=3)
    assert chat["platform"] == "line"
    assert chat["platform_name"] == "LINE"
    assert chat["name"] == "Line User"
    assert chat["last_msg"] == "こんにちは"
    assert chat["conversation_id"] == "line:line-a:line-room"
    assert chat["automation_mode"] == "auto_ai"
    assert chat["message_count"] == 3
    assert chat["risk"]["level"] == "low"
    assert chat["from_store"] is True
    assert chat["send_modes"] == ["manual", "review", "multi_choice", "auto_ai"]


def test_store_row_to_chat_bad_mode_defaults_review():
    chat = store_row_to_chat({"platform": "line", "account_id": "a", "chat_key": "k"},
                             automation_mode="bogus")
    assert chat["automation_mode"] == "review"


# ── /chats 灰度 ────────────────────────────────────────────────────

def test_chats_default_is_live_aggregation(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store, read_from_store=False)
    data = c.get("/api/unified-inbox/chats?limit=10").json()
    assert data["ok"] is True
    # 实时聚合：行不带 from_store 标记
    assert all(not row.get("from_store") for row in data["chats"])
    platforms = {row["platform"] for row in data["chats"]}
    assert "line" in platforms and "telegram" in platforms
    store.close()


def test_chats_flag_on_reads_from_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    c = _client(inbox_store=store, read_from_store=True)
    data = c.get("/api/unified-inbox/chats?limit=10").json()
    assert data["ok"] is True
    # store-backed：每行带 from_store 标记
    assert data["chats"] and all(row.get("from_store") for row in data["chats"])
    platforms = {row["platform"] for row in data["chats"]}
    assert "line" in platforms and "telegram" in platforms
    store.close()


def test_chats_flag_on_without_store_falls_back_live():
    # flag on 但未挂 store → 回落实时聚合，不报错
    c = _client(inbox_store=None, read_from_store=True)
    data = c.get("/api/unified-inbox/chats?limit=10").json()
    assert data["ok"] is True
    assert all(not row.get("from_store") for row in data["chats"])


def test_shadow_read_consistency(tmp_path):
    """影子读一致性：实时聚合 ingest 后，store-backed 视图覆盖同一批 conversation_id。"""
    store = InboxStore(tmp_path / "inbox.db")
    # 先跑一次实时聚合（flag off），触发旁路 ingest
    c_live = _client(inbox_store=store, read_from_store=False)
    live = c_live.get("/api/unified-inbox/chats?limit=20").json()["chats"]
    live_ids = {r["conversation_id"] for r in live}

    # 再用 flag on 读 store-backed 视图
    c_store = _client(inbox_store=store, read_from_store=True)
    stored = c_store.get("/api/unified-inbox/chats?limit=20").json()["chats"]
    stored_ids = {r["conversation_id"] for r in stored}

    # store 视图应覆盖实时聚合产生的全部会话（事实源已落库）
    assert live_ids, "live aggregation should produce conversations"
    assert live_ids <= stored_ids, f"store 缺失会话: {live_ids - stored_ids}"
    store.close()
