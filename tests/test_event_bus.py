"""EventBus 测试 — 发布/订阅/扇出/历史/取消订阅。"""
from __future__ import annotations

import asyncio

import pytest

from src.integrations.shared.event_bus import EventBus


class TestEventBus:

    def test_publish_without_subscribers(self):
        """无订阅者时发布不报错。"""
        bus = EventBus()
        bus.publish("test", {"key": "value"})
        assert len(bus.recent_events()) == 1

    def test_history_limit(self):
        """历史记录不超过 max_history。"""
        bus = EventBus()
        bus._max_history = 5
        for i in range(10):
            bus.publish("test", {"i": i})
        events = bus.recent_events()
        assert len(events) == 5
        assert events[0]["data"]["i"] == 5  # 最旧的保留
        assert events[-1]["data"]["i"] == 9  # 最新的

    def test_recent_events_limit(self):
        """recent_events(limit) 限制返回数量。"""
        bus = EventBus()
        for i in range(10):
            bus.publish("test", {"i": i})
        assert len(bus.recent_events(3)) == 3

    @pytest.mark.asyncio
    async def test_subscribe_receive(self):
        """订阅者能收到发布的事件。"""
        bus = EventBus()
        q = bus.subscribe()
        assert bus.subscriber_count == 1

        bus.publish("circuit_open", {"serial": "ABC123"})
        evt = q.get_nowait()
        assert evt["type"] == "circuit_open"
        assert evt["data"]["serial"] == "ABC123"
        assert "ts" in evt

    @pytest.mark.asyncio
    async def test_fanout_multiple_subscribers(self):
        """多个订阅者都收到同一事件。"""
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        assert bus.subscriber_count == 2

        bus.publish("recovery", {"serial": "DEF456"})
        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1["type"] == "recovery"
        assert e2["type"] == "recovery"

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """取消订阅后不再收到事件。"""
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0

        bus.publish("test", {})
        assert q.empty()

    @pytest.mark.asyncio
    async def test_queue_full_drops_oldest(self):
        """队列满时丢弃最旧事件而不阻塞。"""
        bus = EventBus()
        q = bus.subscribe()
        # 填满队列 (maxsize=100)
        for i in range(100):
            bus.publish("fill", {"i": i})

        # 再发一条 → 应丢弃最旧的
        bus.publish("new", {"i": 999})
        # 队列大小仍为 100
        assert q.qsize() == 100
        # 最新的应在队列中
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert items[-1]["data"]["i"] == 999

    def test_event_structure(self):
        """事件结构包含 type、ts、data。"""
        bus = EventBus()
        bus.publish("device_online", {"serial": "XYZ"})
        evt = bus.recent_events()[0]
        assert "type" in evt
        assert "ts" in evt
        assert "data" in evt
        assert isinstance(evt["ts"], float)
