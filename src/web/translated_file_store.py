"""L2c-1：译后文档临时令牌存储。

翻译端点把译后**二进制**存这里换一个不可猜 token，返回短链；GET 下载端点凭 token
取回后即删（一次性消费）。**避免在 JSON 里塞大 base64**——base64 膨胀 ~33% + 编解码
两端各持一份，10MB 文件实际内存翻数倍；改令牌短链后 JSON 只带几十字节。

进程内单例 + TTL + 条目数/总字节双上限 + 线程安全。重启即清空（临时产物，无需持久化）。
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

_TTL_SECONDS = 600  # 10 分钟：足够用户点下载，又不长期占内存
_MAX_ENTRIES = 64
_MAX_TOTAL_BYTES = 256 * 1024 * 1024  # 256MB 总上限（防堆积 OOM）


@dataclass
class FileEntry:
    data: bytes
    filename: str
    content_type: str
    expires_at: float


class TranslatedFileStore:
    """进程内 TTL 令牌存储：put→token；take→取回并删除（一次性）。"""

    def __init__(
        self,
        *,
        ttl: float = _TTL_SECONDS,
        max_entries: int = _MAX_ENTRIES,
        max_total_bytes: int = _MAX_TOTAL_BYTES,
    ) -> None:
        self._ttl = float(ttl)
        self._max_entries = int(max_entries)
        self._max_total = int(max_total_bytes)
        self._lock = threading.Lock()
        self._store: Dict[str, FileEntry] = {}

    def _evict_expired_locked(self, now: float) -> None:
        for k in [k for k, e in self._store.items() if e.expires_at <= now]:
            self._store.pop(k, None)

    def _total_bytes_locked(self) -> int:
        return sum(len(e.data) for e in self._store.values())

    def put(self, data: bytes, filename: str, content_type: str) -> str:
        now = time.time()
        blob = bytes(data)
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._evict_expired_locked(now)
            # 容量保护：超条目数或超总字节 → 逐出最早到期者，直至放得下
            while self._store and (
                len(self._store) >= self._max_entries
                or self._total_bytes_locked() + len(blob) > self._max_total
            ):
                oldest = min(self._store, key=lambda k: self._store[k].expires_at)
                self._store.pop(oldest, None)
            self._store[token] = FileEntry(blob, filename, content_type, now + self._ttl)
        return token

    def take(self, token: str) -> Optional[FileEntry]:
        """取回并删除（一次性消费）。过期/不存在 → None。"""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            e = self._store.pop(str(token or ""), None)
        if e is None or e.expires_at <= now:
            return None
        return e

    def count(self) -> int:
        with self._lock:
            self._evict_expired_locked(time.time())
            return len(self._store)


_singleton: Optional[TranslatedFileStore] = None
_singleton_lock = threading.Lock()


def get_translated_file_store() -> TranslatedFileStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TranslatedFileStore()
    return _singleton


__all__ = ["TranslatedFileStore", "FileEntry", "get_translated_file_store"]
