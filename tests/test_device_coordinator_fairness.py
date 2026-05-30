"""S2-P0B: 测试 device_coordinator 排程公平性（per-platform force + 最久未跑优先）。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.integrations.shared.device_coordinator import DeviceCoordinator, PlatformRunner


# ── 辅助 ───────────────────────────────────────────────────────────────────

def _make_runner(step: str = "no_unread", ok: bool = True) -> Any:
    r = AsyncMock()
    r.run_once = AsyncMock(return_value={"step": step, "ok": ok})
    return r


def _make_dc(
    types: List[str],
    force_check: float = 120.0,
    run_timeout: float = 10.0,
) -> DeviceCoordinator:
    prs = [PlatformRunner(t, _make_runner(), account_id=t) for t in types]
    dc = DeviceCoordinator(
        serial="TESTSERIAL",
        platform_runners=prs,
        label="test",
        poll_interval_sec=1.0,
        idle_poll_interval_sec=2.0,
        force_check_interval_sec=force_check,
        run_timeout_sec=run_timeout,
    )
    return dc


# ── 测试：per-platform force 让长时间未跑的平台强制进入 ────────────────────

def test_per_platform_force_includes_idle_platform() -> None:
    """当 WA 超过 force_check_interval 未运行时应被纳入 platforms_to_run，即使无 badge。"""
    dc = _make_dc(["messenger", "whatsapp"], force_check=5.0)
    now = time.time()
    # WA 上次 force 检查是 10s 前（超过 5s 阈值）
    dc._platform_last_force_ts["whatsapp"] = now - 10.0
    # Messenger 刚刚 force 过（未超时）
    dc._platform_last_force_ts["messenger"] = now - 1.0

    badges: Dict[str, int] = {"messenger": 0, "whatsapp": 0}

    to_run: List[PlatformRunner] = []
    for p in dc._platforms:
        if now < p.skip_until:
            continue
        badge_now = badges.get(p.platform_type, 0)
        _last_force_p = dc._platform_last_force_ts.get(p.platform_type, 0.0)
        _per_platform_force = (now - _last_force_p) >= dc._force_check_interval
        if _per_platform_force or badge_now > 0:
            to_run.append(p)
            if _per_platform_force:
                dc._platform_last_force_ts[p.platform_type] = now

    types_to_run = [p.platform_type for p in to_run]
    assert "whatsapp" in types_to_run, "WA 超时应被强制纳入"
    assert "messenger" not in types_to_run, "Messenger 未超时且无 badge 不应运行"


def test_badge_platform_always_included() -> None:
    """有 badge 的平台即使刚刚 force 过也必须运行。"""
    dc = _make_dc(["messenger", "whatsapp"], force_check=120.0)
    now = time.time()
    # 两个都刚 force 过
    dc._platform_last_force_ts["messenger"] = now - 1.0
    dc._platform_last_force_ts["whatsapp"] = now - 1.0

    badges: Dict[str, int] = {"messenger": 3, "whatsapp": 0}

    to_run: List[PlatformRunner] = []
    for p in dc._platforms:
        badge_now = badges.get(p.platform_type, 0)
        _last_force_p = dc._platform_last_force_ts.get(p.platform_type, 0.0)
        _per_platform_force = (now - _last_force_p) >= dc._force_check_interval
        if _per_platform_force or badge_now > 0:
            to_run.append(p)

    types_to_run = [p.platform_type for p in to_run]
    assert "messenger" in types_to_run, "有 badge 必须运行"
    assert "whatsapp" not in types_to_run, "无 badge 且未超时不运行"


# ── 测试：最久未跑优先排序 ────────────────────────────────────────────────

def test_sort_longest_idle_first_no_badge() -> None:
    """无 badge 的多平台 force 触发，最久未跑的排第一。"""
    dc = _make_dc(["messenger", "whatsapp", "line"], force_check=5.0)
    now = time.time()
    dc._platforms[0].last_run_ts = now - 10.0  # messenger: 10s ago
    dc._platforms[1].last_run_ts = now - 60.0  # whatsapp:  60s ago (最久)
    dc._platforms[2].last_run_ts = now - 30.0  # line:      30s ago

    badges: Dict[str, int] = {}
    dc._platforms.sort(
        key=lambda p: (
            0 if badges.get(p.platform_type, 0) > 0 else 1,
            p.last_run_ts,
        )
    )
    order = [p.platform_type for p in dc._platforms]
    assert order.index("whatsapp") < order.index("line"), "whatsapp(60s) 应在 line(30s) 前"
    assert order.index("line") < order.index("messenger"), "line(30s) 应在 messenger(10s) 前"


def test_sort_badge_beats_idle() -> None:
    """有 badge 的平台排最前，即使它刚刚跑过。"""
    dc = _make_dc(["messenger", "whatsapp"], force_check=5.0)
    now = time.time()
    dc._platforms[0].last_run_ts = now - 5.0   # messenger: 5s ago, badge=0
    dc._platforms[1].last_run_ts = now - 200.0  # whatsapp: 200s ago, badge=1

    badges: Dict[str, int] = {"messenger": 0, "whatsapp": 1}
    dc._platforms.sort(
        key=lambda p: (
            0 if badges.get(p.platform_type, 0) > 0 else 1,
            p.last_run_ts,
        )
    )
    order = [p.platform_type for p in dc._platforms]
    assert order[0] == "whatsapp", "有 badge 的 whatsapp 应排第一"


# ── 测试：熔断仍正常跳过 ──────────────────────────────────────────────────

def test_circuit_broken_platform_skipped() -> None:
    """熔断中的平台应被跳过，即使 force 触发。"""
    dc = _make_dc(["messenger"], force_check=0.0)  # force_check=0 确保立即 force
    dc._platforms[0].skip_until = time.time() + 300.0  # 熔断 5min

    now = time.time()
    to_run = []
    for p in dc._platforms:
        if now < p.skip_until:
            continue
        to_run.append(p)

    assert len(to_run) == 0, "熔断中的平台不应运行"


# ── 测试：on_circuit_open 回调 ────────────────────────────────────────────

def test_on_circuit_open_callback_fires() -> None:
    """当熔断触发时 on_circuit_open 回调被调用。"""
    alerts = []

    def _on_alert(serial, platform, consecutive):
        alerts.append((serial, platform, consecutive))

    prs = [PlatformRunner("line", _make_runner("fail", False), account_id="line_1")]
    dc = DeviceCoordinator(
        serial="ALERT_SER",
        platform_runners=prs,
        label="alert-test",
        circuit_breaker_threshold=3,
        on_circuit_open=_on_alert,
    )

    # 模拟连续失败达到阈值
    dc._platforms[0].consecutive_fail = 3
    dc._maybe_trip_breaker(dc._platforms[0])

    assert len(alerts) == 1
    assert alerts[0] == ("ALERT_SER", "line", 3)


def test_on_circuit_open_not_called_below_threshold() -> None:
    """未达阈值时 on_circuit_open 不被调用。"""
    alerts = []

    def _on_alert(serial, platform, consecutive):
        alerts.append((serial, platform, consecutive))

    prs = [PlatformRunner("messenger", _make_runner("fail", False), account_id="msg_1")]
    dc = DeviceCoordinator(
        serial="BELOW_SER",
        platform_runners=prs,
        label="below-test",
        circuit_breaker_threshold=5,
        on_circuit_open=_on_alert,
    )

    dc._platforms[0].consecutive_fail = 4
    dc._maybe_trip_breaker(dc._platforms[0])

    assert len(alerts) == 0, "未达阈值不应触发 callback"


# ── 测试：告警冷却 ────────────────────────────────────────────────────────

def test_alert_cooldown_suppresses_rapid_alerts() -> None:
    """冷却期内同平台不重复告警。"""
    alerts = []

    def _on_alert(serial, platform, consecutive):
        alerts.append((serial, platform, consecutive))

    prs = [PlatformRunner("line", _make_runner("fail", False), account_id="line_1")]
    dc = DeviceCoordinator(
        serial="COOL_SER",
        platform_runners=prs,
        label="cool-test",
        circuit_breaker_threshold=3,
        on_circuit_open=_on_alert,
        alert_cooldown_sec=600.0,
    )

    # 第一次熔断 → 告警
    dc._platforms[0].consecutive_fail = 3
    dc._maybe_trip_breaker(dc._platforms[0])
    assert len(alerts) == 1

    # 连续失败继续，但在冷却期内 → 不告警
    dc._platforms[0].consecutive_fail = 6
    dc._maybe_trip_breaker(dc._platforms[0])
    assert len(alerts) == 1, "冷却期内不应重复告警"


# ── 测试：run_history ──────────────────────────────────────────────────────

def test_run_history_recorded() -> None:
    """PlatformRunner.run_history 应按 appendleft 顺序记录。"""
    pr = PlatformRunner("line", _make_runner(), account_id="line_1")
    assert len(pr.run_history) == 0

    # 模拟记录
    pr.run_history.appendleft({"ts": 100.0, "step": "sent", "ok": True, "badge": 1})
    pr.run_history.appendleft({"ts": 101.0, "step": "fail", "ok": False, "badge": 0})
    assert len(pr.run_history) == 2
    assert pr.run_history[0]["step"] == "fail"  # 最新在前
    assert pr.run_history[1]["step"] == "sent"


def test_run_history_maxlen() -> None:
    """run_history 超过 maxlen 自动丢弃旧条目。"""
    pr = PlatformRunner("messenger", _make_runner(), account_id="msg_1")
    for i in range(25):
        pr.run_history.appendleft({"ts": float(i), "step": f"s{i}", "ok": True, "badge": 0})
    assert len(pr.run_history) == 20  # maxlen=20


# ── 测试：was_circuit_open 状态追踪 ──────────────────────────────────────

def test_was_circuit_open_tracks_state() -> None:
    """_maybe_trip_breaker 设置 was_circuit_open=True。"""
    prs = [PlatformRunner("whatsapp", _make_runner("fail", False), account_id="wa_1")]
    dc = DeviceCoordinator(
        serial="WC_SER",
        platform_runners=prs,
        label="wc-test",
        circuit_breaker_threshold=3,
    )

    assert dc._platforms[0].was_circuit_open is False
    dc._platforms[0].consecutive_fail = 3
    dc._maybe_trip_breaker(dc._platforms[0])
    assert dc._platforms[0].was_circuit_open is True
