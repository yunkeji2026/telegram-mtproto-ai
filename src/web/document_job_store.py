"""L2c-2：文档翻译「待处理作业」暂存（配合 SSE 进度流）。

EventSource 只能 GET，无法携 10MB 文件体；故拆两步：POST 上传校验后把**输入载荷**暂存
本存储换 ``job_id`` 即返回；前端用 ``job_id`` 开 SSE，**翻译在该 GET 长连接内执行**并逐段
推进度——避免 fire-and-forget 后台任务（在 ASGI 请求结束时可能被取消，且难测）。

进程内单例 + TTL + 条目上限 + 一次性消费（GET 取走即删）+ 线程安全。重启清空。
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

_TTL_SECONDS = 120  # POST→GET 紧随，2 分钟足够；超时即作废（前端会报链接失效）
_MAX_JOBS = 128


@dataclass
class PendingDocJob:
    payload: Dict[str, Any]
    expires_at: float


class DocumentJobStore:
    """待处理文档翻译作业：create(payload)→token；take(token)→payload（一次性）。"""

    def __init__(self, *, ttl: float = _TTL_SECONDS, max_jobs: int = _MAX_JOBS) -> None:
        self._ttl = float(ttl)
        self._max_jobs = int(max_jobs)
        self._lock = threading.Lock()
        self._jobs: Dict[str, PendingDocJob] = {}

    def _evict_locked(self, now: float) -> None:
        for k in [k for k, j in self._jobs.items() if j.expires_at <= now]:
            self._jobs.pop(k, None)
        while len(self._jobs) > self._max_jobs and self._jobs:
            oldest = min(self._jobs, key=lambda k: self._jobs[k].expires_at)
            self._jobs.pop(oldest, None)

    def create(self, payload: Dict[str, Any]) -> str:
        now = time.time()
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._evict_locked(now)
            self._jobs[token] = PendingDocJob(dict(payload), now + self._ttl)
        return token

    def take(self, token: str) -> Optional[Dict[str, Any]]:
        """取回并删除（一次性）。过期/不存在 → None。"""
        now = time.time()
        with self._lock:
            self._evict_locked(now)
            j = self._jobs.pop(str(token or ""), None)
        if j is None or j.expires_at <= now:
            return None
        return j.payload

    def count(self) -> int:
        with self._lock:
            self._evict_locked(time.time())
            return len(self._jobs)


_singleton: Optional[DocumentJobStore] = None
_singleton_lock = threading.Lock()


def get_document_job_store() -> DocumentJobStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = DocumentJobStore()
    return _singleton


__all__ = ["DocumentJobStore", "PendingDocJob", "get_document_job_store"]
