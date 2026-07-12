"""全自动发送安全视图聚合器单测（纯函数 core，零 IO）。"""

from __future__ import annotations

from src.inbox.send_health import (
    FAIL_GATE, FAIL_PERMANENT, FAIL_PLATFORM, FAIL_OTHER,
    account_from_conv, classify_fail_reason, compute_send_health,
    is_sender_account, reply_stats,
)


def test_is_sender_account_excludes_removed():
    assert is_sender_account("online") is True
    assert is_sender_account("pending") is True
    assert is_sender_account("") is True
    assert is_sender_account("removed") is False
    assert is_sender_account("REMOVED") is False


# ── 解析 / 归因 ──────────────────────────────────────────────────────────────

def test_account_from_conv():
    assert account_from_conv("telegram:8244899900:5898110595") == ("telegram", "8244899900")
    assert account_from_conv("MESSENGER:100:abc") == ("messenger", "100")
    assert account_from_conv("garbage") == ("", "")
    assert account_from_conv("") == ("", "")


def test_classify_fail_reason():
    assert classify_fail_reason("send_gate:warmup_cap") == FAIL_GATE
    assert classify_fail_reason("kill_switch:global") == FAIL_GATE
    assert classify_fail_reason("canary_hold") == FAIL_GATE
    assert classify_fail_reason("平台投递失败: 无发言权") == FAIL_PERMANENT
    assert classify_fail_reason("平台投递失败: 500 Internal Server Error") == FAIL_PLATFORM
    assert classify_fail_reason("something weird") == FAIL_OTHER


# ── compute_send_health ──────────────────────────────────────────────────────

def _sig(acc, **kw):
    d = {"platform": "telegram", "account_id": acc}
    d.update(kw)
    return d


def test_healthy_account_ok():
    accounts = [_sig("A", age_days=40, proxy_bound=True, sends_today=10)]
    audit = [{"action": "autosend", "conversation_id": "telegram:A:c1", "reason": ""}] * 10
    rep = compute_send_health(accounts=accounts, audit_24h=audit,
                              config={"companion_send_gate": {"target_cap": 80}})
    a = rep["accounts"][0]
    assert a["level"] == "ok"
    assert a["sends_today"] == 10 and a["recommended_cap"] == 80
    assert a["delivered_24h"] == 10 and a["failed_24h"] == 0
    assert rep["fleet_level"] == "ok"


def test_approaching_cap_watch():
    accounts = [_sig("A", age_days=40, proxy_bound=True, sends_today=68)]
    rep = compute_send_health(accounts=accounts, audit_24h=[],
                              config={"companion_send_gate": {"target_cap": 80}})
    a = rep["accounts"][0]
    assert a["level"] == "watch"        # 68/80 = 85% ≥ 80%
    assert "接近上限" in a["reason"]


def test_over_cap_risk():
    accounts = [_sig("A", age_days=40, proxy_bound=True, sends_today=85)]
    rep = compute_send_health(accounts=accounts, audit_24h=[],
                              config={"companion_send_gate": {"target_cap": 80}})
    a = rep["accounts"][0]
    assert a["level"] == "risk"
    assert "上限" in a["reason"]


def test_banned_account_risk_red():
    accounts = [_sig("A", banned=True, sends_today=0)]
    rep = compute_send_health(accounts=accounts, audit_24h=[], config={})
    a = rep["accounts"][0]
    assert a["light"] == "red" and a["level"] == "risk"


def test_platform_failures_drive_risk_but_gate_holds_do_not():
    """关键区分：平台报错高失败率=risk；闸门拦截即便多也不算故障。"""
    accounts = [_sig("A", age_days=40, proxy_bound=True, sends_today=10),
                _sig("B", age_days=40, proxy_bound=True, sends_today=10)]
    # A：3 达 + 3 平台报错 → 失败率 50% 且含平台错 → risk
    audit_a = ([{"action": "autosend", "conversation_id": "telegram:A:c", "reason": ""}] * 3
               + [{"action": "autosend_failed", "conversation_id": "telegram:A:c",
                   "reason": "平台投递失败: 500 Internal Server Error"}] * 3)
    # B：3 达 + 3 闸门 hold → 不算故障（闸门=预期节流）
    audit_b = ([{"action": "autosend", "conversation_id": "telegram:B:c", "reason": ""}] * 3
               + [{"action": "autosend_failed", "conversation_id": "telegram:B:c",
                   "reason": "send_gate:warmup_cap"}] * 3)
    rep = compute_send_health(accounts=accounts, audit_24h=audit_a + audit_b,
                              config={"companion_send_gate": {"target_cap": 80}})
    by = {a["account_id"]: a for a in rep["accounts"]}
    assert by["A"]["level"] == "risk"
    assert by["A"]["fail_by_cat"][FAIL_PLATFORM] == 3
    assert by["B"]["level"] == "ok"           # 闸门拦截不升级为 risk
    assert by["B"]["fail_by_cat"][FAIL_GATE] == 3


def test_risk_sorted_first():
    accounts = [_sig("ok1", age_days=40, proxy_bound=True, sends_today=1),
                _sig("bad", banned=True),
                _sig("watch1", age_days=40, proxy_bound=True, sends_today=68)]
    rep = compute_send_health(accounts=accounts, audit_24h=[],
                              config={"companion_send_gate": {"target_cap": 80}})
    levels = [a["level"] for a in rep["accounts"]]
    assert levels[0] == "risk"                # 最该看的在最前
    assert levels.index("watch") < levels.index("ok")


def test_reply_view_attached():
    accounts = [_sig("A", age_days=40, proxy_bound=True, sends_today=5)]
    audit = [{"action": "autosend", "conversation_id": "telegram:A:c1", "reason": ""}]
    reply_by = {"telegram:A": {"autosent_convs": 4, "replied_convs": 3}}
    rep = compute_send_health(accounts=accounts, audit_24h=audit, config={},
                              reply_by_account=reply_by)
    a = rep["accounts"][0]
    assert a["reply"]["reply_rate"] == 0.75
    assert a["reply"]["replied_convs"] == 3


# ── reply_stats（注入 inbound_exists）─────────────────────────────────────────

def test_reply_stats_counts_replied_convs():
    audit = [
        {"action": "autosend", "conversation_id": "telegram:A:c1", "reason": ""},
        {"action": "autosend", "conversation_id": "telegram:A:c2", "reason": ""},
        {"action": "autosend_failed", "conversation_id": "telegram:A:c3", "reason": "x"},
    ]
    replied = {"telegram:A:c1"}   # 只有 c1 有客户回
    out = reply_stats(audit, since=0.0,
                      inbound_exists=lambda cid, s: cid in replied)
    assert out["telegram:A"] == {"autosent_convs": 2, "replied_convs": 1}  # c3 是 failed 不计
