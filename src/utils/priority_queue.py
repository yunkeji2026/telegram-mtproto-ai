"""优先级消息队列 — asyncio 实现，支持背压控制"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("PriorityQueue")

PRIORITY_CRITICAL = 0
PRIORITY_HIGH = 10
PRIORITY_NORMAL = 50
PRIORITY_LOW = 90

INTENT_PRIORITY = {
    "gxp_command": PRIORITY_HIGH,
    "enhanced_quota_config": PRIORITY_HIGH,
    "quota_config": PRIORITY_HIGH,
    "order_query": PRIORITY_HIGH,
    "channel_info": PRIORITY_NORMAL,
    "price_check": PRIORITY_NORMAL,
    "status_check": PRIORITY_NORMAL,
    "greeting": PRIORITY_LOW,
    "small_talk": PRIORITY_LOW,
    "complaint": PRIORITY_NORMAL,
}


@dataclass(order=True)
class PriorityMessage:
    priority: int
    timestamp: float = field(compare=False)
    data: Any = field(compare=False)


class PriorityMessageQueue:

    def __init__(self, config: dict = None):
        cfg = (config or {}).get("message_queue", {})
        self._enabled = cfg.get("enabled", False)
        self._max_size = int(cfg.get("max_size", 500))
        self._backpressure_threshold = float(cfg.get("backpressure_threshold", 0.8))
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=self._max_size)
        self._stats = {"enqueued": 0, "dequeued": 0, "dropped": 0, "backpressure_events": 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def utilization(self) -> float:
        return self._queue.qsize() / max(self._max_size, 1)

    def is_backpressured(self) -> bool:
        return self.utilization >= self._backpressure_threshold

    async def enqueue(self, data: Any, priority: int = PRIORITY_NORMAL) -> bool:
        if not self._enabled:
            return False
        if self._queue.full():
            if priority >= PRIORITY_NORMAL:
                self._stats["dropped"] += 1
                logger.debug("[队列] 丢弃低优先级消息 (queue full, pri=%d)", priority)
                return False
            try:
                self._queue.get_nowait()
                self._stats["dropped"] += 1
            except asyncio.QueueEmpty:
                pass

        if self.is_backpressured() and priority >= PRIORITY_LOW:
            self._stats["backpressure_events"] += 1
            self._stats["dropped"] += 1
            return False

        msg = PriorityMessage(priority=priority, timestamp=time.time(), data=data)
        try:
            self._queue.put_nowait(msg)
            self._stats["enqueued"] += 1
            return True
        except asyncio.QueueFull:
            self._stats["dropped"] += 1
            return False

    async def dequeue(self, timeout: float = 5.0) -> Optional[Any]:
        try:
            msg = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            self._stats["dequeued"] += 1
            return msg.data
        except asyncio.TimeoutError:
            return None

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "size": self.size,
            "max_size": self._max_size,
            "utilization": round(self.utilization * 100, 1),
            "backpressured": self.is_backpressured(),
        }

    @staticmethod
    def get_priority_for_intent(intent: str) -> int:
        return INTENT_PRIORITY.get(intent, PRIORITY_NORMAL)
