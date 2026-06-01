"""DraftService 单元测试（Phase B）：read-through 聚合 + resolve 派发。

stub 严格按真实 service 的分歧签名实现：
- LINE:      resolve_pending(id, *, action, final_reply=None, by="")
- WhatsApp:  resolve_pending(id, action, by="")
- Messenger: state_store().decide_approval(id, *, approve, decided_by="")
"""

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def __init__(self):
        self.calls = []

    def list_pending(self, *, status=None, limit=50):
        return [{
            "id": 11, "chat_key": "lk", "chat_name": "Line User",
            "peer_text": "こんにちは", "draft_reply": "你好", "status": status or "pending",
            "ts": 100, "forced_lang": "ja",
        }]

    def resolve_pending(self, pending_id, *, action, final_reply=None, by=""):
        self.calls.append(("line", pending_id, action, final_reply, by))
        return {"id": pending_id, "status": "approved", "final_reply": final_reply}


class WaSvc:
    account_id = "wa-a"
    _merged_cfg = {"label": "WA-A"}

    def __init__(self):
        self.calls = []

    def list_pending(self, *, status=None, limit=50):
        return [{
            "id": 22, "chat_key": "wk", "peer_name": "WA User",
            "peer_text": "hola", "proposed_reply": "你好朋友", "status": status or "pending",
            "ts": 110,
        }]

    def resolve_pending(self, pending_id, action, by=""):
        self.calls.append(("wa", pending_id, action, by))
        return {"id": pending_id, "status": "approved"}


class _MsgStore:
    def __init__(self):
        self.calls = []

    def list_approvals(self, *, status=None, limit=50):
        return [{
            "id": 33, "account_id": "ms-a", "chat_key": "mk", "chat_name": "MS User",
            "peer_text": "bonjour", "reply_text": "你好", "status": status or "pending",
            "created_at": 120,
        }]

    def decide_approval(self, approval_id, *, approve, decided_by=""):
        self.calls.append(("decide", approval_id, approve, decided_by))
        return {"id": approval_id, "status": "approved" if approve else "rejected"}

    def update_approval_reply(self, approval_id, *, reply_text):
        self.calls.append(("edit", approval_id, reply_text))
        return True


class MsgSvc:
    def __init__(self):
        self._store = _MsgStore()

    def state_store(self):
        return self._store


def _service(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    line, wa, msg = LineSvc(), WaSvc(), MsgSvc()
    svc = DraftService(
        inbox_store=store, line_services=[line], wa_services=[wa], messenger_service=msg,
    )
    return svc, store, line, wa, msg


def test_list_drafts_aggregates_three_platforms(tmp_path):
    svc, store, *_ = _service(tmp_path)
    drafts = svc.list_drafts(status="pending", limit=50)
    platforms = {d["platform"] for d in drafts}
    assert platforms == {"line", "whatsapp", "messenger"}
    # draft_id 形如 source_kind:account_id:raw_id（account 内嵌支持多账号路由）
    ids = {d["draft_id"] for d in drafts}
    assert "line_pending:line-a:11" in ids
    assert "wa_pending:wa-a:22" in ids
    assert "messenger_approval:ms-a:33" in ids
    store.close()


def test_list_drafts_platform_filter(tmp_path):
    svc, store, *_ = _service(tmp_path)
    drafts = svc.list_drafts(platform="line")
    assert all(d["platform"] == "line" for d in drafts)
    assert len(drafts) == 1
    store.close()


def test_resolve_line_uses_keyword_action_and_final_reply(tmp_path):
    svc, store, line, _, _ = _service(tmp_path)
    res = svc.resolve("line_pending:line-a:11", "edit_send", text="改后的回复", by="op1")
    assert res["ok"] is True
    # edit_send → LINE edit_approve，文本走 final_reply
    assert line.calls == [("line", 11, "edit_approve", "改后的回复", "op1")]
    store.close()


def test_resolve_whatsapp_positional_action_no_text(tmp_path):
    svc, store, _, wa, _ = _service(tmp_path)
    res = svc.resolve("wa_pending:wa-a:22", "approve", by="op2")
    assert res["ok"] is True
    assert wa.calls == [("wa", 22, "approve", "op2")]
    store.close()


def test_resolve_whatsapp_edit_send_degrades_to_approve_with_note(tmp_path):
    svc, store, _, wa, _ = _service(tmp_path)
    res = svc.resolve("wa_pending:wa-a:22", "edit_send", text="编辑", by="op")
    assert res["ok"] is True
    assert wa.calls[0][2] == "approve"  # edit_send 退化 approve
    assert res["result"].get("note") == "wa_no_text_edit"
    store.close()


def test_resolve_messenger_decide_approval(tmp_path):
    svc, store, _, _, msg = _service(tmp_path)
    res = svc.resolve("messenger_approval:ms-a:33", "edit_send", text="新文案", by="op3")
    assert res["ok"] is True
    calls = msg.state_store().calls
    assert ("edit", 33, "新文案") in calls
    assert ("decide", 33, True, "op3") in calls
    store.close()


def test_resolve_reject_messenger(tmp_path):
    svc, store, _, _, msg = _service(tmp_path)
    res = svc.resolve("messenger_approval:ms-a:33", "reject", by="op")
    assert res["ok"] is True
    assert ("decide", 33, False, "op") in msg.state_store().calls
    store.close()


def test_resolve_invalid_action(tmp_path):
    svc, store, *_ = _service(tmp_path)
    res = svc.resolve("line_pending:line-a:11", "explode")
    assert res["ok"] is False
    assert res["code"] == 400
    store.close()


def test_resolve_writes_overlay_status(tmp_path):
    svc, store, *_ = _service(tmp_path)
    svc.resolve("line_pending:line-a:11", "approve", by="op")
    ov = store.get_overlay("line_pending", "line-a:11")
    assert ov is not None
    assert ov["status"] == "approved"
    assert ov["decided_by"] == "op"
    store.close()


def test_stats_counts_pending_per_platform(tmp_path):
    svc, store, *_ = _service(tmp_path)
    stats = svc.stats()
    assert stats["total_pending"] == 3
    assert stats["by_platform"]["line"]["pending"] == 1
    assert stats["by_platform"]["whatsapp"]["pending"] == 1
    assert stats["by_platform"]["messenger"]["pending"] == 1
    store.close()


def test_overlay_merges_risk_into_listed_draft(tmp_path):
    svc, store, *_ = _service(tmp_path)
    store.upsert_draft({
        "source_kind": "line_pending", "source_id": "line-a:11", "platform": "line",
        "risk_level": "high", "risk_reasons": ["money"], "autopilot_level": "L4",
        "status": "pending",
    })
    drafts = svc.list_drafts(platform="line")
    d = next(x for x in drafts if x["draft_id"] == "line_pending:line-a:11")
    assert d["risk_level"] == "high"
    assert d["risk_reasons"] == ["money"]
    assert d["autopilot_level"] == "L4"
    store.close()


def test_multi_account_resolve_routes_to_correct_service(tmp_path):
    """两个 LINE 账号都有 pending id=11，resolve 必须命中正确账号（不串号）。"""
    store = InboxStore(tmp_path / "inbox.db")

    class LineA(LineSvc):
        account_id = "acc-a"

    class LineB(LineSvc):
        account_id = "acc-b"

    a, b = LineA(), LineB()
    svc = DraftService(inbox_store=store, line_services=[a, b])
    # 处置 acc-b 的草稿
    res = svc.resolve("line_pending:acc-b:11", "approve", by="op")
    assert res["ok"] is True
    assert a.calls == []          # acc-a 不应被触碰
    assert len(b.calls) == 1      # 只命中 acc-b
    store.close()


def test_inbox_originated_draft_listed(tmp_path):
    svc, store, *_ = _service(tmp_path)
    store.upsert_draft({
        "source_kind": "inbox", "platform": "telegram", "chat_key": "tg",
        "draft_text": "自发草稿", "status": "pending", "draft_id": "inbox:abc",
    })
    drafts = svc.list_drafts(platform="inbox")
    assert any(d["draft_id"] == "inbox:abc" and d["draft_text"] == "自发草稿" for d in drafts)
    store.close()
