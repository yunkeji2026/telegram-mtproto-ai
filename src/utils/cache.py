"""通用缓存原语。

当前提供：
  - ``SingleEntryTTLCache``: 单条目 + monotonic clock + threading.Lock 的轻量 cache。
    适合"全域聚合数据短期不变"的场景（如运营 dashboard 的 trend / digest）。

不提供（已存在）：
  - 多 key + LRU 淘汰：见 ``src.utils.kb_store._LRUCache``
    （领域专用 cache，不与本模块合并以避免错误抽象）。

设计理由（与 ``kb_store._LRUCache`` 区分）：
  - 这是"全域聚合接口"用的 single-entry cache：key 由 (param_combo, day_bucket)
    构成，命中率天然高，不需要 LRU 淘汰策略
  - 用 monotonic clock 防系统时间跳变（与 ``kb_store._LRUCache`` 用 ``time.time``
    的差异是有意保留的——一个怕墙钟跳变，一个不太关心）
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


class SingleEntryTTLCache:
    """单条目 TTL 缓存。

    - 同 key 命中 + 未过期 → 返回 payload；否则 None
    - put 会覆盖（无论 key 是否相同）
    - clear 显式清空（测试隔离 / 写后失效用）

    并发安全：内部 ``threading.Lock``。

    使用：

        cache = SingleEntryTTLCache(ttl_s=60)
        if (hit := cache.get(key)) is not None:
            return hit
        result = expensive_compute()
        cache.put(key, result)
        return result
    """

    __slots__ = ("_ttl_s", "_lock", "_entry")

    def __init__(self, ttl_s: float = 60.0) -> None:
        if ttl_s < 0:
            raise ValueError("ttl_s must be >= 0")
        self._ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        # (key, expire_monotonic, payload) | None
        self._entry: Optional[Tuple[Any, float, Dict[str, Any]]] = None

    def get(self, key: Any) -> Optional[Dict[str, Any]]:
        if self._ttl_s <= 0:
            return None
        with self._lock:
            ent = self._entry
            if ent is None:
                return None
            k, exp, payload = ent
            if k == key and time.monotonic() < exp:
                return payload
        return None

    def put(self, key: Any, payload: Dict[str, Any]) -> None:
        if self._ttl_s <= 0:
            return
        with self._lock:
            self._entry = (key, time.monotonic() + self._ttl_s, payload)

    def clear(self) -> None:
        with self._lock:
            self._entry = None

    @property
    def ttl_s(self) -> float:
        return self._ttl_s
