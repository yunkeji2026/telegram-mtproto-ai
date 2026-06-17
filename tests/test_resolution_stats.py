"""M6 AI 解决率度量测试：store.get_resolution_stats + ROI 接线。

口径覆盖：
- AI 独立解决（仅 autosend）→ ai_handled。
- 转人工（含人工动作）→ human_handled，不计入 ai_handled。
- 再联系（autosend 后静默又在 72h 内回来）→ reopened，扣减 ai_resolved。
- 静默后间隔超 72h（新问题）/ 同一突发（间隔 < 会话间隔）→ 不算 reopened。
- 比率计算与窗口过滤。
"""

import time

from src.inbox.store import InboxStore


def _audit(store, cid, action, ts):
    store.record_draft_audit(
        f"d-{cid}-{ts}", action=action, conversation_id=cid, ts=ts,
        autopilot_level="L2", risk_level="low",
    )


def test_ai_handled_vs_human_handled(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    base = time.time() - 86400
    # conv A：仅 AI 自动发 → ai_handled
    _audit(store, "A", "autosend", base)
    # conv B：AI 发后人工编辑发 → human_handled（不算 AI 独立）
    _audit(store, "B", "autosend", base)
    _audit(store, "B", "edit_send", base + 60)
    # conv C：人工 approved → human_handled
    _audit(store, "C", "approved", base)
    # conv D：仅 blocked/rejected（无实际处置）→ 不计入
    _audit(store, "D", "blocked", base)

    st = store.get_resolution_stats(since_ts=base - 10)
    assert st["ai_handled"] == 1          # 仅 A
    assert st["human_handled"] == 2       # B、C
    assert st["decided"] == 3
    assert st["ai_resolution_rate"] == round(1 / 3, 4)
    assert st["escalation_rate"] == round(2 / 3, 4)


def test_recontact_marks_reopened(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    base = time.time() - 5 * 86400
    # conv R：AI 处理后静默 5h（>1h 会话间隔），仍在 72h 内 → reopened
    _audit(store, "R", "autosend", base)
    _audit(store, "R", "autosend", base + 5 * 3600)
    # conv K：AI 处理后再次活动仅隔 5 分钟（同一突发）→ 不算 reopened
    _audit(store, "K", "autosend", base)
    _audit(store, "K", "autosend", base + 300)

    st = store.get_resolution_stats(since_ts=base - 10)
    assert st["ai_handled"] == 2
    assert st["reopened"] == 1            # 仅 R
    assert st["ai_resolved"] == 1
    assert st["recontact_rate"] == round(1 / 2, 4)
    assert st["true_resolution_rate"] == round(1 / 2, 4)


def test_gap_beyond_window_is_new_issue(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    base = time.time() - 10 * 86400
    # 间隔 80h（> 72h 窗口）→ 视为全新问题，不算再联系
    _audit(store, "N", "autosend", base)
    _audit(store, "N", "autosend", base + 80 * 3600)
    st = store.get_resolution_stats(since_ts=base - 10)
    assert st["ai_handled"] == 1
    assert st["reopened"] == 0
    assert st["recontact_rate"] == 0.0


def test_window_filters_old_events(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _audit(store, "OLD", "autosend", now - 100 * 86400)   # 窗口外
    _audit(store, "NEW", "autosend", now - 3600)          # 窗口内
    st = store.get_resolution_stats(since_ts=now - 7 * 86400)
    assert st["ai_handled"] == 1
    assert st["decided"] == 1


def test_empty_store_is_zero(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    st = store.get_resolution_stats(since_ts=0)
    assert st["decided"] == 0
    assert st["ai_resolution_rate"] == 0.0
    assert st["recontact_rate"] == 0.0
    assert st["recontact_window_hours"] == 72.0


def test_roi_summary_includes_resolution(tmp_path):
    """build_roi_summary 接入 resolution 段，store 在场时给出真实数字。"""
    from types import SimpleNamespace
    from src.web.routes.unified_inbox_roi import build_roi_summary

    store = InboxStore(tmp_path / "inbox.db")
    base = time.time() - 3600
    _audit(store, "A", "autosend", base)
    _audit(store, "B", "approved", base)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))
    out = build_roi_summary(request, config_manager=None, span=7)
    res = out["resolution"]
    assert res["ai_handled"] == 1
    assert res["human_handled"] == 1
    assert res["ai_resolution_rate_pct"] == 50.0
    assert res["recontact_window_hours"] == 72.0
