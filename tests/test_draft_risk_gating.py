"""Phase B↔C 桥接：风险分层 L0–L4 + 自动发送门禁。"""

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService, risk_to_autopilot, is_autosend_allowed


def test_risk_to_autopilot_levels():
    assert risk_to_autopilot("high", "auto_ai") == "L4"     # high 强制人工
    assert risk_to_autopilot("medium", "auto_ai") == "L3"   # medium 强制审批
    assert risk_to_autopilot("low", "manual") == "L0"       # 仅翻译
    assert risk_to_autopilot("low", "auto_ai") == "L2"      # 低风险自动
    assert risk_to_autopilot("low", "review") == "L1"       # 默认草稿待审


def test_autosend_only_l2():
    assert is_autosend_allowed("low", "auto_ai") is True
    # 核心安全不变量：medium/high 即使 auto_ai 也禁止自动发
    assert is_autosend_allowed("medium", "auto_ai") is False
    assert is_autosend_allowed("high", "auto_ai") is False
    assert is_autosend_allowed("low", "review") is False


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_pending(self, *, status=None, limit=50):
        return [{
            "id": 11, "chat_key": "lk", "chat_name": "U",
            "peer_text": "请帮我转账", "draft_reply": "好的", "status": "pending", "ts": 1,
        }]


def test_apply_analysis_writes_overlay_and_blocks_autosend(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store, line_services=[LineSvc()])
    analysis = {"risk_level": "high", "risk_reasons": ["money"]}
    res = svc.apply_analysis("line_pending:line-a:11", analysis, automation_mode="auto_ai")
    assert res["ok"] is True
    assert res["autopilot_level"] == "L4"
    assert res["autosend_allowed"] is False
    # overlay 落库 + 列表合并 risk
    ov = store.get_overlay("line_pending", "line-a:11")
    assert ov["risk_level"] == "high"
    assert ov["autopilot_level"] == "L4"
    drafts = svc.list_drafts(platform="line")
    d = next(x for x in drafts if x["draft_id"] == "line_pending:line-a:11")
    assert d["risk_level"] == "high"
    assert d["autopilot_level"] == "L4"
    store.close()


def test_apply_analysis_low_risk_auto_ai_allows_autosend(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store, line_services=[LineSvc()])
    res = svc.apply_analysis(
        "line_pending:line-a:11", {"risk_level": "low"}, automation_mode="auto_ai",
    )
    assert res["autopilot_level"] == "L2"
    assert res["autosend_allowed"] is True
    store.close()
