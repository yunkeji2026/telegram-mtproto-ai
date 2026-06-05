"""按会话维度的出站推送中枢（访客 SSE 用）。

与全局 EventBus（内部设备/收件箱事件）刻意分离：
  - 安全：公网访客 SSE 不应收到内部设备事件；
  - 伸缩：仅把出站消息投递给目标会话的订阅者，避免全局扇出 O(N²)。

仅在主事件循环线程内 publish（AI 后台任务/坐席发送均在 loop 上），
asyncio.Queue 线程安全前提成立。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Set


class WebOutboundHub:
    def __init__(self) -> None:
        self._subs: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subs.setdefault(conversation_id, set()).add(q)
        return q

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        s = self._subs.get(conversation_id)
        if s:
            s.discard(q)
            if not s:
                self._subs.pop(conversation_id, None)

    def publish(self, conversation_id: str, event: Dict[str, Any]) -> None:
        for q in list(self._subs.get(conversation_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    @property
    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subs.values())


_hub: "WebOutboundHub | None" = None


def get_web_outbound_hub() -> WebOutboundHub:
    global _hub
    if _hub is None:
        _hub = WebOutboundHub()
    return _hub
