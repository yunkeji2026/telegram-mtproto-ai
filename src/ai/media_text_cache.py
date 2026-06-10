"""P58-2：多模态识别结果缓存（OCR/ASR 复用）。

OCR/ASR 都是高延迟外部调用。同一张图/同一段语音被重复识别（坐席手滑点两次、
或同一图片转发多次）时，按**媒体内容 hash** 命中缓存可直接跳过 provider 调用。

进程级、有界（FIFO 淘汰）、线程安全；只缓存识别出的**文本**（非字节），TTL 可选。
键约定：``f"{kind}:{sha1}"``，kind ∈ {ocr, asr}。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple


class MediaTextCache:
    def __init__(self, max_entries: int = 256, ttl_sec: float = 3600.0) -> None:
        self._lock = threading.RLock()
        self._d: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._max = int(max_entries)
        self._ttl = float(ttl_sec)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        if not key:
            return None
        with self._lock:
            item = self._d.get(key)
            if item is None:
                self.misses += 1
                return None
            text, ts = item
            if self._ttl > 0 and (time.time() - ts) > self._ttl:
                self._d.pop(key, None)
                self.misses += 1
                return None
            self._d.move_to_end(key)  # LRU 触达
            self.hits += 1
            return text

    def put(self, key: str, text: str) -> None:
        if not key or not text:
            return
        with self._lock:
            self._d[key] = (text, time.time())
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._d.clear()
            self.hits = 0
            self.misses = 0


def hash_file(path: str) -> Optional[str]:
    """读文件算 sha1；失败（文件不存在/不可读）返回 None（调用方跳过缓存）。"""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


_SINGLETON: Optional[MediaTextCache] = None
_LOCK = threading.Lock()


def get_media_text_cache() -> MediaTextCache:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = MediaTextCache()
    return _SINGLETON


__all__ = ["MediaTextCache", "get_media_text_cache", "hash_file"]
