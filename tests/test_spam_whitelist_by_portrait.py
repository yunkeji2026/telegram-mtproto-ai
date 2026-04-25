"""优化 B — 画像反向白名单：已建立画像 + ≥ N 入站 → spam HIGH 也只单次跳过。

直接测 runner._is_spam_whitelisted_contact 决策逻辑。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.integrations.messenger_rpa.runner import MessengerRpaRunner


def _make_runner_skeleton(cfg=None) -> MessengerRpaRunner:
    """绕过 init 拿到 instance（避免 ConfigManager + StateStore 依赖）。"""
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._cfg = cfg or {}
    r._contact_hooks = None
    return r


def _make_hooks_with_store(store):
    hooks = MagicMock()
    hooks._gw = MagicMock()
    hooks._gw._store = store
    return hooks


def test_no_hooks_returns_not_whitelisted():
    r = _make_runner_skeleton()
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert info["reason"] == "no_hooks"


def test_no_contact_returns_not_whitelisted():
    store = MagicMock()
    store.get_ci_by_external.return_value = None
    r = _make_runner_skeleton()
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert info["reason"] == "no_contact"


def test_contact_exists_but_no_portrait_below_min_inbound():
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = ""  # 无 portrait
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": 100} for _ in range(2)
    ]
    r = _make_runner_skeleton()
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert info["msg_in_count"] == 2


def test_portrait_exists_and_enough_inbound_returns_whitelisted():
    """优化 B 黄金路径：5+ 入站 + portrait 已写 → 白名单。"""
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = '{"language":"ja"}'
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i} for i in range(7)
    ] + [
        {"event_type": "msg_out", "ts": 100},
    ]
    r = _make_runner_skeleton()
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is True
    assert info["msg_in_count"] == 7
    assert info["has_portrait"] is True


def test_portrait_required_but_missing_blocks_whitelist():
    """默认 require_portrait=True → 即使 8 入站没 portrait 仍非白名单。"""
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = ""
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i} for i in range(8)
    ]
    r = _make_runner_skeleton()
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert info["msg_in_count"] == 8
    assert info["has_portrait"] is False


def test_can_relax_require_portrait_via_config():
    """运营把 require_portrait=false → 8 入站无 portrait 也可白名单。"""
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = ""
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i} for i in range(8)
    ]
    r = _make_runner_skeleton(cfg={
        "spam_whitelist": {"require_portrait": False, "min_inbound_msgs": 5}
    })
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is True


def test_min_inbound_configurable():
    """运营可改阈值：min_inbound_msgs=3 → 4 入站即白名单。"""
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = '{"language":"ja"}'
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i} for i in range(4)
    ]
    r = _make_runner_skeleton(cfg={"spam_whitelist": {"min_inbound_msgs": 3}})
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is True


def test_disabled_via_config_returns_not_whitelisted():
    """spam_whitelist.enabled=false → 即便满足条件也不白名单（紧急关闭）。"""
    store = MagicMock()
    ci = MagicMock(); ci.contact_id = "c1"
    store.get_ci_by_external.return_value = ci
    journey = MagicMock()
    journey.context_snapshot_json = '{"language":"ja"}'
    journey.journey_id = "j1"
    store.get_journey_by_contact.return_value = journey
    store.list_events.return_value = [
        {"event_type": "msg_in", "ts": i} for i in range(7)
    ]
    r = _make_runner_skeleton(cfg={"spam_whitelist": {"enabled": False}})
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert info["reason"] == "disabled_by_config"


def test_store_exception_fails_safe():
    store = MagicMock()
    store.get_ci_by_external.side_effect = RuntimeError("db locked")
    r = _make_runner_skeleton()
    r._contact_hooks = _make_hooks_with_store(store)
    wh, info = r._is_spam_whitelisted_contact("acc1", "user_a")
    assert wh is False
    assert "exception" in info["reason"]
