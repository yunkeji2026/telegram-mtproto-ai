"""全局事件总线 — 用于 SSE 实时推送设备状态变更。

设计原则：
  - 生产者: DeviceCoordinator / HotPlugWatcher 发布事件
  - 消费者: SSE 端点订阅事件流
  - 基于 asyncio.Queue 扇出到多个 SSE 客户端
  - 无依赖：纯标准库 + asyncio

内存安全：
  - 每个 SSE 客户端有独立 queue（maxsize=100）
  - 客户端断开后自动移除 queue
  - queue 满时丢弃最旧事件（不阻塞生产者）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class EventBus:
    """全局事件总线（singleton）。"""

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._history: List[Dict[str, Any]] = []  # 最近 50 条，供新 SSE 客户端 replay
        self._max_history = 50

    def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        """发布事件（可从任何线程/协程调用）。"""
        evt = {
            "type": event_type,
            "ts": time.time(),
            "data": data,
        }
        # 保存历史
        self._history.append(evt)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # 扇出到所有订阅者
        dead: List[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # 丢弃最旧事件
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)

        for q in dead:
            self._subscribers.discard(q)

    def subscribe(self) -> asyncio.Queue:
        """订阅事件流（返回 queue，SSE 端点 await queue.get()）。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """取消订阅。"""
        self._subscribers.discard(q)

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """返回最近 N 条事件（供 replay）。"""
        return self._history[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# 全局单例
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """获取全局事件总线单例。"""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
