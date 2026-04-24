"""环形日志缓冲区 + SSE 推送支持"""

import asyncio
import collections
import json
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional

logger = logging.getLogger("LogBuffer")


class LogBuffer(logging.Handler):
    """收集日志记录到环形缓冲区，供 Web UI 实时查看"""

    def __init__(self, maxlen: int = 500, level=logging.INFO):
        super().__init__(level)
        self._buffer = collections.deque(maxlen=maxlen)
        self._subscribers: List[asyncio.Queue] = []
        self.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%H:%M:%S'
        ))

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            self._buffer.append(entry)
            for q in self._subscribers[:]:
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    pass
        except Exception:
            pass

    def get_recent(self, limit: int = 100) -> List[Dict]:
        items = list(self._buffer)
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def stream(self) -> AsyncGenerator[str, None]:
        q = self.subscribe()
        try:
            while True:
                entry = await q.get()
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(q)


_global_buffer: Optional[LogBuffer] = None


def get_log_buffer() -> LogBuffer:
    global _global_buffer
    if _global_buffer is None:
        _global_buffer = LogBuffer()
    return _global_buffer


def install_log_buffer(level=logging.INFO):
    buf = get_log_buffer()
    buf.setLevel(level)
    root = logging.getLogger()
    if buf not in root.handlers:
        root.addHandler(buf)
    return buf
