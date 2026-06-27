"""告警投递端到端回归（E9）。

现有测试分两段、各自 mock 了中间层：
  - health_watchdog 测试 mock 了 EventBus，只断言"发了 *_alert 事件"；
  - webhook_notifier 测试只覆盖匹配/格式化/限流，未驱动 run() 真订阅真投递。

本文件补上**整条链**：真 EventBus → WebhookNotifier.run() 真订阅 → publish →
_dispatch → 真 _http_post。覆盖两条新告警（draft_quality / memory_key_drift），
并以 watchdog 驱动跑一遍全栈，确保"告警发了就一定投得出去"，而非"发了没人收"。

为什么值得：notifier 靠 _EVENT_ALIASES 把别名映到 event_type，若哪天别名漏了、
或 run()/_dispatch 链路改坏，单段测试都不会红——只有端到端能抓到"静默丢告警"。
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from src.inbox.webhook_notifier import WebhookNotifier


# ── 共享脚手架 ────────────────────────────────────────────────────────────────

def _reset_bus():
    """重置 EventBus 单例，隔离用例（notifier.run 与 publish 共用此单例）。"""
    from src.integrations.shared import event_bus as eb
    eb._bus = None
    return eb.get_event_bus()


def _patch_http(monkeypatch):
    """把真 HTTP POST 换成内存捕获（保留 _send→_http_post 整条投递路径）。"""
    captured: list = []

    def _fake_post(url, body, headers=None):
        captured.append({
            "url": url,
            "body": body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body,
            "headers": headers or {},
        })

    monkeypatch.setattr(WebhookNotifier, "_http_post", staticmethod(_fake_post))
    return captured


async def _drain_until(captured, n=1, timeout=2.0):
    """轮询等待捕获到至少 n 条投递（_http_post 在 executor 线程里回填）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(captured) < n and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    return captured


async def _run_notifier(events):
    """起一个订阅了 ``events`` 的 json notifier，返回 (notifier, task)。"""
    notifier = WebhookNotifier([{
        "name": "ops-json", "format": "json",
        "url": "https://example.test/hook", "events": events,
    }])
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0.05)  # 让 run() 先完成 bus.subscribe()
    return notifier, task


async def _stop(notifier, task):
    notifier.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()


# ── Level A：直接 publish 告警 → 投递 ─────────────────────────────────────────

@pytest.mark.parametrize("alias,etype,needle", [
    ("draft_quality", "draft_quality_alert", "草稿质量告警"),
    ("memory_key_drift", "memory_key_drift_alert", "记忆 key 漂移告警"),
])
async def test_alert_delivered_end_to_end(monkeypatch, alias, etype, needle):
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier([alias])
    try:
        bus.publish(etype, {
            "light": "red",
            "problems": [{"id": "x", "name": "测试项", "detail": "细节"}],
        })
        await _drain_until(captured, 1)
    finally:
        await _stop(notifier, task)

    assert captured, f"{etype} 应经 webhook 投递（真链路）"
    payload = json.loads(captured[0]["body"])
    assert needle in payload["title"]
    assert captured[0]["url"] == "https://example.test/hook"


async def test_all_alias_catches_new_alert_types(monkeypatch):
    """events:["all"] 的渠道应同时收到两类新告警（防别名遗漏导致静默丢失）。"""
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier(["all"])
    try:
        bus.publish("draft_quality_alert", {"light": "yellow", "problems": []})
        bus.publish("memory_key_drift_alert", {"light": "yellow", "problems": []})
        await _drain_until(captured, 2)
    finally:
        await _stop(notifier, task)

    titles = [json.loads(c["body"])["title"] for c in captured]
    assert any("草稿质量" in t for t in titles)
    assert any("记忆 key 漂移" in t for t in titles)


async def test_unsubscribed_alert_not_delivered(monkeypatch):
    """只订阅 draft_quality 的渠道，不应收到 memory_key_drift（反向隔离）。"""
    bus = _reset_bus()
    captured = _patch_http(monkeypatch)
    notifier, task = await _run_notifier(["draft_quality"])
    try:
        bus.publish("memory_key_drift_alert", {"light": "red", "problems": []})
        await asyncio.sleep(0.3)  # 给足时间，确认确实没有投递
    finally:
        await _stop(notifier, task)
    assert captured == []


# ── Level B：watchdog 驱动的全栈链路 ─────────────────────────────────────────

def _reset_draft_metrics():
    from src.monitoring import metrics_store as _ms
    _ms.MetricsStore._instance = None
    return _ms.get_metrics_store()


class _CM:
    def __init__(self, config):
        self.config = config


def _fake_app():
    state = types.SimpleNamespace()
    state.inbox_store = types.SimpleNamespace(ping=lambda: True)
    state.draft_service = types.SimpleNamespace(
        list_drafts=lambda status="pending", limit=1000: [{} for _ in range(10)])
    return types.SimpleNamespace(state=state)


async def test_full_stack_watchdog_to_webhook(monkeypatch):
    """watchdog._check_draft_quality 发现低命中率 → 真 bus → notifier → 真 _http_post。"""
    from src.inbox.health_watchdog import HealthWatchdog

    bus = _reset_bus()
    captured = _patch_http(monkeypatch)

    m = _reset_draft_metrics()
    for _ in range(30):
        m.record_inbox_draft_event("generated")
    for _ in range(3):  # 命中率 10% < 阈值 30%
        m.record_inbox_draft_event("memory_hit")

    notifier, task = await _run_notifier(["draft_quality"])
    try:
        cm = _CM({"ai": {"provider": "openai", "api_key": "sk-real-123"},
                  "inbox": {"auto_draft": {"quality_alert": {
                      "enabled": True, "min_samples": 10,
                      "memory_hit_min": 0.30, "p95_ms_max": 8000,
                      "fast_path_ratio_max": 0.98}}}})
        wd = HealthWatchdog(app=_fake_app(), config_manager=cm, interval_sec=60)
        wd._check_draft_quality()  # 同步 publish 到真 bus
        assert wd.total_draft_quality_alerts == 1
        await _drain_until(captured, 1)
    finally:
        await _stop(notifier, task)

    assert captured, "watchdog 告警应一路投递到 webhook（全栈）"
    payload = json.loads(captured[0]["body"])
    assert "草稿质量告警" in payload["title"]
    assert any("memory_hit_low" == p.get("id") for p in payload["data"].get("problems", []))
