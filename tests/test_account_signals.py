"""N 线 N3 信号接线 + N6 机群概览：account_signals 单测。

用假 registry/limiter 验证信号装配、生命周期推断、机群概览（不依赖真号）。
"""
import time

from src.skills.account_signals import (
    STAGE_ACTIVE,
    STAGE_BANNED,
    STAGE_PENDING,
    STAGE_RESTRICTED,
    STAGE_WARMING,
    build_account_signals,
    fleet_overview,
    lifecycle_stage,
)

NOW = 1_000_000.0


class _FakeRegistry:
    def __init__(self, rows):
        # rows: {(platform, account_id): dict}
        self._rows = rows

    def get(self, platform, account_id):
        return self._rows.get((str(platform).lower(), str(account_id)))


class _FakeLimiter:
    def __init__(self, snaps):
        self._snaps = snaps  # {account_key: {day_used, circuit_open}}

    def snapshot(self, account_key, now=None):
        return self._snaps.get(account_key, {"day_used": 0})


# ── build_account_signals ────────────────────────────────────────────────────

def test_signals_from_registry_age_and_proxy():
    reg = _FakeRegistry({
        ("telegram", "a"): {
            "created_at": NOW - 5 * 86400, "proxy_id": "px", "status": "online",
            "meta": {},
        },
    })
    sig = build_account_signals("telegram", "a", registry=reg, now=NOW)
    assert round(sig["age_days"]) == 5
    assert sig["proxy_bound"] is True
    assert sig["banned"] is False


def test_signals_banned_from_status_removed():
    reg = _FakeRegistry({("telegram", "x"): {"status": "removed", "meta": {}}})
    sig = build_account_signals("telegram", "x", registry=reg, now=NOW)
    assert sig["banned"] is True


def test_signals_banned_from_meta_flag():
    reg = _FakeRegistry({("telegram", "x"): {"status": "online", "meta": {"banned": True}}})
    sig = build_account_signals("telegram", "x", registry=reg, now=NOW)
    assert sig["banned"] is True


def test_signals_sends_today_from_limiter():
    reg = _FakeRegistry({("telegram", "a"): {"created_at": NOW - 86400, "proxy_id": "p", "meta": {}}})
    lim = _FakeLimiter({"telegram:a": {"day_used": 7, "circuit_open": False}})
    sig = build_account_signals("telegram", "a", registry=reg, limiter=lim, now=NOW)
    assert sig["sends_today"] == 7
    assert "_circuit_open" not in sig


def test_signals_circuit_open_flag():
    lim = _FakeLimiter({"telegram:a": {"day_used": 3, "circuit_open": True}})
    sig = build_account_signals("telegram", "a", limiter=lim, now=NOW)
    assert sig["_circuit_open"] is True


def test_signals_no_registry_benign_defaults():
    sig = build_account_signals("telegram", "a", now=NOW)
    assert sig["proxy_bound"] is False
    assert sig["banned"] is False
    assert "age_days" not in sig  # 无 created_at → 不带（视为良性）


# ── lifecycle_stage ──────────────────────────────────────────────────────────

def test_stage_banned_wins():
    assert lifecycle_stage({"banned": True}, "online") == STAGE_BANNED
    assert lifecycle_stage({}, "removed") == STAGE_BANNED


def test_stage_restricted_on_circuit():
    assert lifecycle_stage({"_circuit_open": True}, "online") == STAGE_RESTRICTED


def test_stage_pending():
    assert lifecycle_stage({}, "pending") == STAGE_PENDING


def test_stage_warming_within_ramp():
    assert lifecycle_stage({"age_days": 3}, "online", warmup_ramp_days=14) == STAGE_WARMING


def test_stage_active_after_ramp():
    assert lifecycle_stage({"age_days": 30}, "online") == STAGE_ACTIVE


# ── fleet_overview ───────────────────────────────────────────────────────────

def test_fleet_overview_aggregates_health_and_lifecycle():
    reg = _FakeRegistry({
        ("telegram", "good"): {
            "created_at": NOW - 30 * 86400, "proxy_id": "px",
            "status": "online", "meta": {},
        },
        ("telegram", "new"): {
            "created_at": NOW - 2 * 86400, "proxy_id": "px2",
            "status": "online", "meta": {},
        },
        ("telegram", "bad"): {"status": "removed", "meta": {"banned": True}},
    })
    lim = _FakeLimiter({
        "telegram:good": {"day_used": 3},
        "telegram:new": {"day_used": 0},
    })
    accounts = [
        ("telegram", "good", "online"),
        ("telegram", "new", "online"),
        ("telegram", "bad", "removed"),
    ]
    ov = fleet_overview(accounts, registry=reg, limiter=lim, now=NOW)
    assert ov["total"] == 3
    # 有封禁号 → 机群最差灯 red
    assert ov["fleet"]["fleet_light"] == "red"
    # 生命周期：1 active + 1 warming + 1 banned
    assert ov["lifecycle"].get(STAGE_ACTIVE) == 1
    assert ov["lifecycle"].get(STAGE_WARMING) == 1
    assert ov["lifecycle"].get(STAGE_BANNED) == 1


def test_fleet_overview_accepts_dicts():
    ov = fleet_overview(
        [{"platform": "telegram", "account_id": "a", "status": "pending"}],
        now=NOW,
    )
    assert ov["total"] == 1
    assert ov["accounts"][0]["stage"] == STAGE_PENDING


def test_fleet_overview_empty():
    ov = fleet_overview([], now=NOW)
    assert ov["total"] == 0
    assert ov["fleet"]["fleet_light"] == "unknown"


# ── 优化1：统一发送计数器（A 线 record → 同一 limiter → 闸门读到） ──────────────

def test_unified_counter_feeds_signals_and_gate():
    """A 线把发送记进共用 AutoReplyLimiter → build_account_signals 读到 sends_today →
    闸门按预热上限拦截。验证'一个计数器喂两线'闭环。"""
    from src.integrations.protocol_autoreply_limits import (
        AutoReplyLimiter,
        get_autoreply_limiter,
        reset_autoreply_limiter,
    )
    from src.skills.companion_send_gate import evaluate

    reset_autoreply_limiter()
    try:
        # persist=false：本用例测「内存统一计数器」语义，不落 DB（保持测试 hermetic）
        _cfg_nopersist = {"protocol_autoreply": {"rate": {"persist": False}}}
        lim = get_autoreply_limiter(_cfg_nopersist)
        assert isinstance(lim, AutoReplyLimiter)
        key = "telegram:acct_unified"
        # 新号当天预热上限 = start_cap(2)；A 线连发 3 条记入同一计数器
        for _ in range(3):
            lim.record_sent(key)

        sig = build_account_signals("telegram", "acct_unified", limiter=lim)
        assert sig["sends_today"] == 3

        cfg = {"companion_send_gate": {"enabled": True}}
        # 无 registry → age_days 缺省视为 0（新号）→ 预热上限 2，已发 3 → 拦截
        dec = evaluate(sig, cfg)
        assert dec["allowed"] is False
        assert dec["reason"] == "warmup_cap"
    finally:
        reset_autoreply_limiter()


def test_shared_send_limiter_returns_singleton():
    """sender._shared_send_limiter 取到的就是 B 线共用的那个单例。"""
    from src.integrations.protocol_autoreply_limits import (
        get_autoreply_limiter,
        reset_autoreply_limiter,
    )

    reset_autoreply_limiter()
    try:
        from src.client.sender import TelegramSenderMixin
        obj = TelegramSenderMixin.__new__(TelegramSenderMixin)
        # persist=false：仅验单例身份，不在测试期创建真实 config/account_sends.db
        _cfg = {"protocol_autoreply": {"rate": {"persist": False}}}
        lim = obj._shared_send_limiter(_cfg)
        assert lim is get_autoreply_limiter(_cfg)
    finally:
        reset_autoreply_limiter()
