"""A1 读路径灰度切换测试。

覆盖：
- store_row_to_chat 纯函数：store 行 → live chat 行形状映射正确；
- /chats 默认（flag off）= 实时聚合（原行为，零变化）；
- /chats flag on + store 可用 = store-backed 列表（from_store 标记）；
- 影子读一致性：实时聚合 ingest 后，store-backed 视图覆盖同一批 conversation_id；
- flag on 但 store 缺失 → 自动回落实时聚合（不报错）。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.ingest import ingest_collected_chats, ingest_thread
from src.inbox.normalizer import (
    extract_platform_msg_id,
    store_message_to_obj,
    store_row_to_chat,
)
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
    chat = store_row_to_chat(row, automation_mode="auto_ai", message_count=3,
                             account_label="LINE-A")
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
    # A1 等价硬化：account_label 用传入友好名（非账号 id）；last_message/messages 由末条重建
    assert chat["account_label"] == "LINE-A"
    assert chat["last_message"] is not None
    assert chat["last_message"]["text"] == "こんにちは"
    assert len(chat["messages"]) == 1 and chat["messages"][0]["text"] == "こんにちは"


def test_store_row_to_chat_no_label_falls_back_account_id():
    chat = store_row_to_chat({"platform": "line", "account_id": "acc7", "chat_key": "k"})
    assert chat["account_label"] == "acc7"


def test_store_row_to_chat_empty_last_text_no_messages():
    chat = store_row_to_chat({"platform": "line", "account_id": "a", "chat_key": "k"})
    assert chat["last_message"] is None
    assert chat["messages"] == []


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


def test_chats_live_store_equivalence(tmp_path):
    """A1「灰度转默认」强等价：同一 fixture 下 flag off(live) 与 flag on(store) 对每个
    会话的关键展示字段（name/last_msg/last_ts/account_label/last_message.text/language）一致。

    比 shadow（仅 ID 覆盖）更强——验收要求「/chats 行为不变」。先跑 live 触发旁路 ingest，
    再读 store；store 读路径借 live 派生 label 回填，应与 live 行逐字段相等。
    """
    store = InboxStore(tmp_path / "inbox.db")
    c_live = _client(inbox_store=store, read_from_store=False)
    live = c_live.get("/api/unified-inbox/chats?limit=20").json()["chats"]
    c_store = _client(inbox_store=store, read_from_store=True)
    stored = c_store.get("/api/unified-inbox/chats?limit=20").json()["chats"]

    # 实时聚合对同一会话可能产出多行（如 Telegram 一条消息一行——历史窗口快照），
    # store 收口为「每会话一行 + 末条」。取 live 每会话 last_ts 最大者作等价基准（= store 语义）。
    live_by_id: dict = {}
    for r in live:
        cid = r["conversation_id"]
        if cid not in live_by_id or (r.get("last_ts") or 0) >= (
                live_by_id[cid].get("last_ts") or 0):
            live_by_id[cid] = r
    stored_by_id = {r["conversation_id"]: r for r in stored}
    assert live_by_id, "live aggregation should produce conversations"

    for cid, lr in live_by_id.items():
        sr = stored_by_id.get(cid)
        assert sr is not None, f"store 缺会话 {cid}"
        for field in ("name", "last_msg", "last_ts", "account_label", "language"):
            assert sr[field] == lr[field], (
                f"{cid} 字段 {field} 不等: live={lr[field]!r} store={sr[field]!r}")
        # last_message 末条文本两路径一致（store 由 last_text 重建）
        ls = (lr.get("last_message") or {}).get("text", "")
        ss = (sr.get("last_message") or {}).get("text", "")
        assert ss == ls, f"{cid} last_message.text 不等: live={ls!r} store={ss!r}"
    store.close()


# ── F4：live 列表 store 身份「仅补空/仅升级」富集 ─────────────────

def _fake_req(store):
    """最小 request 桩：仅暴露 request.app.state.inbox_store（_inbox_store 读取点）。"""
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=store)))


@pytest.fixture(autouse=True)
def _reset_readback_state():
    """F5：清进程级回读观测（去重集 + 计数单例），使各用例互不串扰、可断言精确计数。"""
    from src.web.peer_identity_stats import get_peer_identity_stats
    from src.web.routes.unified_inbox_aggregate import _READBACK_SEEN
    _READBACK_SEEN.clear()
    get_peer_identity_stats().reset()
    yield
    _READBACK_SEEN.clear()
    get_peer_identity_stats().reset()


def test_apply_store_identity_upgrades_and_fills():
    from src.web.routes.unified_inbox_aggregate import _apply_store_identity
    chat = {"chat_key": "123", "name": "123",  # 裸数字名 = 非真名
            "username": "", "phone": "", "avatar_url": ""}
    row = {"display_name": "林小雨", "username": "lin",
           "phone": "+639171234567", "avatar_url": "https://cdn/x.jpg"}
    # 返回本次补齐的字段列表（avatar_url → "avatar"），供 F5 观测按字段计数
    assert set(_apply_store_identity(chat, row)) == {"name", "username", "phone", "avatar"}
    assert chat["name"] == "林小雨"          # 裸号→真名 升级
    assert chat["username"] == "lin"
    assert chat["phone"] == "+639171234567"
    assert chat["avatar_url"] == "https://cdn/x.jpg"


def test_apply_store_identity_never_clobbers_real_name_or_existing_values():
    from src.web.routes.unified_inbox_aggregate import _apply_store_identity
    chat = {"chat_key": "123", "name": "Real Name",
            "username": "keep", "phone": "", "avatar_url": "live://a"}
    row = {"display_name": "Other", "username": "other",
           "phone": "+63", "avatar_url": "store://b"}
    upgraded = _apply_store_identity(chat, row)
    assert chat["name"] == "Real Name"       # 已是真名 → 不覆盖
    assert chat["username"] == "keep"        # 已有值 → 不覆盖
    assert chat["avatar_url"] == "live://a"  # 已有值 → 不覆盖
    assert chat["phone"] == "+63"            # 仅补空
    assert upgraded == ["phone"]             # 只补了 phone


def test_apply_store_identity_store_raw_name_not_used():
    from src.web.routes.unified_inbox_aggregate import _apply_store_identity
    chat = {"chat_key": "123", "name": "123",
            "username": "", "phone": "", "avatar_url": ""}
    row = {"display_name": "123", "username": "", "phone": "", "avatar_url": ""}
    assert _apply_store_identity(chat, row) == []   # 无补齐
    assert chat["name"] == "123"             # store 也是裸号 → 不升级


def test_overlay_store_identity_fills_from_store(tmp_path):
    from src.web.routes.unified_inbox_aggregate import _overlay_store_identity
    store = InboxStore(tmp_path / "i.db")
    cid = "line:line-a:room"
    # 模拟 side-effect ingest 早前已抓到富身份（username/头像/电话）
    ingest_collected_chats(store, [{
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "room", "name": "林小雨", "username": "lin",
        "phone": "+639171234567", "avatar_url": "https://cdn/x.jpg",
        "last_msg": "hi", "last_ts": 10,
    }])
    # live 行只有裸 chat_key 名、无 username/头像（多数 RPA 适配器的贫身份）
    live = [{"conversation_id": cid, "platform": "line", "account_id": "line-a",
             "chat_key": "room", "name": "room",
             "username": "", "phone": "", "avatar_url": ""}]
    out = _overlay_store_identity(_fake_req(store), live)
    assert out is live                        # 原地修改
    assert live[0]["name"] == "林小雨"
    assert live[0]["username"] == "lin"
    assert live[0]["phone"] == "+639171234567"
    assert live[0]["avatar_url"] == "https://cdn/x.jpg"
    store.close()


def test_overlay_store_identity_no_store_is_noop():
    from src.web.routes.unified_inbox_aggregate import _overlay_store_identity
    live = [{"conversation_id": "x", "chat_key": "r", "name": "r",
             "username": "", "phone": "", "avatar_url": ""}]
    out = _overlay_store_identity(_fake_req(None), live)
    assert out is live and live[0]["name"] == "r"   # store 缺失 → 原样返回


def test_overlay_store_identity_skips_db_when_live_complete():
    """身份已齐（真名 + username + 头像）的行不查库（零 IO）。"""
    from src.web.routes.unified_inbox_aggregate import _overlay_store_identity

    class _Counting:
        def __init__(self):
            self.calls = 0

        def get_conversation(self, cid):
            self.calls += 1
            return None

    store = _Counting()
    live = [{"conversation_id": "line:a:r", "chat_key": "r", "name": "Ann",
             "username": "ann", "phone": "", "avatar_url": "https://x"}]
    _overlay_store_identity(_fake_req(store), live)
    assert store.calls == 0                   # 完整身份 → 不回库


def test_chats_live_mode_overlays_store_identity(tmp_path):
    """F4 端到端：read_from_store=false 下 /chats 仍用 store 已持久身份补空。

    live 适配器（LineSvc）只给 name、无 username/头像/电话；store 有 → 返回行补齐。
    依赖 conversations upsert 的「空值不覆盖」护栏：live 旁路 ingest（贫身份）不会清掉
    store 已有的 username/avatar，overlay 才能补回。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "line:line-a:line-room"
    ingest_collected_chats(store, [{
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "line-room", "name": "Line User", "username": "lineuser",
        "phone": "+639171234567", "avatar_url": "https://cdn/l.jpg",
        "last_msg": "こんにちは", "last_ts": 100,
    }])
    c = _client(inbox_store=store, read_from_store=False)
    chats = c.get("/api/unified-inbox/chats?limit=10").json()["chats"]
    row = next(r for r in chats if r["conversation_id"] == cid)
    assert not row.get("from_store")          # 仍是 live 行（未切 store-backed）
    assert row["username"] == "lineuser"      # F4 从 store 补上
    assert row["avatar_url"] == "https://cdn/l.jpg"
    assert row["phone"] == "+639171234567"
    store.close()


def test_overlay_records_readback_deduped(tmp_path):
    """F5：回读补齐落观测——按平台记补了哪些字段；同一会话跨多次 overlay 只计一次（去重）。"""
    from src.web.peer_identity_stats import get_peer_identity_stats
    from src.web.routes.unified_inbox_aggregate import _overlay_store_identity
    store = InboxStore(tmp_path / "i.db")
    cid = "line:line-a:rb-room"
    ingest_collected_chats(store, [{
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "rb-room", "name": "林小雨", "username": "lin",
        "phone": "+63", "avatar_url": "https://c/x.jpg", "last_msg": "hi", "last_ts": 10,
    }])
    req = _fake_req(store)

    def _poor():
        return [{"conversation_id": cid, "platform": "line", "account_id": "line-a",
                 "chat_key": "rb-room", "name": "rb-room",
                 "username": "", "phone": "", "avatar_url": ""}]

    _overlay_store_identity(req, _poor())
    _overlay_store_identity(req, _poor())     # 同 cid 第二次 → 去重不再计
    rb = get_peer_identity_stats().dump()["readback"]
    assert rb["rows"] == 1                     # 去重：一个会话只记一次
    assert rb["name"] == 1 and rb["username"] == 1
    assert rb["phone"] == 1 and rb["avatar"] == 1
    assert rb["by_platform"]["line"]["rows"] == 1


def test_overlay_no_readback_when_nothing_upgraded(tmp_path):
    """live 已有真名+username+头像（无可补）→ 快路径跳过、不记 readback。"""
    from src.web.peer_identity_stats import get_peer_identity_stats
    from src.web.routes.unified_inbox_aggregate import _overlay_store_identity
    store = InboxStore(tmp_path / "i.db")
    cid = "line:line-a:full-room"
    ingest_collected_chats(store, [{
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "full-room", "name": "Ann", "username": "ann",
        "avatar_url": "https://c/a.jpg", "last_msg": "hi", "last_ts": 10,
    }])
    live = [{"conversation_id": cid, "platform": "line", "account_id": "line-a",
             "chat_key": "full-room", "name": "Ann", "username": "ann",
             "phone": "", "avatar_url": "https://c/a.jpg"}]
    _overlay_store_identity(_fake_req(store), live)
    assert get_peer_identity_stats().dump()["readback"]["rows"] == 0
    store.close()


def test_thread_live_config_target_carries_store_identity(tmp_path):
    """F4×F3 组合：read_from_store=false 下 /thread 的 d.chat 携带 store 已持久身份
    （username/头像）→ 前端 _mergePeerIdentity 在 live 模式也能把最新身份合成进会话。

    live telegram 源只给 user_name（无 username/头像）；store 有 → target 补齐后返回。"""
    store = InboxStore(tmp_path / "i.db")
    cid = "telegram:default:tg-room"
    ingest_collected_chats(store, [{
        "conversation_id": cid, "platform": "telegram", "account_id": "default",
        "chat_key": "tg-room", "name": "TG User", "username": "tguser",
        "avatar_url": "/static/peer_avatars/tg.jpg", "last_msg": "你好", "last_ts": 130,
    }])
    app = _thread_client(read_from_store=False)   # live 模式
    app.state.inbox_store = store
    c = TestClient(app)
    data = c.get("/api/unified-inbox/thread?platform=telegram&chat_key=tg-room").json()
    assert data["ok"] is True and data["chat"] is not None
    assert data["chat"]["username"] == "tguser"       # d.chat 带上 store 身份
    assert data["chat"]["avatar_url"] == "/static/peer_avatars/tg.jpg"
    store.close()


# ── 稳定 message id（按平台白名单抽取）─────────────────────────────

def test_extract_platform_msg_id_per_platform():
    assert extract_platform_msg_id({"id": 123}, "telegram") == "123"
    assert extract_platform_msg_id({"wamid": "ABC"}, "whatsapp") == "ABC"
    assert extract_platform_msg_id({"mid": "x"}, "messenger") == "x"
    # LINE 不取裸 id（房间 id），仅取 message_id/server_id
    assert extract_platform_msg_id({"id": "room1"}, "line") == ""
    assert extract_platform_msg_id({"message_id": "m1", "id": "room1"}, "line") == "m1"
    # 未知平台默认只认 message_id
    assert extract_platform_msg_id({"message_id": "z"}, "x") == "z"
    assert extract_platform_msg_id({}, "telegram") == ""
    assert extract_platform_msg_id(None, "telegram") == ""


def test_store_message_to_obj_shape():
    row = {
        "message_id": "telegram:default:r:42", "platform_msg_id": "42",
        "direction": "in", "text": "hi", "original_text": "hi",
        "translated_text": "你好", "source_lang": "en", "target_lang": "zh",
        "ts": 100, "media_type": "", "media_ref": "",
    }
    obj = store_message_to_obj(row)
    assert obj["from_store"] is True
    assert obj["message_id"] == "telegram:default:r:42"
    assert obj["platform_msg_id"] == "42"
    assert obj["translated_text"] == "你好"
    assert obj["translation"]["provider"] == "store"
    assert obj["translation"]["ok"] is True
    # 无译文 + 非中文 → ok=False（待译）
    obj2 = store_message_to_obj({"text": "hello", "source_lang": "en"})
    assert obj2["translation"]["ok"] is False


def test_stable_id_dedup_survives_ts_drift(tmp_path):
    """同一条消息（同平台 id）即使 ts 漂移，也只去重为一条；hash 兜底则会重复。"""
    store = InboxStore(tmp_path / "inbox.db")
    chat = {
        "conversation_id": "telegram:default:room", "platform": "telegram",
        "account_id": "default", "chat_key": "room", "name": "U",
        "last_msg": "hi", "last_ts": 100,
        "last_message": {"text": "hi", "ts": 100, "direction": "in",
                         "source": {"id": 555}},
    }
    # collect 路径：last_message ts=100, id=555
    ingest_collected_chats(store, [chat])
    # thread 路径：同 id=555 但 ts 漂移到 101（RPA 重抓常见）
    ingest_thread(store, chat, [{"text": "hi", "ts": 101, "direction": "in",
                                 "source": {"id": 555}}])
    msgs = store.list_messages("telegram:default:room")
    assert len(msgs) == 1, "稳定平台 id 应跨 ts 漂移去重为一条"
    assert msgs[0]["platform_msg_id"] == "555"
    store.close()


def test_distinct_ids_not_collapsed_even_if_same_text(tmp_path):
    """同文本同秒但平台 id 不同 → 两条（hash 兜底会误并为一条）。"""
    store = InboxStore(tmp_path / "inbox.db")
    chat = {
        "conversation_id": "whatsapp:wa1:peer", "platform": "whatsapp",
        "account_id": "wa1", "chat_key": "peer", "name": "P",
        "last_msg": "ok", "last_ts": 200,
    }
    ingest_thread(store, chat, [
        {"text": "ok", "ts": 200, "direction": "in", "source": {"wamid": "A"}},
        {"text": "ok", "ts": 200, "direction": "in", "source": {"wamid": "B"}},
    ])
    msgs = store.list_messages("whatsapp:wa1:peer")
    assert len(msgs) == 2, "不同 wamid 不应被内容哈希误并"
    assert {m["platform_msg_id"] for m in msgs} == {"A", "B"}
    store.close()


# ── thread 读路径收尾（store-backed history）──────────────────────

class _TgClientWithIds:
    running = True
    _recent_messages = [
        {"chat_id": "tg-room", "user_name": "TG", "text": "你好", "ts": 130, "id": 7001},
        {"chat_id": "tg-room", "user_name": "TG", "text": "在吗", "ts": 131, "id": 7002},
    ]


def _thread_client(read_from_store):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=page_auth, api_auth=api_auth,
                                  templates=_Templates())
    app.state.telegram_client = _TgClientWithIds()
    app.state.config_manager = _Cfg(read_from_store)
    return app


def test_thread_flag_off_is_live(tmp_path):
    app = _thread_client(read_from_store=False)
    app.state.inbox_store = InboxStore(tmp_path / "i.db")
    c = TestClient(app)
    data = c.get("/api/unified-inbox/thread?platform=telegram&chat_key=tg-room").json()
    assert data["ok"] is True
    assert data["messages"]
    assert all(not m.get("from_store") for m in data["messages"])


def test_thread_flag_on_reads_from_store(tmp_path):
    app = _thread_client(read_from_store=True)
    app.state.inbox_store = InboxStore(tmp_path / "i.db")
    c = TestClient(app)
    data = c.get("/api/unified-inbox/thread?platform=telegram&chat_key=tg-room").json()
    assert data["ok"] is True
    assert data["messages"], "store-backed thread 应返回历史"
    assert all(m.get("from_store") for m in data["messages"])
    # 稳定 id 持久：platform_msg_id 来自 MTProto id
    pids = {m.get("platform_msg_id") for m in data["messages"]}
    assert "7001" in pids and "7002" in pids
    app.state.inbox_store.close()


def test_thread_before_ts_reads_older_page(tmp_path):
    app = _thread_client(read_from_store=True)
    app.state.inbox_store = InboxStore(tmp_path / "i.db")
    c = TestClient(app)
    first = c.get(
        "/api/unified-inbox/thread?platform=telegram&chat_key=tg-room&limit=2"
    ).json()
    assert [m["text"] for m in first["messages"]] == ["你好", "在吗"]

    older = c.get(
        "/api/unified-inbox/thread?platform=telegram&chat_key=tg-room"
        "&limit=2&before_ts=131"
    ).json()
    assert [m["text"] for m in older["messages"]] == ["你好"]
    assert older["oldest_ts"] == 130
    app.state.inbox_store.close()


def test_thread_from_store_does_not_reingest_preview(tmp_path):
    """store-backed 会话打开线程时，不应把 last_message 预览重写成 :h: 入站假消息。"""
    store = InboxStore(tmp_path / "i.db")
    cid = "line:line-a:room"
    chat = {
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "room", "name": "Old User", "last_msg": "我方回复", "last_ts": 50,
    }
    ingest_thread(store, chat, [{
        "text": "我方回复", "ts": 50, "direction": "out",
        "source": {"message_id": "L-out-1"},
    }])
    app = FastAPI()
    register_unified_inbox_routes(app, page_auth=lambda r: True,
                                  api_auth=lambda r: True, templates=_Templates())
    app.state.config_manager = _Cfg(True)
    app.state.inbox_store = store
    c = TestClient(app)

    data = c.get(
        "/api/unified-inbox/thread?platform=line&account_id=line-a&chat_key=room"
    ).json()
    assert data["ok"] is True
    rows = store.list_recent_messages(cid, limit=10)
    assert len(rows) == 1
    assert rows[0]["direction"] == "out"
    assert rows[0]["platform_msg_id"] == "L-out-1"
    store.close()


def test_thread_served_from_store_when_not_live(tmp_path):
    """会话已不在实时聚合窗口，但 store 有持久档 → 仍能从 store 读出历史 + header。"""
    store = InboxStore(tmp_path / "i.db")
    # 预先把一条历史落库（模拟此前已 ingest），实时源此刻无该会话
    cid = "line:line-a:gone-room"
    chat = {
        "conversation_id": cid, "platform": "line", "account_id": "line-a",
        "chat_key": "gone-room", "name": "Old User", "last_msg": "再见", "last_ts": 50,
    }
    ingest_thread(store, chat, [{"text": "再见", "ts": 50, "direction": "in",
                                 "source": {"message_id": "L1"}}])
    app = FastAPI()
    register_unified_inbox_routes(app, page_auth=lambda r: True,
                                  api_auth=lambda r: True, templates=_Templates())
    app.state.telegram_client = _TgClientWithIds()  # 实时源里没有 line gone-room
    app.state.config_manager = _Cfg(True)
    app.state.inbox_store = store
    c = TestClient(app)
    data = c.get("/api/unified-inbox/thread"
                 "?platform=line&account_id=line-a&chat_key=gone-room").json()
    assert data["ok"] is True
    assert data["messages"] and data["messages"][0]["from_store"] is True
    assert data["chat"] is not None and data["chat"]["conversation_id"] == cid
    store.close()
