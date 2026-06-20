"""N 线 核心3：共享发送前反封号闸门 companion_send_gate 单测。

验证两线共用的 gate_decision / evaluate（默认关零破坏）/ aggregate_fleet，
复用 M7 account_health 逻辑且决策正确。
"""
from src.skills.companion_send_gate import (
    aggregate_fleet,
    evaluate,
    gate_decision,
    gate_enabled,
)


# ── gate_enabled ─────────────────────────────────────────────────────────────

def test_gate_disabled_by_default():
    assert gate_enabled(None) is False
    assert gate_enabled({}) is False
    assert gate_enabled({"companion_send_gate": {}}) is False


def test_gate_enabled_flag():
    assert gate_enabled({"companion_send_gate": {"enabled": True}}) is True


# ── gate_decision ────────────────────────────────────────────────────────────

def test_healthy_account_allowed():
    dec = gate_decision({"age_days": 30, "sends_today": 3, "proxy_bound": True})
    assert dec["allowed"] is True
    assert dec["reason"] == "ok"
    assert dec["light"] == "green"


def test_banned_account_blocked():
    dec = gate_decision({"banned": True})
    assert dec["allowed"] is False
    assert dec["reason"] == "banned"


def test_warmup_cap_blocks_new_account_burst():
    # 新号当天预热上限 = start_cap(2)；已发 5 → 超限拦截
    dec = gate_decision({"age_days": 0, "sends_today": 5, "proxy_bound": True})
    assert dec["allowed"] is False
    assert dec["reason"] == "warmup_cap"
    assert dec["recommended_cap"] == 2


def test_new_account_within_warmup_allowed():
    dec = gate_decision({"age_days": 0, "sends_today": 1, "proxy_bound": True})
    assert dec["allowed"] is True


def test_red_light_blocks_when_block_on_red():
    # 无代理(-35) + 多次 flood → 红灯
    sig = {"proxy_bound": False, "flood_waits_24h": 5, "errors_24h": 3}
    dec = gate_decision(sig, block_on_red=True)
    assert dec["light"] == "red"
    assert dec["allowed"] is False
    assert dec["reason"] == "health_red"


def test_red_light_not_blocking_when_disabled():
    sig = {"proxy_bound": False, "flood_waits_24h": 5, "errors_24h": 3,
           "age_days": 30, "sends_today": 0}
    dec = gate_decision(sig, block_on_red=False)
    # 红灯但不因红灯拦；且未超 cap → 放行
    assert dec["light"] == "red"
    assert dec["allowed"] is True


# ── evaluate（config 驱动，默认关零破坏） ────────────────────────────────────

def test_evaluate_disabled_always_allows():
    # 即使是被封账号，闸门关闭时也恒放行（零破坏）
    dec = evaluate({"banned": True}, config={})
    assert dec["allowed"] is True
    assert dec["reason"] == "disabled"


def test_evaluate_enabled_blocks_banned():
    cfg = {"companion_send_gate": {"enabled": True}}
    dec = evaluate({"banned": True}, config=cfg)
    assert dec["allowed"] is False
    assert dec["reason"] == "banned"


def test_evaluate_respects_custom_thresholds():
    cfg = {"companion_send_gate": {
        "enabled": True, "target_cap": 100,
        "warmup_start_cap": 10, "warmup_ramp_days": 7,
    }}
    # 老号 target 100，已发 50 < cap → 放行
    dec = evaluate({"age_days": 30, "sends_today": 50, "proxy_bound": True}, config=cfg)
    assert dec["allowed"] is True
    assert dec["recommended_cap"] == 100


# ── aggregate_fleet ──────────────────────────────────────────────────────────

def test_aggregate_fleet_worst_light_wins():
    accounts = [
        {"account_id": "a", "age_days": 30, "proxy_bound": True},
        {"account_id": "b", "banned": True},
    ]
    fleet = aggregate_fleet(accounts)
    assert fleet["fleet_light"] == "red"
    assert fleet["total"] == 2
    assert fleet["counts"]["red"] == 1
    # 最差账号排在前
    assert fleet["accounts"][0]["account_id"] == "b"


def test_aggregate_fleet_empty():
    fleet = aggregate_fleet([])
    assert fleet["fleet_light"] == "unknown"
    assert fleet["total"] == 0
